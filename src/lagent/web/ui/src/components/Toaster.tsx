import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

type Tone = "ok" | "err" | null;

interface ToastItem {
  id: number;
  message: string;
  tone: Tone;
  leaving: boolean;
}

interface ToastApi {
  toast: (message: string, tone?: Tone) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast 必须在 Toaster 内使用");
  return ctx;
}

/* 提示条。用自己的一层而不是浏览器的 alert：
 *  ① alert 会阻塞渲染线程（流式对话时会把动画冻住）；
 *  ② 系统弹窗的外观和这套深浅色主题完全割裂；
 *  ③ Playwright 默认会自动取消所有原生弹窗 —— 曾因此把一个
 *     "按钮没绑事件"的 bug 误判成真的。
 * 同一个理由适用于下面的对话框（Confirm/Prompt）。
 */
export function Toaster({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const toast = useCallback((message: string, tone: Tone = null) => {
    const id = ++seq.current;
    setItems((prev) => [...prev, { id, message, tone, leaving: false }]);
    window.setTimeout(() => {
      setItems((prev) => prev.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
      window.setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 260);
    }, 3400);
  }, []);

  const api = useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="fixed right-7 bottom-7 z-50 flex flex-col gap-2" role="status" aria-live="polite">
        {items.map((t) => (
          <div
            key={t.id}
            className={
              "min-w-[220px] max-w-[380px] rounded-md border border-line bg-surface px-3.5 py-2.5 text-[12px] shadow-lg transition-all duration-200 " +
              (t.leaving ? "translate-y-1 opacity-0 " : "opacity-100 ") +
              (t.tone === "err"
                ? "border-l-2 border-l-bad-ink"
                : t.tone === "ok"
                  ? "border-l-2 border-l-ok-ink"
                  : "")
            }
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
