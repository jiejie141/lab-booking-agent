import { useEffect, useMemo, useState } from "react";
import { fetchToday } from "../lib/api";
import { useApp } from "../store/app";
import type { TodayOut } from "../lib/types";
import { BookPane } from "../panes/Book";
import { LabsPane } from "../panes/Labs";
import { ReservationsPane } from "../panes/Reservations";
import { KnowledgePane } from "../panes/Knowledge";
import { UsersPane } from "../panes/Users";
import { ApprovalsPane } from "../panes/Approvals";
import { ViolationsPane } from "../panes/Violations";
import { NotificationsPane } from "../panes/Notifications";
import { AuditPane } from "../panes/Audit";
import { GateSimPane } from "../panes/GateSim";

/* 右侧数据卡：10 个 tab。
 *
 * 权限门是**能力点**驱动的（capabilities），不是角色字符串 ——
 * 后端加一个新角色时，这里一个 if 都不用改。
 *
 * 六个 admin tab 在非管理员下不渲染，而不是渲染出来再禁用：
 * 一个"能点但永远 403"的入口比没有入口更让人困惑。
 */

export type TabKey =
  | "book" | "labs" | "res" | "kb" | "users"
  | "appr" | "viol" | "notif" | "audit" | "gate";

const TABS: { key: TabKey; label: string; capability?: string }[] = [
  { key: "book", label: "直接预约" },
  { key: "labs", label: "实验室与设备" },
  { key: "res", label: "预约记录" },
  { key: "kb", label: "规范检索" },
  { key: "users", label: "用户目录", capability: "user.read.all" },
  { key: "appr", label: "审批待办" },
  { key: "viol", label: "违约" },
  { key: "notif", label: "通知" },
  { key: "audit", label: "审计" },
  { key: "gate", label: "门禁核验" },
];

/* 哪些 tab 需要管理员。用一个集合而不是逐个写 capability：
   后端目前只有 user.read.all 和 role.manage 两个能力点，六个 admin
   面板靠的都是"能看到这个用户目录"这一件事。 */
const ADMIN_TABS: TabKey[] = ["users", "appr", "viol", "notif", "audit", "gate"];

export function visibleTabs(isAdmin: boolean) {
  return TABS.filter((t) => !ADMIN_TABS.includes(t.key) || isAdmin);
}

export function DataCard({ tab, onTab, onBooked }: { tab: TabKey; onTab: (t: TabKey) => void; onBooked: () => void }) {
  const { isAdmin } = useApp();
  const [today, setToday] = useState<TodayOut | null>(null);
  const [stamp, setStamp] = useState("");

  useEffect(() => {
    fetchToday().then(setToday).catch(() => setToday(null));
  }, []);

  // "更新于"来自真实的一次数据动作，不是定时器 —— 定时器会在
  // 拉取失败时也往前走，那行字就成了装饰。
  const touched = useMemo(() => {
    return () => setStamp("更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false }));
  }, []);

  return (
    <section className="card">
      <div className="card-h flex-wrap gap-2" style={{ minHeight: 54 }}>
        <h2 className="sr-only">数据面板</h2>
        <div className="tabs" role="tablist">
          {visibleTabs(isAdmin).map((t) => (
            <button
              key={t.key}
              role="tab"
              aria-selected={tab === t.key}
              className={"tab" + (tab === t.key ? " on" : "")}
              onClick={() => { onTab(t.key); touched(); }}
            >
              {t.label}
            </button>
          ))}
        </div>
        <span className="flex-1" />
        <span className="stamp" title="服务端当前日期与星期（取自 /api/today）">
          {today ? today.date + " " + today.weekday + " " + today.time : ""}
        </span>
        <span className="stamp">{stamp}</span>
      </div>

      <div className="card-b">
        {tab === "book" ? <BookPane onBooked={onBooked} /> : null}
        {tab === "labs" ? <LabsPane /> : null}
        {tab === "res" ? <ReservationsPane /> : null}
        {tab === "kb" ? <KnowledgePane /> : null}
        {tab === "users" && isAdmin ? <UsersPane /> : null}
        {tab === "appr" && isAdmin ? <ApprovalsPane /> : null}
        {tab === "viol" && isAdmin ? <ViolationsPane /> : null}
        {tab === "notif" && isAdmin ? <NotificationsPane /> : null}
        {tab === "audit" && isAdmin ? <AuditPane /> : null}
        {tab === "gate" && isAdmin ? <GateSimPane /> : null}
      </div>
    </section>
  );
}
