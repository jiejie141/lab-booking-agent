import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { installFetch } from "../test/fakeFetch";
import { Wrap, adminMe, makeMe } from "../test/harness";
import { DataCard, visibleTabs } from "./DataCard";

/* 这组守的是权限门的形态：非管理员**看不到**那六个入口，
   而不是"看得到、点了 403"。一个永远失败的按钮比没有按钮更糟。 */

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

async function renderCard(me: ReturnType<typeof makeMe>, tab: "labs" | "users" = "labs") {
  installFetch((url) => {
    if (url.startsWith("/api/today")) {
      return { json: { date: "2026-11-01", weekday: "周日", time: "10:00", tomorrow: "2026-11-02" } };
    }
    return { json: [] };
  });
  const out = render(
    <Wrap me={me}>
      <DataCard tab={tab} onTab={() => {}} onBooked={() => {}} />
    </Wrap>,
  );
  // /api/today 是挂载后异步回来的；先把那一次状态更新排干，
  // 否则它会落在测试结束之后，冒出一条 act() 警告。
  await act(async () => {
    await Promise.resolve();
  });
  return out;
}

describe("visibleTabs", () => {
  it("普通用户只有四个 tab", () => {
    expect(visibleTabs(false).map((t) => t.key)).toEqual(["book", "labs", "res", "kb"]);
  });

  it("管理员十个 tab 全在，且顺序固定", () => {
    expect(visibleTabs(true).map((t) => t.key)).toEqual([
      "book", "labs", "res", "kb", "users", "appr", "viol", "notif", "audit", "gate",
    ]);
  });
});

describe("DataCard", () => {
  it("普通用户看到 4 个 tab，管理员看到 10 个", async () => {
    await renderCard(makeMe());
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    cleanup();
    await renderCard(adminMe());
    expect(screen.getAllByRole("tab")).toHaveLength(10);
  });

  it("选中的 tab 带 aria-selected", async () => {
    await renderCard(makeMe(), "labs");
    const tabs = screen.getAllByRole("tab");
    expect(tabs.find((t) => t.textContent === "实验室与设备")!.getAttribute("aria-selected")).toBe("true");
    expect(tabs.find((t) => t.textContent === "直接预约")!.getAttribute("aria-selected")).toBe("false");
  });

  it("普通用户即使被塞进管理员的 tab，也只画出一张空面板", async () => {
    // 防御的是"状态里残留了上次的身份"这种情形：光靠不渲染按钮不够，
    // 面板本身也要再判一次权限。
    const { container } = await renderCard(makeMe(), "users");
    expect(container.textContent).not.toContain("用户目录");
    expect(screen.queryByText(/当前身份无权查看/)).toBeNull();
  });

  it("每个 tab 带 id / data-tab，面板带 id=pane-xxx（与旧控制台同名）", async () => {
    // 这组 id 不是装饰：旧控制台的锚点、外部文档里的深链接、
    // 以及 pytest 里对 #pane-book 的定位都靠它。React 重写时漏掉了
    // 一次——页面看着完全正常，只有从链接进来的人会发现点不到。
    const { container } = await renderCard(adminMe(), "book");
    const tab = screen.getByRole("tab", { name: "直接预约" });
    expect(tab.id).toBe("tab-book");
    expect(tab.getAttribute("data-tab")).toBe("book");
    const pane = container.querySelector("#pane-book");
    expect(pane).not.toBeNull();
    expect(pane!.getAttribute("role")).toBe("tabpanel");
    expect(pane!.getAttribute("aria-labelledby")).toBe("tab-book");
    // aria-controls 只挂在选中的那个 tab 上。未选中的 tab 没有对应面板
    // 在 DOM 里，指向一个不存在的 id 读屏软件会读出空引用。
    expect(tab.getAttribute("aria-controls")).toBe("pane-book");
    for (const other of screen.getAllByRole("tab")) {
      if (other.id === "tab-book") continue;
      expect(other.getAttribute("aria-controls")).toBeNull();
    }
  });

  it("顶栏那行日期来自 /api/today，不是本地时钟", async () => {
    await renderCard(makeMe());
    const stamp = screen.getByTitle(/服务端当前日期与星期/);
    await waitFor(() => expect(stamp.textContent).toContain("2026-11-01"));
    expect(stamp.textContent).toContain("周日");
  });
});
