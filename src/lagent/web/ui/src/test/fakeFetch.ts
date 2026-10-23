import { vi } from "vitest";

/* fetch 的替身。写成一个显式的小工厂而不是用 msw：
   这里要验的是「请求发出去的样子」和「响应回来之后的处理」，
   不需要一个真的 HTTP 语义层 —— 反而需要能一眼看到 body 里有什么。 */

export interface FakeReply {
  status?: number;
  json?: unknown;
  headers?: Record<string, string>;
}

export interface FakeCall {
  url: string;
  init: RequestInit;
}

export function installFetch(
  handler: (url: string, init: RequestInit) => FakeReply | Promise<FakeReply>,
): { calls: FakeCall[] } {
  const calls: FakeCall[] = [];
  const spy = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    calls.push({ url, init });
    const reply = await handler(url, init);
    const status = reply.status ?? 200;
    const payload = reply.json;
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: new Headers(reply.headers ?? {}),
      json: async () => {
        if (payload === undefined) throw new Error("no json body");
        return payload;
      },
      blob: async () => new Blob([]),
      text: async () => (payload === undefined ? "" : JSON.stringify(payload)),
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", spy);
  return { calls };
}

export function parseBody(call: FakeCall): Record<string, unknown> | null {
  const raw = call.init.body;
  if (typeof raw !== "string") return null;
  return JSON.parse(raw) as Record<string, unknown>;
}
