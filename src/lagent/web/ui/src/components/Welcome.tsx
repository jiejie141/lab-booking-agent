import { useHealth } from "../store/health";

/* 首页三块：欢迎区（含快捷入口）、四个读数、两栏主体。
   快捷入口和标签页数字键 1/2/3/4 指向同一批面板 —— 两条路都能进去，
   因为"从左边开始读"和"我已经知道我要看哪块"是两种不同的进入方式。 */

export function Welcome({ onJump }: { onJump: (tab: string) => void }) {
  return (
    <section className="mt-8 grid items-center gap-6 md:grid-cols-[minmax(0,1fr)_260px]">
      <div>
        <span className="inline-flex items-center gap-2 text-micro uppercase tracking-[0.14em] text-ink-3">
          <span className="live-dot inline-block h-1.5 w-1.5 rounded-full bg-ok-ink" />
          实验室预约工作台
        </span>
        <h1 className="mt-2.5 font-display text-[clamp(28px,3.2vw,40px)] leading-[1.15] tracking-[-0.01em]">
          安排下一场实验，<em className="not-italic text-accent">从这里开始。</em>
        </h1>
        <p className="mt-2 max-w-[52ch] text-md text-ink-2">
          直接预约设备，或让助手帮你寻找合适的时段。预约记录和处理进度随时可查。
        </p>
        <div className="mt-4 flex flex-wrap gap-2" aria-label="快捷操作">
          <button className="btn" onClick={() => onJump("book")}>
            直接预约 <span aria-hidden="true">→</span>
          </button>
          <button className="btn btn-ghost" onClick={() => onJump("labs")}>
            浏览设备
          </button>
          <button className="btn btn-ghost" onClick={() => onJump("res")}>
            我的预约
          </button>
        </div>
      </div>

      <div className="relative hidden h-[220px] place-items-center md:grid" aria-hidden="true">
        <div className="absolute h-[200px] w-[200px] rounded-full border border-dashed border-line" />
        <div className="absolute h-[132px] w-[132px] rounded-full border border-line-soft" />
        <div className="relative grid h-[74px] w-[74px] place-items-center rounded-full border border-line bg-surface text-center">
          <span className="font-display text-lg">LB</span>
          <small className="absolute bottom-2 text-[8px] tracking-[0.2em] text-ink-3">READY</small>
        </div>
        <span className="absolute left-2 top-6 text-micro text-ink-3">预约</span>
        <span className="absolute right-1 top-[100px] text-micro text-ink-3">资质</span>
        <span className="absolute bottom-3 left-8 text-micro text-ink-3">审计</span>
      </div>
    </section>
  );
}

const STAT_DEFS = [
  {
    key: "labs" as const,
    label: "实验室",
    title: undefined as string | undefined,
    icon: (
      <>
        <path d="M3 21h18" />
        <path d="M5 21V8l7-5 7 5v13" />
        <path d="M9.5 21v-5h5v5" />
      </>
    ),
  },
  {
    key: "equipment" as const,
    label: "设备",
    icon: (
      <>
        <path d="M9 3h6" />
        <path d="M10 3v6.5L5.2 18A2 2 0 007 21h10a2 2 0 001.8-3L14 9.5V3" />
        <path d="M7.5 15h9" />
      </>
    ),
  },
  {
    key: "active" as const,
    label: "生效中预约",
    // 这个数会比"已确认"大，看起来像 bug，所以把原因写在 title 里。
    title:
      "状态为 pending / confirmed / in_use 的预约。注意 pending（待审批）也算 —— 它还没被拒，仍然参与时段冲突判断。所以灌了待审批示例数据时，这个数会明显大于已确认条数。预约总数里其余的是已取消与已过期，它们不参与冲突判断。",
    icon: (
      <>
        <rect x="3" y="5" width="18" height="16" rx="2" />
        <path d="M8 3v4M16 3v4M3 10h18" />
        <path d="m9 15.5 2 2 4-4" />
      </>
    ),
  },
  {
    key: "reservations" as const,
    label: "预约总数",
    title:
      "库中全部预约记录（含已取消与已过期）。它比生效中预约大是正常的 —— 历史记录不删，是为了让审计与违约统计有据可查。",
    icon: (
      <>
        <path d="M3 6h18M3 12h18M3 18h18" />
        <circle cx="7" cy="6" r="1.6" fill="currentColor" stroke="none" />
        <circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none" />
        <circle cx="9" cy="18" r="1.6" fill="currentColor" stroke="none" />
      </>
    ),
  },
];

export function Stats() {
  const { health } = useHealth();
  return (
    <section className="mt-6 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-line bg-line lg:grid-cols-4" aria-label="概览">
      {STAT_DEFS.map((s) => (
        <div key={s.key} className="bg-surface px-4 py-3.5" title={s.title}>
          <div className="flex items-center justify-between">
            <span className="text-micro uppercase tracking-[0.08em] text-ink-3">{s.label}</span>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" className="h-[15px] w-[15px] text-ink-3">
              {s.icon}
            </svg>
          </div>
          <div className="mt-1 font-mono text-xl tabular-nums">
            {health ? health.counts[s.key] : "–"}
          </div>
        </div>
      ))}
    </section>
  );
}
