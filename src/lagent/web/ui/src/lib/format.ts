/* 小工具。刻意保持极短 —— 这里每多一个函数，就多一处"和浏览器原生
   行为不一致"的风险。 */

import type { ReservationStatus } from "./types";

export const STATUS_TAG: Record<ReservationStatus, string> = {
  pending: "warn",
  confirmed: "ok",
  cancelled: "",
  completed: "ok",
  expired: "bad",
};

export const STATUS_CN: Record<ReservationStatus, string> = {
  pending: "待确认",
  confirmed: "已确认",
  cancelled: "已取消",
  completed: "已完成",
  expired: "已过期",
};

export const NOTIFY_CN: Record<string, string> = {
  pending: "待投递",
  sent: "已投递",
  skipped: "已跳过",
  failed: "投递失败",
};

/* 门禁拒绝码 → 人话。16 条全部来自 api.py 的 reason_code。
   注意：**放行时 reason_code 是空串**，调用方必须先看 granted，
   不能拿这张表去查成功的情况（查不到会得到 undefined，再默认成 bad，
   于是一次成功放行被画成红色）。 */
export const GATE_REASON: Record<string, { text: string; tone: string }> = {
  ok: { text: "放行", tone: "ok" },
  no_permit: { text: "无有效凭证", tone: "bad" },
  permit_used: { text: "凭证已用（单次通行）", tone: "bad" },
  permit_revoked: { text: "凭证已撤销", tone: "bad" },
  permit_expired: { text: "凭证已过期", tone: "bad" },
  not_yet_valid: { text: "未到凭证生效时间", tone: "warn" },
  wrong_lab: { text: "凭证不属于这个实验室", tone: "bad" },
  identity_mismatch: { text: "身份与凭证不匹配", tone: "bad" },
  cert_missing: { text: "缺少准入资质", tone: "bad" },
  cert_expired: { text: "准入资质已过期", tone: "bad" },
  cert_revoked: { text: "准入资质已撤销", tone: "bad" },
  lab_full: { text: "房间已满", tone: "warn" },
  already_inside: { text: "已在馆内（重复进入）", tone: "warn" },
  lab_closed: { text: "不在开放时段", tone: "warn" },
  no_such_user: { text: "无此用户", tone: "bad" },
  concurrent_conflict: { text: "并发冲突（座位被抢）", tone: "warn" },
};

/** 本地日期，YYYY-MM-DD。
 *  刻意不用 toISOString()：它是 UTC，东八区早上 8 点前会退回**昨天**，
 *  于是"今天签发凭证"会签成昨天。 */
export function isoDate(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

/** 08:00–22:00，每 30 分钟一档。 */
export const BK_TIMES: string[] = (() => {
  const out: string[] = [];
  for (let m = 8 * 60; m <= 22 * 60; m += 30) {
    out.push(String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0"));
  }
  return out;
})();

/** 后端时间戳 → "MM-DD HH:MM"。后端给的是 ISO 字符串。 */
export function fmtStamp(value: string | null | undefined): string {
  if (!value) return "—";
  const t = new Date(value);
  if (Number.isNaN(t.getTime())) return String(value);
  const pad = (n: number) => String(n).padStart(2, "0");
  return pad(t.getMonth() + 1) + "-" + pad(t.getDate()) + " " + pad(t.getHours()) + ":" + pad(t.getMinutes());
}

export function fmtClock(value: string | null | undefined): string {
  if (!value) return "—";
  const t = new Date(value);
  if (Number.isNaN(t.getTime())) return String(value);
  const pad = (n: number) => String(n).padStart(2, "0");
  return pad(t.getHours()) + ":" + pad(t.getMinutes());
}
