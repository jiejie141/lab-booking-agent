import type { ReactNode } from "react";
import type { ApiError } from "../lib/api";

/* 面板共用的三种小块：骨架、空态、错误。
   错误块的规矩来自原来那次教训：**网络失败和 5xx 必须给一个重试按钮**，
   否则用户唯一的出路是刷新整个页面，而那会把对话也一起清掉。 */

export function Skel({ rows = 3, card = false }: { rows?: number; card?: boolean }) {
  const cls = card ? "skel sk-card" : "skel sk-row";
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className={cls} />
      ))}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrBox({ error, onRetry, what }: { error: ApiError; onRetry: () => void; what?: string }) {
  const canRetry = error.network || error.status >= 500;
  const raw = error.raw === undefined ? "" : JSON.stringify(error.raw);
  return (
    <div className="empty" title={raw || error.message}>
      <div>{what ? what + "失败：" : ""}{error.message}</div>
      {canRetry ? (
        <button className="btn btn-ghost btn-sm mt-2" onClick={onRetry}>
          重试
        </button>
      ) : null}
    </div>
  );
}

export function Tag({ tone, children }: { tone?: "" | "ok" | "warn" | "bad"; children: ReactNode }) {
  return <span className={"tag " + (tone || "")}>{children}</span>;
}

export function Note({ children }: { children: ReactNode }) {
  return (
    <div className="note">
      <span className="mr-1.5 inline-block align-[-2px] text-[13px]">i</span>
      <span>{children}</span>
    </div>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h3 className="mb-2 mt-4 text-sm font-medium text-ink-2">{children}</h3>;
}
