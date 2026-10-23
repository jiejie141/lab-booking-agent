import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { installFetch, parseBody } from "../test/fakeFetch";
import { Wrap, makeLabs, makeMe } from "../test/harness";
import { BookPane } from "./Book";

/* 直接预约面板守三件事：
 *   ① 请求体里**没有身份字段**（身份只由令牌决定）；
 *   ② 请求成功后真正的预约在 data.reservation 里，要能显示单号；
 *   ③ 失败时把服务端的原因原样显示，再按状态码补一句"下一步"。
 * 第三条是这个项目的性格：409 说"换个时段"，403 说"这是资格问题"，
 * 而不是统一一句"预约失败"。 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function reply(json: unknown, status = 200) {
  return { status, json };
}

describe("BookPane", () => {
  it("提交的请求体里没有 user_id / username", async () => {
    const { calls } = installFetch(() =>
      reply({
        ok: true,
        message: "ok",
        reservation: { id: 77, status: "confirmed", slot: "2026-11-02 10:00-12:00" },
      }),
    );
    render(<Wrap me={makeMe()} labs={makeLabs()}><BookPane onBooked={() => {}} /></Wrap>);
    const user = userEvent.setup();
    const select = screen.getByLabelText("设备") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    await user.selectOptions(select, "11");
    await user.click(screen.getByRole("button", { name: "提交预约" }));
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    const body = parseBody(calls[0])!;
    expect(body.equipment_id).toBe(11);
    expect(Object.keys(body)).not.toContain("user_id");
    expect(Object.keys(body)).not.toContain("username");
    expect(Object.keys(body).sort()).toEqual(["date", "end", "equipment_id", "purpose", "start"]);
  });

  it("成功时显示的是 reservation 里的单号，不是笼统的 ok", async () => {
    installFetch(() =>
      reply({
        ok: true,
        message: "ok",
        reservation: { id: 77, status: "confirmed", slot: "2026-11-02 10:00-12:00" },
      }),
    );
    render(<Wrap me={makeMe()} labs={makeLabs()}><BookPane onBooked={() => {}} /></Wrap>);
    const user = userEvent.setup();
    const select = screen.getByLabelText("设备") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    await user.selectOptions(select, "11");
    await user.click(screen.getByRole("button", { name: "提交预约" }));
    // 提示条和结果区都会写"预约成功"，所以用 findAll —— 单号只出现在结果区
    const hits = await screen.findAllByText(/预约成功/);
    expect(hits.length).toBeGreaterThan(0);
    expect(hits.some((n) => n.textContent!.includes("77"))).toBe(true);
  });

  it("需要审批时说的是等待审批，不是预约成功", async () => {
    installFetch(() =>
      reply({
        ok: true,
        message: "ok",
        reservation: { id: 88, status: "pending", slot: "2026-11-02 10:00-12:00" },
      }),
    );
    render(<Wrap me={makeMe()} labs={makeLabs()}><BookPane onBooked={() => {}} /></Wrap>);
    const user = userEvent.setup();
    const select = screen.getByLabelText("设备") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    await user.selectOptions(select, "11");
    await user.click(screen.getByRole("button", { name: "提交预约" }));
    expect(await screen.findByText(/已提交，等待管理员审批/)).toBeTruthy();
  });

  it("409 时补一句'换个时段'，而不是只说失败", async () => {
    installFetch(() => reply({ detail: "该时段已被占用" }, 409));
    render(<Wrap me={makeMe()} labs={makeLabs()}><BookPane onBooked={() => {}} /></Wrap>);
    const user = userEvent.setup();
    const select = screen.getByLabelText("设备") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    await user.selectOptions(select, "11");
    await user.click(screen.getByRole("button", { name: "提交预约" }));
    const line = await screen.findByText(/该时段已被占用/);
    expect(line.textContent).toContain("换个时段再试");
  });

  it("403 时说明这是资格问题，不是时段问题", async () => {
    installFetch(() => reply({ detail: "缺少资质：光谱" }, 403));
    render(<Wrap me={makeMe()} labs={makeLabs()}><BookPane onBooked={() => {}} /></Wrap>);
    const user = userEvent.setup();
    const select = screen.getByLabelText("设备") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    await user.selectOptions(select, "11");
    await user.click(screen.getByRole("button", { name: "提交预约" }));
    const line = await screen.findByText(/缺少资质/);
    expect(line.textContent).toContain("资格问题");
  });

  it("维修中的设备在下拉里是禁用的", async () => {
    render(<Wrap me={makeMe()} labs={makeLabs()}><BookPane onBooked={() => {}} /></Wrap>);
    const select = screen.getByLabelText("设备") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    const byText = (t: string) =>
      Array.from(select.options).find((o) => o.textContent!.includes(t))!;
    expect(byText("荧光光谱仪").disabled).toBe(false);
    expect(byText("紫外分光光度计").disabled).toBe(true);
  });

  it("结束时间不晚于开始时间就不发请求", async () => {
    const { calls } = installFetch(() => reply({ ok: true }));
    render(<Wrap me={makeMe()} labs={makeLabs()}><BookPane onBooked={() => {}} /></Wrap>);
    const user = userEvent.setup();
    const select = screen.getByLabelText("设备") as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    await user.selectOptions(select, "11");
    await user.selectOptions(screen.getByLabelText("开始"), "12:00");
    await user.selectOptions(screen.getByLabelText("结束"), "10:00");
    await user.click(screen.getByRole("button", { name: "提交预约" }));
    expect(await screen.findByText(/结束时间要晚于开始时间/)).toBeTruthy();
    expect(calls.filter((c) => c.url.startsWith("/api/reservations"))).toHaveLength(0);
  });
});
