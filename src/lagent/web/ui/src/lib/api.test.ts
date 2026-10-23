import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installFetch, parseBody } from "../test/fakeFetch";
import {
  ApiError,
  apiGet,
  apiPost,
  bindAuth,
  currentEpoch,
  errText,
  login,
  register,
  rotateSession,
} from "./api";

/* 这些用例守的是"请求发出去的样子"和"错误翻译成人话"。
 * 界面长什么样由组件的测试管，这里只管线缆那一层。 */

const ME = { id: 2, username: "alice", role: "user", certs: [], rank: 1, capabilities: [] };

beforeEach(() => {
  bindAuth(() => null, () => {});
  rotateSession();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request", () => {
  it("带上 Bearer 令牌", async () => {
    bindAuth(() => "tok-123", () => {});
    const { calls } = installFetch(() => ({ json: { ok: true } }));
    await apiGet("/api/labs");
    const auth = (calls[0].init.headers as Record<string, string>).Authorization;
    expect(auth).toBe("Bearer tok-123");
  });

  it("不主动往请求体里塞身份字段", async () => {
    // 身份由令牌决定，body 里再写一遍 user_id 就等于给了前端一个
    // 冒充别人的入口 —— 后端会 403，但更该做的是根本不发。
    bindAuth(() => "tok-123", () => {});
    const { calls } = installFetch(() => ({ json: { ok: true } }));
    await apiPost("/api/reservations", {
      equipment_id: 3,
      date: "2026-11-01",
      start: "10:00",
      end: "12:00",
    });
    const body = parseBody(calls[0]);
    expect(body).not.toBeNull();
    expect(Object.keys(body!)).not.toContain("user_id");
    expect(Object.keys(body!)).not.toContain("username");
  });

  it("401 时先清会话再抛错", async () => {
    const onUnauthorized = vi.fn();
    bindAuth(() => "tok-123", onUnauthorized);
    installFetch(() => ({ status: 401, json: { detail: "令牌无效" } }));
    await expect(apiGet("/api/auth/me")).rejects.toThrow("登录状态已失效，请重新登录");
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("把后端的 detail 原样翻出来，而不是只说 HTTP 500", async () => {
    installFetch(() => ({ status: 409, json: { detail: "该时段已被占用" } }));
    const err = (await apiGet("/api/reservations").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("该时段已被占用");
  });

  it("连不上服务器时标成网络错误，好让界面说人话", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));
    const err = (await apiGet("/api/labs").catch((e) => e)) as ApiError;
    expect(err.network).toBe(true);
    expect(err.status).toBe(0);
    expect(err.message).toContain("无法连接到服务器");
  });

  it("会话换代之后，旧请求的结果被丢掉", async () => {
    // 登出后上一个会话的响应才回来 —— 不拦住就会把"别人的数据"
    // 写进新会话的界面里。靠 epoch 对比，不靠组件自己记得。
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    installFetch(async () => {
      await gate;
      return { json: { ok: true } };
    });
    const before = currentEpoch();
    const pending = apiGet("/api/labs");
    rotateSession();
    release();
    const err = (await pending.catch((e) => e)) as Error;
    expect(err.name).toBe("AbortError");
    expect(currentEpoch()).toBe(before + 1);
  });
});

describe("errText", () => {
  it("字段级校验错误拼成一句人话", () => {
    const body = [
      { loc: ["body", "date"], msg: "日期格式不对" },
      { loc: ["body", "start"], msg: "不能为空" },
    ];
    // 后端的形态是 {detail: [...]}，不是裸数组
    expect(errText({ detail: body }, 422)).toBe("date：日期格式不对；start：不能为空");
  });

  it("没有可用信息时退回到状态码", () => {
    expect(errText(null, 502)).toBe("HTTP 502");
    expect(errText({}, 500)).toBe("HTTP 500");
  });
});

describe("登录与注册", () => {
  it("注册请求体里没有 role —— 多一个字段就是 422", async () => {
    // RegisterRequest 是 extra="forbid"。前端"顺手"带上 role 不会提权，
    // 只会让注册接口直接 422，而且报错是"多了一个字段"，很难联想。
    const { calls } = installFetch(() => ({
      json: { access_token: "t", token_type: "bearer", expires_in: 3600, user: ME },
    }));
    await register("新同学", "new@example.com", "pw12345678");
    const body = parseBody(calls[0]);
    expect(Object.keys(body!)).toEqual(["username", "email", "password"]);
  });

  it("登录失败不会触发会话过期回调", async () => {
    // 走裸 fetch 的原因：401 若触发 signOut，闸门会被重置，
    // 刚填的账号密码连同错误提示一起消失，用户只看到"点了没反应"。
    const onUnauthorized = vi.fn();
    bindAuth(() => null, onUnauthorized);
    installFetch(() => ({ status: 401, json: { detail: "用户名或密码错误" } }));
    await expect(login("李娜", "wrong")).rejects.toThrow("用户名或密码错误");
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});
