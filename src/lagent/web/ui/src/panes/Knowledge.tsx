import { useState } from "react";
import { apiGet } from "../lib/api";
import type { RetrieveOut, ToolOut } from "../lib/types";
import { ErrBox, Skel } from "../components/bits";

/* 规范检索 + Agent 工具清单。
 *
 * 两块放在同一个面板里，因为它们回答的是同一个问题：
 *   「系统到底知道什么、能动什么」。
 * 检索看到的是"规范里写了什么"，工具清单看到的是"Agent 能动手做什么"。
 * 之前后端有 /api/tools，前端一直没接 —— 那块能力在界面上等于不存在。
 */

export function KnowledgePane() {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [result, setResult] = useState<RetrieveOut | null>(null);

  const [tools, setTools] = useState<ToolOut[] | null>(null);
  const [toolsBusy, setToolsBusy] = useState(false);

  const run = async () => {
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const { data } = await apiGet<RetrieveOut>("/api/retrieve?q=" + encodeURIComponent(q) + "&k=4");
      setResult(data);
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  };

  const toggleTools = async () => {
    if (tools) {
      setTools(null);
      return;
    }
    setToolsBusy(true);
    try {
      const { data } = await apiGet<{ tools: ToolOut[] }>("/api/tools");
      setTools(data.tools);
    } catch {
      setTools(null);
    } finally {
      setToolsBusy(false);
    }
  };

  return (
    <div>
      <div className="flex gap-2">
        <input
          className="fld flex-1"
          placeholder="例如：离心机 配平 / 准入资质 培训"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void run(); }}
        />
        <button className="btn" disabled={busy} onClick={() => void run()}>{busy ? "检索中…" : "检索"}</button>
      </div>
      <div className="hint mt-1.5">检索《实验室准入与设备使用规范》，命中会标注由哪几路召回。</div>

      {busy ? <div className="mt-3"><Skel rows={2} /></div> : null}
      {error ? <div className="mt-3"><ErrBox error={error as never} onRetry={() => void run()} what="检索" /></div> : null}

      {result ? (
        <div className="mt-3">
          <div className="hint mb-2">
            后端 <b className="mono">{result.backend}</b> · 语料 {result.stats.chunks} 条
            {result.degraded_reason ? "（降级：" + result.degraded_reason + "）" : ""}
          </div>
          {result.hits.length === 0 ? (
            <div className="empty">没有命中</div>
          ) : (
            <div className="flex flex-col gap-2.5">
              {result.hits.map((h, i) => (
                <div key={i} className="border-l-2 border-line-soft pl-2.5">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    {h.heading}
                    {h.matched_by?.length ? <span className="tag">{h.matched_by.join("+")}</span> : null}
                  </div>
                  <div className="text-sm text-ink-3">
                    {h.text.length > 150 ? h.text.slice(0, 150) + "…" : h.text}
                  </div>
                  <div className="mono text-micro text-ink-3">score {h.score.toFixed(3)}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}

      <div className="mt-4 border-t border-line-soft pt-3">
        <button className="btn btn-ghost btn-sm" disabled={toolsBusy} onClick={() => void toggleTools()}>
          {tools ? "收起 Agent 工具清单" : toolsBusy ? "加载中…" : "显示 Agent 工具清单"}
        </button>
        {tools ? (
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {tools.map((t) => (
              <div key={t.name} className="rounded-md border border-line bg-surface-2 p-2.5">
                <div className="flex items-center gap-2">
                  <b className="mono text-sm">{t.name}</b>
                  <span className={"tag " + (t.side_effect ? "warn" : "")}>{t.side_effect ? "写操作" : "只读"}</span>
                </div>
                <div className="mt-1 text-sm text-ink-3">{t.description}</div>
                <div className="mono mt-1 text-micro text-ink-3">
                  参数：{Object.keys(t.params || {}).join("、") || "（无）"}
                </div>
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
