import { useEffect, useRef, useState } from "react";

/* 登录页左侧那块交互演示：三步讲完"约束协商"是什么。
 *
 * 为什么放在登录页：这是整个项目唯一一处能在**登录之前**说明
 * 「你和普通表单系统的区别在哪」的地方。登录之后有的是面板可以看，
 * 而登录之前用户只有一个决定要做 —— 要不要进去。
 */

const STEPS = [
  {
    title: "先说说，你的计划",
    detail: "“我想用光谱仪，下午两点开始，使用两小时。”",
  },
  {
    title: "这个时间，已经有安排",
    detail: "14:00—16:00 已被占用。直接拒绝，故事就到这里为止。",
  },
  {
    title: "换个时间，研究照常继续",
    detail: "16:00—18:00 仅调整开始时间 —— 这就是它给出的备选。",
  },
];

const REDUCED =
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function BookingStory() {
  const [step, setStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (!playing) return;
    timer.current = window.setTimeout(() => {
      setStep((cur) => {
        if (cur >= STEPS.length - 1) {
          setPlaying(false);
          return cur;
        }
        return cur + 1;
      });
    }, 1700);
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, [playing, step]);

  // 切到后台就把自动播放停掉：回来时看到的是停在原地的演示，
  // 而不是"不知道已经播到第几步"的一段动画。
  useEffect(() => {
    const onHide = () => {
      if (document.hidden) setPlaying(false);
    };
    document.addEventListener("visibilitychange", onHide);
    return () => document.removeEventListener("visibilitychange", onHide);
  }, []);

  const copy = STEPS[step];

  return (
    <section className="mt-8 max-w-[440px] rounded-lg border border-line bg-surface/70 p-4" aria-label="交互预约演示">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-medium text-ink-2">一次预约，三步安排</span>
        <small className="text-micro text-ink-3">交互示例 · 不创建真实预约</small>
      </div>

      <div className="mt-3 flex items-center gap-2.5">
        <span className="flex h-8 w-8 items-center justify-center rounded-md bg-[#e7efe6] text-[15px] text-[#23694f]">
          ↗
        </span>
        <div className="leading-tight">
          <b className="block text-base">荧光光谱仪</b>
          <small className="text-xs text-ink-3">分析实验室 / 02</small>
        </div>
        <span className="ml-auto text-micro text-ink-3">保留设备</span>
      </div>

      <div className="mt-2 flex justify-between text-micro text-ink-3">
        <span>14:00</span>
        <span>16:00</span>
        <span>18:00</span>
      </div>

      <div className="relative mt-1 flex h-[80px] gap-1 overflow-hidden rounded-md bg-canvas-2 p-1">
        <div className="flex-1 rounded-sm bg-[#e2e2de] text-center text-micro leading-[68px] text-ink-3">
          已有安排
        </div>
        <div className="flex-1 rounded-sm bg-[#e7efe6] text-center text-micro leading-[68px] text-[#23694f]">
          空闲时段
        </div>
        <div
          className="absolute left-1 top-[39px] w-[calc(50%-8px)] rounded-sm px-2 py-1 text-center text-micro text-white transition-transform duration-300"
          style={{
            background: step === 2 ? "#23775b" : "#b77749",
            transform: step === 2 ? "translateX(calc(100% + 8px))" : undefined,
          }}
        >
          你的实验 · 2 小时
        </div>
      </div>

      <div className="mt-3 min-h-[77px]" aria-live="polite">
        <b className="block text-base">{copy.title}</b>
        <p className="mt-0.5 text-sm text-ink-3">{copy.detail}</p>
      </div>

      <div className="mt-1 flex items-center justify-between">
        <div className="flex gap-1" aria-label="演示步骤">
          {STEPS.map((s, i) => (
            <button
              key={s.title}
              type="button"
              className={
                "rounded-sm px-2 py-0.5 text-micro transition-colors " +
                (i === step ? "bg-surface-3 text-ink" : "text-ink-3 hover:text-ink-2")
              }
              aria-pressed={i === step}
              onClick={() => {
                setPlaying(false);
                setStep(i);
              }}
            >
              0{i + 1} {["需求", "冲突", "备选"][i]}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="text-sm text-ink-2 hover:text-ink"
          onClick={() => {
            setPlaying((p) => !p);
            if (REDUCED) setStep(2);
          }}
        >
          {playing ? "暂停演示" : "播放演示 ↗"}
        </button>
      </div>
    </section>
  );
}
