import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

export interface DialogOptions {
  title: string;
  body?: string;
  /* 传了就是 prompt（需要输入），不传就是 confirm */
  input?: boolean;
  value?: string;
  placeholder?: string;
  okText?: string;
  cancelText?: string;
  danger?: boolean;
}

type DialogResult = boolean | string | null;

interface Pending {
  options: DialogOptions;
  resolve: (value: DialogResult) => void;
}

interface DialogApi {
  confirm: (title: string, body?: string, extra?: Partial<DialogOptions>) => Promise<boolean>;
  prompt: (title: string, value?: string, extra?: Partial<DialogOptions>) => Promise<string | null>;
}

const DialogContext = createContext<DialogApi | null>(null);

export function useDialog(): DialogApi {
  const ctx = useContext(DialogContext);
  if (!ctx) throw new Error("useDialog 必须在 DialogHost 内使用");
  return ctx;
}

/* 一个原语，两种用法。
 *
 * 不用 window.confirm / prompt 的三个理由写在 Toaster 里（阻塞、外观割裂、
 * Playwright 默认自动取消）。这里补第四条：prompt 的"取消"必须和"留空"
 * 区分开 —— 前者是"我不想做这件事"，后者是"这一项我就是空着"。
 * 原生 prompt 两者都返回 null，调用方只能靠猜。
 */
export function DialogHost({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const yesRef = useRef<HTMLButtonElement | null>(null);
  const settled = useRef(false);

  const open = useCallback((options: DialogOptions) => {
    return new Promise<DialogResult>((resolve) => {
      settled.current = false;
      setPending({ options, resolve });
    });
  }, []);

  const close = useCallback((value: DialogResult) => {
    if (settled.current) return;
    settled.current = true;
    setPending((cur) => {
      cur?.resolve(value);
      return null;
    });
  }, []);

  const api: DialogApi = {
    confirm: (title, body, extra) =>
      open({ title, body, ...extra }).then((v) => v === true),
    prompt: (title, value, extra) =>
      open({ title, input: true, value: value ?? "", ...extra }).then((v) =>
        typeof v === "string" ? v : null,
      ),
  };

  useEffect(() => {
    if (!pending) return;
    const t = window.setTimeout(() => {
      if (pending.options.input) {
        inputRef.current?.focus();
        inputRef.current?.select();
      } else {
        yesRef.current?.focus();
      }
    }, 0);

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close(pending.options.input ? null : false);
      } else if (e.key === "Enter") {
        // prompt 里回车 = 提交输入框里的内容；confirm 里回车 = 确认
        if (pending.options.input) {
          e.preventDefault();
          close(inputRef.current?.value ?? "");
        }
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      window.clearTimeout(t);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [pending, close]);

  return (
    <DialogContext.Provider value={api}>
      {children}
      {pending ? (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/35 p-6"
          onMouseDown={(e) => {
            // 只有点在遮罩本身才关闭。点在对话框上拖选文字时松开鼠标
            // 会落到遮罩上 —— 不判断 target 就会把用户正在读的东西关掉。
            if (e.target === e.currentTarget) close(pending.options.input ? null : false);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label={pending.options.title}
            className="w-full max-w-[420px] rounded-lg border border-line bg-surface p-5 shadow-2xl"
          >
            <div className="text-[15px] font-medium">{pending.options.title}</div>
            {pending.options.body ? (
              <div className="mt-1.5 text-[12px] text-ink-2">{pending.options.body}</div>
            ) : null}
            {pending.options.input ? (
              <input
                ref={inputRef}
                defaultValue={pending.options.value ?? ""}
                placeholder={pending.options.placeholder}
                className="fld mt-3 w-full"
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    close(inputRef.current?.value ?? "");
                  }
                }}
              />
            ) : null}
            <div className="mt-4 flex justify-end gap-2">
              <button className="btn btn-ghost" onClick={() => close(pending.options.input ? null : false)}>
                {pending.options.cancelText ?? "取消"}
              </button>
              <button
                ref={yesRef}
                className={"btn" + (pending.options.danger ? " btn-danger" : "")}
                onClick={() => close(pending.options.input ? (inputRef.current?.value ?? "") : true)}
              >
                {pending.options.okText ?? "确定"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </DialogContext.Provider>
  );
}
