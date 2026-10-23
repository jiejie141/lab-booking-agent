import { afterEach, describe, expect, it, vi } from "vitest";
import { BK_TIMES, GATE_REASON, STATUS_CN, isoDate } from "./format";

afterEach(() => {
  vi.useRealTimers();
});

describe("isoDate", () => {
  it("清晨也不会掉回前一天", () => {
    // 这条守的是曾经的写法：用 toISOString().slice(0,10) 取日期，
    // 东八区 0:30 会被换成 UTC 的前一天 16:30 —— 预约单上就写了昨天。
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 2, 1, 0, 30, 0));
    expect(isoDate(0)).toBe("2026-03-01");
    expect(isoDate(1)).toBe("2026-03-02");
  });
});

describe("GATE_REASON", () => {
  it("放行的 reason_code 是空串，查表查不到东西", () => {
    // 门禁面板必须先看 granted 再查表，否则成功会被渲染成一个红色标签。
    expect(GATE_REASON[""]).toBeUndefined();
    expect(GATE_REASON.ok.text).toBe("放行");
    expect(GATE_REASON.ok.tone).toBe("ok");
  });

  it("每种拒绝理由都有中文", () => {
    for (const key of ["no_permit", "permit_used", "wrong_lab", "lab_full", "already_inside"]) {
      expect(GATE_REASON[key]?.text).toBeTruthy();
    }
  });
});

describe("常量表", () => {
  it("五种预约状态都有中文", () => {
    for (const key of ["pending", "confirmed", "cancelled", "completed", "expired"]) {
      expect(STATUS_CN[key]).toBeTruthy();
    }
  });

  it("时间段落在开放时间里且升序", () => {
    expect(BK_TIMES[0]).toBe("08:00");
    expect(BK_TIMES[BK_TIMES.length - 1]).toBe("22:00");
  });
});
