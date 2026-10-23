import { logout } from "../lib/api";
import { useApp } from "../store/app";
import { useHealth } from "../store/health";

/* 顶栏。四个徽章回答四个彼此不同的问题：
 *   我是谁（#badge-user）、系统现在什么状态（#badge-mode，点一下能看完整串）、
 *   检索走哪条路（#badge-backend）、服务端认为现在几点（#badge-now）。
 *
 * 四合一就没人看得懂；而"时段"这一项特别重要 —— 演示时会钉住 LAB_FAKE_NOW，
 * 浏览器本地时间和服务端时间能差好几天，看到「时段 14:32」才知道以谁为准。
 */

function Dot({ tone }: { tone: "ok" | "warn" | "bad" }) {
  const color = tone === "ok" ? "bg-ok-ink" : tone === "warn" ? "bg-warn-ink" : "bg-bad-ink";
  return <span className={"inline-block h-1.5 w-1.5 shrink-0 rounded-full " + color} />;
}

export function TopBar() {
  const { me, theme, toggleTheme, signOut } = useApp();
  const { health, status } = useHealth();

  const modeTone: "ok" | "warn" | "bad" =
    status === "ok" ? (health?.agent_available ? "ok" : "warn") : status === "fail" ? "bad" : "warn";

  const modeLabel =
    status === "anon"
      ? "未登录"
      : status === "expired"
        ? "登录已过期"
        : status === "fail"
          ? "连接失败"
          : status === "unknown"
            ? "连接中…"
            : (health?.app_mode ?? "—");

  const modeTitle = health
    ? "运行模式：" + health.app_mode + " · 数据库：" + health.database
    : "运行模式与数据库（悬停可看完整串）";

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-canvas/85 backdrop-blur">
      <div className="mx-auto flex h-[54px] max-w-[1440px] items-center gap-3 px-8">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-sm bg-action font-display text-xs text-action-ink">
            LB
          </span>
          <span className="leading-none">
            <b className="block text-base font-medium">lab-booking-agent</b>
            <small className="text-micro text-ink-3">智能实验室预约</small>
          </span>
        </div>

        <div className="flex-1" />

        <div className="flex flex-wrap items-center gap-2">
          {me ? (
            <span className="badge">
              <Dot tone="ok" />
              <b>{me.username}</b>
              <span className={"who-role" + (me.role !== "user" ? " admin" : "")}>{me.role}</span>
            </span>
          ) : null}

          <span className="badge" title={modeTitle}>
            <Dot tone={modeTone} />
            {modeLabel}
          </span>

          <span className="badge">
            检索 <b className="font-mono">{health?.retrieval_backend ?? "–"}</b>
            {health?.retrieval_degraded_reason ? "（降级）" : ""}
          </span>

          <span className="badge">
            时段 <b className="font-mono">{health ? health.now.slice(11, 16) : "–"}</b>
          </span>
        </div>

        {me ? (
          <button
            className="icon-btn"
            title="退出登录"
            aria-label="退出登录"
            onClick={() => {
              // 先请服务端作废令牌，再清本地 —— 只清本地只是"忘了它"。
              void logout().finally(() => signOut());
            }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-[15px] w-[15px]">
              <path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" />
              <path d="M16 17l5-5-5-5" />
              <path d="M21 12H9" />
            </svg>
          </button>
        ) : null}

        <button className="icon-btn" title="切换主题" aria-label="切换主题" onClick={toggleTheme}>
          {theme === "dark" ? (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-[15px] w-[15px]">
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="h-[15px] w-[15px]">
              <path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" />
            </svg>
          )}
        </button>
      </div>
    </header>
  );
}
