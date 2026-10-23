import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { installFetch, parseBody } from "../test/fakeFetch";
import { Wrap, adminMe, makeLabs, resetSession } from "../test/harness";
import { GateSimPane } from "./GateSim";

/* 门禁面板守三处只有踩过才知道的地方：
 *   ① 放行时 reason_code 是**空串** —— 必须先看 granted 再查中文表，
 *      否则一次成功的放行会被画成红色的"—"；
 *   ② /api/access/inside 返回 {count, inside}，不是裸数组；
 *   ③ 「刷卡出场」只在识别到身份时出现 —— 出场必须知道是谁。 */

function replyGranted(over: Record<string, unknown> = {}) {
  return {
    granted: true,
    reason_code: "",
    message: "放行",
    permit_id: 5,
    user_id: 2,
    user_name: "李娜",
    lab_id: 1,
    lab_label: "光谱实验室",
    ...over,
  };
}

beforeEach(() => {
  resetSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(verifyReply: Record<string, unknown>) {
  const h = installFetch((url) => {
    if (url.startsWith("/api/access/verify")) return { json: verifyReply };
    if (url.startsWith("/api/access/inside")) return { json: { count: 1, inside: [] } };
    if (url.startsWith("/api/labs")) return { json: makeLabs() };
    if (url.startsWith("/api/users")) return { json: [] };
    return { json: {} };
  });
  render(<Wrap me={adminMe()} labs={makeLabs()}><GateSimPane /></Wrap>);
  return h;
}

async function verifyWithCredential() {
  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("实验室"), "1");
  await user.type(screen.getByPlaceholderText(/凭证串/), "permit-abc");
  await user.click(screen.getByRole("button", { name: "核验" }));
  return user;
}

describe("GateSimPane", () => {
  it("放行时显示「放行」，不是查出空白的红色标签", async () => {
    mount(replyGranted());
    await verifyWithCredential();
    expect(await screen.findByText(/✓ 放行/)).toBeTruthy();
    // 空 reason_code 落成占位符，而不是被当成一个理由去查表
    expect(screen.getByText("放行", { selector: ".tag" })).toBeTruthy();
  });

  it("拒绝时把 reason_code 翻成中文", async () => {
    mount(replyGranted({ granted: false, reason_code: "no_permit", message: "没有有效凭证", permit_id: null }));
    await verifyWithCredential();
    expect(await screen.findByText(/✗ 拒绝/)).toBeTruthy();
    expect(screen.getByText("无有效凭证", { selector: ".tag" })).toBeTruthy();
  });

  it("核验请求体里 gate_id 是固定的 console-sim —— 便于审计时区分真闸机与模拟", async () => {
    const { calls } = mount(replyGranted());
    await verifyWithCredential();
    await waitFor(() => expect(calls.some((c) => c.url.startsWith("/api/access/verify"))).toBe(true));
    const body = parseBody(calls.find((c) => c.url.startsWith("/api/access/verify"))!)!;
    expect(body.gate_id).toBe("console-sim");
    expect(body.lab_id).toBe(1);
    expect(body.credential).toBe("permit-abc");
    expect(body.precheck).toBe(false);
  });

  it("识别到身份时才给「刷卡出场」", async () => {
    mount(replyGranted());
    await verifyWithCredential();
    expect(await screen.findByRole("button", { name: "刷卡出场" })).toBeTruthy();
  });

  it("只凭凭证放行时提示要选身份才能出场", async () => {
    mount(replyGranted({ user_id: null, user_name: null }));
    await verifyWithCredential();
    expect(await screen.findByText(/出场必须识别到身份/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "刷卡出场" })).toBeNull();
  });

  it("在馆名单读的是 {count, inside}，不是裸数组", async () => {
    mount(replyGranted());
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "在馆名单" }));
    // 写成 data.length 的话这里会抛异常、什么都不显示
    expect(await screen.findByText(/在馆 0 人/)).toBeTruthy();
  });

  it("凭证与身份都不给就不发请求", async () => {
    const { calls } = mount(replyGranted());
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("实验室"), "1");
    await user.click(screen.getByRole("button", { name: "核验" }));
    expect(await screen.findByText(/凭证与身份至少给一个/)).toBeTruthy();
    expect(calls.some((c) => c.url.startsWith("/api/access/verify"))).toBe(false);
  });

  it("没选实验室就不能签发 —— 否则凭证不知道该对哪扇门有效", async () => {
    mount(replyGranted());
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /签发/ }));
    expect(await screen.findByText(/还没选实验室/)).toBeTruthy();
  });
});
