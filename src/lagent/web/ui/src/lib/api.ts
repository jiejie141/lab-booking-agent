/* 唯一的 HTTP 入口。
 *
 * 这一层存在，是为了让「每条请求都会做的那四件事」真的只写一遍：
 *
 *   1. 带 token；2. 401 就走会话过期（清会话 + 回闸门）；
 *   3. 把后端的 detail 翻成人话（含 FastAPI 422 的数组形态）；
 *   4. 网络层异常兜住 —— 否则面板会永远停在骨架屏上。
 *
 * 原来的控制台把这四条写在一个 apiFull 函数里，React 版原样保留它们，
 * 因为每一条都对应一次真踩过的坑（见下面各自的注释）。
 */

import type { MeOut, TokenResponse, HealthDetails, TodayOut } from "./types";

export class ApiError extends Error {
  status: number;
  raw: unknown;
  network: boolean;

  constructor(message: string, opts: { status?: number; raw?: unknown; network?: boolean } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = opts.status ?? 0;
    this.raw = opts.raw;
    this.network = opts.network ?? false;
  }
}

/* 这两个钩子由 App 注入 —— api 模块本身不 import React，
   这样它能被单测直接调用，不需要挂一个 Provider。 */
let tokenGetter: () => string | null = () => null;
let unauthorizedHandler: () => void = () => {};

export function bindAuth(getToken: () => string | null, onUnauthorized: () => void): void {
  tokenGetter = getToken;
  unauthorizedHandler = onUnauthorized;
}

/* ---- 会话世代（epoch）----
   登出/换账号时，上一秒发出去的请求可能还在路上。它回来之后如果照常
   写进 state，就会把**上一个身份**的数据画到新会话上。
   做法：每次清会话把 epoch +1，并在请求返回后比一次；不一致就当作
   被取消（AbortError），让它安静地死掉。 */
let epoch = 0;
let inflight = new AbortController();

export const currentEpoch = (): number => epoch;

export function rotateSession(): AbortSignal {
  epoch += 1;
  inflight.abort();
  inflight = new AbortController();
  return inflight.signal;
}

export const sessionSignal = (): AbortSignal => inflight.signal;

interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

export interface RawResponse<T> {
  data: T;
  headers: Headers;
}

function isPlainSerializable(body: unknown): boolean {
  if (body === undefined || body === null) return false;
  if (typeof body === "string") return false;
  if (typeof FormData !== "undefined" && body instanceof FormData) return false;
  if (typeof Blob !== "undefined" && body instanceof Blob) return false;
  if (typeof URLSearchParams !== "undefined" && body instanceof URLSearchParams) return false;
  return true;
}

/** 把后端返回的 detail / message 翻成一句可以直接显示给用户的话。 */
export function errText(body: unknown, status: number): string {
  const rec = (body ?? {}) as Record<string, unknown>;
  const detail = rec.detail ?? rec.message;

  if (typeof detail === "string") return detail;

  // FastAPI 的 422 是 [{loc:[...], msg, type}]。
  // 直接把数组丢进界面会显示成 [object Object]；拼成「字段：原因」才有用。
  if (Array.isArray(detail)) {
    const parts = detail
      .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
      .map((item) => {
        const loc = Array.isArray(item.loc) ? item.loc : [];
        // loc 的前缀是请求的哪个部分，不是字段名，留着只会让人困惑
        const field = loc.filter((x) => x !== "body" && x !== "query" && x !== "path").join(".");
        const msg = String(item.msg ?? "");
        return field ? field + "：" + msg : msg;
      })
      .filter((s) => s.length > 0);
    return parts.length ? parts.join("；") : "请求参数校验失败";
  }

  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return "HTTP " + status;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<RawResponse<T>> {
  const startEpoch = epoch;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(opts.headers ?? {}),
  };
  const token = tokenGetter();
  if (token) headers.Authorization = "Bearer " + token;

  // 传了原生对象就自动 JSON 化。这条是补一个真 bug 的：
  // 门禁面板曾经直接传对象，被 String() 成了 "[object Object]"，后端 422。
  const body = isPlainSerializable(opts.body) ? JSON.stringify(opts.body) : (opts.body as BodyInit | undefined);

  let resp: Response;
  try {
    resp = await fetch(path, {
      method: opts.method ?? "GET",
      headers,
      body: body as BodyInit | undefined,
      signal: opts.signal ?? inflight.signal,
    });
  } catch (e) {
    const err = e as Error;
    if (err.name === "AbortError") throw err;
    // ★ fetch 自己抛异常（断网、DNS、后端没起）必须兜住。
    // 不兜的话面板会永远停在骨架屏 —— 看起来像"一直在加载"，
    // 实际是"永远不会加载"。
    const offline = typeof navigator !== "undefined" && navigator.onLine === false;
    throw new ApiError(offline ? "当前处于离线状态，连上网后重试" : "无法连接到服务器，请稍后重试", {
      status: 0,
      network: true,
      raw: err.message,
    });
  }

  const data = (await resp.json().catch(() => ({}))) as T;

  if (epoch !== startEpoch) throw new DOMException("会话已切换", "AbortError");

  if (resp.status === 401) {
    unauthorizedHandler();
    throw new ApiError("登录状态已失效，请重新登录", { status: 401, raw: data });
  }
  if (!resp.ok) {
    throw new ApiError(errText(data, resp.status), { status: resp.status, raw: data });
  }
  return { data, headers: resp.headers };
}

export const apiGet = <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal });
export const apiPost = <T>(path: string, body?: unknown, signal?: AbortSignal) =>
  request<T>(path, { method: "POST", body, signal });
export const apiPatch = <T>(path: string, body?: unknown, signal?: AbortSignal) =>
  request<T>(path, { method: "PATCH", body, signal });

/* ---- 认证 ----
   登录/注册**不走 request()**：401 会触发会话过期、把闸门重置掉，
   于是刚填的用户名密码连同错误提示一起消失 —— 用户只会看到"点了没反应"。
   这两个直接用 fetch，错误自己显示。 */
export async function login(username: string, password: string): Promise<TokenResponse> {
  const resp = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = (await resp.json().catch(() => ({}))) as Record<string, unknown>;
  if (!resp.ok) throw new ApiError(errText(data, resp.status), { status: resp.status, raw: data });
  return data as unknown as TokenResponse;
}

export async function register(username: string, email: string, password: string): Promise<TokenResponse> {
  const resp = await fetch("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // 不要带 role：RegisterRequest 是 extra="forbid"，多一个字段就是 422
    body: JSON.stringify({ username, email, password }),
  });
  const data = (await resp.json().catch(() => ({}))) as Record<string, unknown>;
  if (!resp.ok) throw new ApiError(errText(data, resp.status), { status: resp.status, raw: data });
  return data as unknown as TokenResponse;
}

export const fetchMe = (): Promise<MeOut> => apiGet<MeOut>("/api/auth/me").then((r) => r.data);
export const fetchHealthDetails = (): Promise<HealthDetails> =>
  apiGet<HealthDetails>("/api/health/details").then((r) => r.data);
export const fetchToday = (): Promise<TodayOut> => apiGet<TodayOut>("/api/today").then((r) => r.data);

export async function logout(): Promise<void> {
  // 先请服务端作废令牌，再清本地 —— 只清本地只是"忘了它"。
  // 服务端失败（令牌本来就过期）也不该挡住用户离开，所以吞掉异常。
  try {
    await apiPost("/api/auth/logout");
  } catch {
    /* 令牌已失效或断网：本地照样清 */
  }
}

/** SSE 流式对话要自己拼请求头 —— 它不是 JSON 请求/响应，走不了 request()。 */
export function authHeader(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = tokenGetter();
  if (token) headers.Authorization = "Bearer " + token;
  return headers;
}

/** 导出 CSV：必须是裸 fetch，因为要拿 blob 而不是 JSON。 */
export async function exportReservations(): Promise<void> {
  const token = tokenGetter();
  const resp = await fetch("/api/reservations/export.csv", {
    headers: token ? { Authorization: "Bearer " + token } : {},
  });
  if (resp.status === 401) {
    unauthorizedHandler();
    throw new ApiError("登录状态已失效，请重新登录", { status: 401 });
  }
  if (!resp.ok) throw new ApiError("HTTP " + resp.status, { status: resp.status });
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "reservations.csv";
  a.click();
  URL.revokeObjectURL(url);
}
