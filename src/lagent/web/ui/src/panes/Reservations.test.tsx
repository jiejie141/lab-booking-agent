import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { installFetch, parseBody } from "../test/fakeFetch";
import { Wrap, makeMe, resetSession } from "../test/harness";
import { ReservationsPane } from "./Reservations";

/* 预约记录这一屏守三个刻意的选择：
 *   ① 状态筛选在前端做 —— 换一次筛选不该多打一个请求；
 *   ② 改期走 PATCH，不是"取消再重约"（两者之间有时段被抢的窗口）；
 *   ③ 取消的请求体里没有身份字段（管理员代取消另有 as_user_id，
 *      普通用户传了就是 403）。 */

const ROWS = [
  { id: 1, equipment_id: 11, user_id: 2, user_name: "李娜", equipment_name: "荧光光谱仪", lab_label: "光谱实验室", date: "2026-11-02", start_time: "10:00", end_time: "12:00", status: "confirmed", purpose: "", slot: "2026-11-02 10:00-12:00", no_show_at: null, pardoned_at: null },
  { id: 2, equipment_id: 11, user_id: 2, user_name: "李娜", equipment_name: "荧光光谱仪", lab_label: "光谱实验室", date: "2026-11-03", start_time: "10:00", end_time: "12:00", status: "pending", purpose: "", slot: "2026-11-03 10:00-12:00", no_show_at: null, pardoned_at: null },
  { id: 3, equipment_id: 12, user_id: 2, user_name: "李娜", equipment_name: "紫外分光光度计", lab_label: "光谱实验室", date: "2026-11-04", start_time: "10:00", end_time: "12:00", status: "cancelled", purpose: "", slot: "2026-11-04 10:00-12:00", no_show_at: null, pardoned_at: null },
];

beforeEach(() => {
  resetSession();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mount(me = makeMe()) {
  const h = installFetch((url, init) => {
    if (url.startsWith("/api/reservations/1") && init.method === "PATCH") {
      return { json: { message: "已改期" } };
    }
    if (url.startsWith("/api/reservations/cancel")) return { json: { ok: true } };
    if (url.startsWith("/api/reservations")) return { json: ROWS };
    if (url.startsWith("/api/users")) return { json: [] };
    return { json: {} };
  });
  render(<Wrap me={me}><ReservationsPane /></Wrap>);
  return h;
}

describe("ReservationsPane", () => {
  it("状态筛选不发新请求，只是把已经拿到的行过滤一遍", async () => {
    const { calls } = mount();
    await screen.findByRole("table");
    const before = calls.filter((c) => c.url.startsWith("/api/reservations")).length;
    const user = userEvent.setup();
    await user.selectOptions(screen.getByDisplayValue("全部状态"), "pending");
    await waitFor(() => expect(screen.queryByText(/2026-11-02 10:00-12:00/)).toBeNull());
    expect(screen.getByText(/2026-11-03 10:00-12:00/)).toBeTruthy();
    const after = calls.filter((c) => c.url.startsWith("/api/reservations")).length;
    expect(after).toBe(before);
  });

  it("只有待确认和已确认的行才有操作按钮", async () => {
    mount();
    await screen.findByRole("table");
    // 三行里两行可操作，一行（已取消）没有 —— 8 个按钮 = 其它两行各两个 + 一个导出
    expect(screen.getAllByRole("button", { name: "改期" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "取消" })).toHaveLength(2);
  });

  it("改期走 PATCH，且请求体只带起始与结束", async () => {
    const { calls } = mount();
    await screen.findByRole("table");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "改期" })[0]);
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("textbox") as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "14:00-16:00");
    await user.click(within(dialog).getByRole("button", { name: "改期" }));

    await waitFor(() => {
      const patch = calls.find((c) => c.init.method === "PATCH");
      expect(patch).toBeTruthy();
      expect(patch!.url).toBe("/api/reservations/1");
      // 后端要 HH:MM:SS；界面上写 HH:MM 更自然，转换只在这一处发生
      expect(parseBody(patch!)).toEqual({ start: "14:00:00", end: "16:00:00" });
    });
    // 关键：没有先取消再重约
    expect(calls.some((c) => c.url.startsWith("/api/reservations/cancel"))).toBe(false);
  });

  it("格式不对就重新弹窗，并保留刚敲的内容", async () => {
    const { calls } = mount();
    await screen.findByRole("table");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "改期" })[0]);
    let dialog = await screen.findByRole("dialog");
    let input = within(dialog).getByRole("textbox") as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "下午两点");
    await user.click(within(dialog).getByRole("button", { name: "改期" }));
    // 原对话框关掉、又开一个新的，输入框里还是"下午两点"
    dialog = await screen.findByRole("dialog");
    input = within(dialog).getByRole("textbox") as HTMLInputElement;
    expect(input.value).toBe("下午两点");
    expect(calls.some((c) => c.init.method === "PATCH")).toBe(false);
  });

  it("取消的请求体里没有身份字段", async () => {
    const { calls } = mount();
    await screen.findByRole("table");
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "取消" })[0]);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "取消这条预约" }));
    await waitFor(() => {
      const call = calls.find((c) => c.url.startsWith("/api/reservations/cancel"));
      expect(call).toBeTruthy();
      const body = parseBody(call!)!;
      expect(Object.keys(body).sort()).toEqual(["reason", "reservation_id"]);
      expect(body.reservation_id).toBe(1);
    });
  });

  it("管理员看得到用户筛选，普通用户看不到", async () => {
    mount(makeMe({ capabilities: [] }));
    await screen.findByRole("table");
    expect(screen.queryByDisplayValue("全部用户")).toBeNull();
    cleanup();
    resetSession();
    vi.unstubAllGlobals();
    mount(makeMe({ role: "admin", capabilities: ["user.read.all"] }));
    await screen.findByRole("table");
    expect(screen.getByDisplayValue("全部用户")).toBeTruthy();
  });
});
