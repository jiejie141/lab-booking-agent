import { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../store/app";
import { useToast } from "./Toaster";
import { useDialog } from "./Dialog";
import { chatStream, NODE_LABEL } from "../lib/chat";
import type { NodeEvent } from "../lib/chat";
import type { BookingOutcome, ChatResponse, DocHit, Proposal, TraceStep } from "../lib/types";
import { mdInline } from "../lib/markdown";

/* 左侧的对话协商面板。
 *
 * 这一块的复杂度集中在"一条回复有五种东西要显示"：
 *   正文（逐字打出来）、下单结果、备选方案、引用、执行轨迹。
 * 它们**不能同时出现** —— 正文打到一半就挂上结构化块，读的人会先看到
 * 结论再看问题；所以正文出完才一次性挂尾部。
 */

const QUICK = [
  "我想约个设备",
  "明天上午十点到十一点，紫外可见分光光度计",
  "离心机使用有什么安全规范",
  "张伟能约离心机吗",
];

type Role = "u" | "a";

interface Msg {
  id: number;
  role: Role;
  /* 正文（已转义的 HTML）。流式过程中是纯文本，收尾时才按 markdown 渲染。 */
  text: string;
  /* 正文后面挂的结构化块（下单结果 / 备选 / 引用 / 轨迹）。 */
  tail?: React.ReactNode;
  kind?: "err";
  /* 逐字动画的进度；undefined 表示不需要（用户消息、纯文本提示）。 */
  typing?: boolean;
}

const REDUCED =
  typeof window !== "undefined" &&
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function Chat({ onBooked }: { onBooked: () => void }) {
  const { me, sessionId, newSession } = useApp();
  const { toast } = useToast();
  const dialog = useDialog();

  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [node, setNode] = useState<{ label: string; ms: number } | null>(null);

  const seq = useRef(0);
  const boxRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const endedRef = useRef(false);

  const push = useCallback((m: Omit<Msg, "id">) => {
    const id = ++seq.current;
    setMsgs((prev) => [...prev, { ...m, id }]);
    return id;
  }, []);

  // 首条欢迎语。放在 state 初始化里而不是 HTML 里，是为了让它跟着会话一起
  // 被"新会话"清掉 —— 否则新会话里会留着上一轮的自我介绍。
  useEffect(() => {
    setMsgs([
      {
        id: ++seq.current,
        role: "a",
        text:
          "我是实验室预约助手。\n\n直接说需求即可，例如「明天下午两点想用荧光光谱仪两小时」。\n" +
          "遇到时段冲突我会给出**带取舍说明**的备选方案，而不是只说「约不上」。",
      },
    ]);
  }, []);

  const scroll = () => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  useEffect(scroll, [msgs, node]);

  const setBusyBoth = (on: boolean) => {
    setBusy(on);
    // 顶栏扫描线只在真的在工作时出现。
    document.body.classList.toggle("busy", on);
  };

  const stop = () => {
    // 不是把按钮禁掉 —— 那样请求还在跑、配额还在消耗。
    abortRef.current?.abort();
  };

  const send = useCallback(
    async (raw: string, accept?: { equipmentId: number; date: string; start: string; end: string }) => {
      if (busy) return;
      const text = raw.trim();
      if (!text && !accept) {
        toast("说点什么再发");
        return;
      }

      setBusyBoth(true);
      setInput("");
      setNode(null);
      if (text) push({ role: "u", text });

      // 占位气泡：显示节点进度，收到终态后整体替换。
      const phId = push({ role: "a", text: "", typing: true });

      const ctl = new AbortController();
      abortRef.current = ctl;
      endedRef.current = false;

      try {
        const payload: Record<string, unknown> = {
          message: text || "就选这个",
          session_id: sessionId,
        };
        if (accept) {
          payload.accept_equipment_id = accept.equipmentId;
          payload.accept_date = accept.date;
          payload.accept_start = accept.start;
          payload.accept_end = accept.end;
        }

        const res = await chatStream(payload as never, (ev: NodeEvent) => {
          setNode({ label: NODE_LABEL[ev.node] ?? ev.node ?? "处理中…", ms: ev.elapsed_ms ?? 0 });
        }, ctl.signal);

        setNode(null);
        await renderFinal(res, phId);
        if (res.booking?.ok) onBooked();
      } catch (e) {
        setNode(null);
        const err = e as Error;
        setMsgs((prev) => prev.filter((m) => m.id !== phId));
        if (err.name === "AbortError" || ctl.signal.aborted) {
          push({
            role: "a",
            kind: "err",
            text: "已停止生成。你可以换个说法再问一次，或者直接用上面的表单下单。",
          });
        } else {
          push({ role: "a", kind: "err", text: "出错了：" + err.message });
          toast(err.message, "err");
        }
      } finally {
        setBusyBoth(false);
        abortRef.current = null;
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [busy, sessionId, onBooked, toast, push],
  );

  /* 终态渲染。正文先打出来，打完再挂结构化块 —— 顺序反过来，
     读的人会先看到"已下单"再看"你说的是什么"。 */
  const renderFinal = async (res: ChatResponse, phId: number) => {
    const tail = buildTail(res);
    const reply = String(res.reply ?? "");

    if (REDUCED) {
      setMsgs((prev) => prev.map((m) => (m.id === phId ? { ...m, text: reply, tail, typing: false } : m)));
      return;
    }

    // 逐字：总时长封顶 1100ms，长回复不会变成看它打字等半分钟。
    const chars = Array.from(reply);
    const dur = Math.min(1100, 140 + chars.length * 11);
    const started = performance.now();

    await new Promise<void>((resolve) => {
      const tick = () => {
        const p = Math.min(1, (performance.now() - started) / dur);
        const eased = p * (2 - p);
        const n = Math.round(eased * chars.length);
        // 打字过程中只写 textContent 等价的纯文本（不带标签）：
        // 未闭合的星号会闪出来，而且每帧重新转义是白费。
        setMsgs((prev) =>
          prev.map((m) => (m.id === phId ? { ...m, text: chars.slice(0, n).join(""), typing: p < 1 } : m)),
        );
        if (p >= 1) resolve();
        else requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    });

    setMsgs((prev) => prev.map((m) => (m.id === phId ? { ...m, text: reply, tail, typing: false } : m)));
  };

  const reset = () => {
    if (busy) {
      toast("请先停止生成，再开启新会话");
      return;
    }
    newSession();
    setMsgs([]);
    push({ role: "a", text: "已开新会话。上一轮的备选方案不会再被引用。" });
  };

  const tooLong = input.length > 2000;

  // 高度上限按视口算，别让对话卡跟着右侧长列表一起长高 ——
  // 实测 900px 视口下，聊天卡被撑到 950px+，composer（含快捷示例
  // 和发送按钮）直接被推出屏幕。一个把主操作推出视口的布局，单看再整齐也是错的。
  return (
      <section
        className="card flex flex-col"
        style={{ minHeight: 560, maxHeight: "calc(100dvh - 260px)" }}
      >
      <div className="card-h">
        <h2 className="text-sm font-medium text-ink-2">对话协商</h2>
        <span className="badge">{busy ? "推理中" : "Agent 在线"}</span>
        <div className="flex-1" />
        <button className="btn btn-ghost btn-sm" onClick={reset}>
          新会话
        </button>
      </div>

      <div className="card-b flex min-h-0 flex-1 flex-col">
        <div className="mb-2 text-sm text-ink-3">
          {me ? (
            <span>
              以 <b className="text-ink-2">{me.username}</b> 的身份对话（身份取自令牌，不由前端指定）
              {me.certs.length ? " · 准入资质：" + me.certs.join("、") : " · 准入资质：无"}
            </span>
          ) : null}
        </div>

        <div ref={boxRef} className="msgs min-h-[220px] flex-1 overflow-y-auto pr-1" style={{ maxHeight: "min(60vh, 560px)" }}>
          {msgs.map((m) => (
            <div key={m.id} className={"msg " + m.role + (m.kind === "err" ? " err" : "")}>
              <div className="who">{m.role === "u" ? "我" : "AI"}</div>
              <div className="bub">
                {m.typing && !m.text ? (
                  <span className="dots">
                    <i /><i /><i />
                  </span>
                ) : null}
                {m.typing ? m.text : <span dangerouslySetInnerHTML={{ __html: mdInline(m.text) }} />}
                {m.tail}
              </div>
            </div>
          ))}
          {node ? (
            <div className="msg a">
              <div className="who">AI</div>
              <div className="bub text-ink-3">
                <span className="dots"><i /><i /><i /></span>
                {" "}
                {node.label}
                {node.ms ? " · " + node.ms + " ms" : ""}
              </div>
            </div>
          ) : null}
        </div>

        <div className="composer">
          <div className="mb-1 flex items-baseline justify-between">
            <label className="fld-label" htmlFor="inp-msg">你来说</label>
            <div className="hint text-right">
              {tooLong ? "超出长度上限（" + input.length + "/2000）" : "Ctrl + Enter 发送"}
            </div>
          </div>

          {/* 快捷示例**必须在输入框上方**。原来在底部，实测 top=921px 而
              视口只有 900px —— 新用户第一眼只看到一个空输入框，
              而示例恰恰是"不知道该问什么"时唯一的入口。 */}
          <div className="mb-2 flex flex-wrap gap-2">
            {QUICK.map((q) => (
              <button
                key={q}
                className="btn btn-ghost btn-sm"
                type="button"
                onClick={() => {
                  setInput(q);
                  void send(q);
                }}
              >
                {q}
              </button>
            ))}
          </div>

          <textarea
            id="inp-msg"
            className="fld w-full"
            placeholder="例如：明天下午两点想用荧光光谱仪两小时"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                if (!tooLong) void send(input);
              }
            }}
          />

          <div className="mt-2 flex gap-2">
            <button className="btn" disabled={tooLong || busy} onClick={() => void send(input)}>
              发送
            </button>
            {busy ? (
              <button className="btn btn-ghost" onClick={stop}>
                停止生成
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );

  function buildTail(res: ChatResponse): React.ReactNode | undefined {
    const parts: React.ReactNode[] = [];
    const booking: BookingOutcome | undefined = res.booking ?? undefined;

    if (booking) {
      const ok = booking.ok && !!booking.reservation;
      parts.push(
        <div key="bk" className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <span className={"tag " + (ok ? "ok" : "bad")}>{ok ? "已下单" : "下单失败"}</span>
          {ok && booking.reservation ? (
            <span className="mono text-ink-2">
              #{booking.reservation.id} · {booking.reservation.slot}
            </span>
          ) : (
            <span className="text-ink-2">{booking.message}</span>
          )}
          {booking.retries ? <span className="tag warn">重试 {booking.retries} 次</span> : null}
        </div>,
      );
    }

    if (res.degraded) {
      parts.push(
        <div key="dg" className="mt-2">
          <span className="tag warn">降级模式</span>
        </div>,
      );
    }

    if (res.proposals?.length) {
      parts.push(<Proposals key="pr" list={res.proposals} onPick={(p) => pick(p)} />);
    }
    if (res.citations?.length) {
      parts.push(<Citations key="ct" list={res.citations} />);
    }
    if (res.trace?.length) {
      parts.push(<Trace key="tr" list={res.trace} />);
    }
    return parts.length ? <>{parts}</> : undefined;
  }

  async function pick(p: Proposal) {
    const ok = await dialog.confirm(
      "确认预约方案",
      p.equipment_name + " · " + p.date + " " + p.start + "–" + p.end,
      { okText: "确认选择" },
    );
    if (!ok) return;
    await send("选：" + p.equipment_name + " " + p.date + " " + p.start + "-" + p.end, {
      equipmentId: p.equipment_id,
      date: p.date,
      start: p.start,
      end: p.end,
    });
  }
}

function Proposals({ list, onPick }: { list: Proposal[]; onPick: (p: Proposal) => void }) {
  return (
    <div className="mt-3 flex flex-col gap-2">
      {list.map((p, i) => (
        <div key={i} className="rounded-md border border-line bg-surface-2 p-2.5">
          <div className="flex items-start gap-2.5">
            <span className="mono mt-0.5 text-micro text-ink-3">{i + 1}</span>
            <div className="min-w-0 flex-1">
              <div className="text-base">
                {p.equipment_name} <span className="text-ink-3">· {p.lab_label}</span>
              </div>
              <div className="text-sm text-ink-3">
                {p.date} {p.start}-{p.end} · {p.hours}h
              </div>
              <div className="mt-1 flex flex-wrap gap-1">
                {p.relaxations.length ? (
                  p.relaxations.map((r) => (
                    <span key={r} className="tag warn">{r}</span>
                  ))
                ) : (
                  <span className="tag ok">完全满足</span>
                )}
              </div>
            </div>
            <div className="flex flex-col items-end gap-1.5">
              <span className="mono text-micro text-ink-3">{Math.round(p.score * 100)}%</span>
              <button className="btn btn-sm" onClick={() => onPick(p)}>
                选这个
              </button>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function Citations({ list }: { list: DocHit[] }) {
  return (
    <div className="mt-3 flex flex-col gap-2">
      {list.map((c, i) => (
        <div key={i} className="border-l-2 border-line-soft pl-2.5">
          <div className="flex items-center gap-2 text-sm font-medium text-ink-2">
            {c.heading}
            {c.matched_by?.length ? <span className="tag">{c.matched_by.join("+")}</span> : null}
          </div>
          <div className="text-sm text-ink-3">
            {c.text.length > 160 ? c.text.slice(0, 160) + "…" : c.text}
          </div>
          <div className="mono text-micro text-ink-3">
            {c.source} · {c.score.toFixed(3)}
          </div>
        </div>
      ))}
    </div>
  );
}

function Trace({ list }: { list: TraceStep[] }) {
  const max = Math.max(...list.map((t) => t.elapsed_ms || 0), 1);
  return (
    <div className="mt-3">
      <div className="mb-1 text-micro uppercase tracking-[0.08em] text-ink-3">执行轨迹</div>
      <div className="flex flex-col gap-1">
        {list.map((t, i) => (
          <div key={i} className="grid grid-cols-[78px_1fr_66px] items-center gap-2 text-micro">
            <span className="mono text-ink-2">{t.node}</span>
            <span className="flex items-center gap-2">
              <span
                className="inline-block h-1 rounded-pill bg-accent"
                style={{ width: Math.max(2, (t.elapsed_ms / max) * 100) + "%" }}
              />
              <span className="text-ink-3">{t.detail}</span>
            </span>
            <span className="mono text-right text-ink-3">{t.elapsed_ms} ms</span>
          </div>
        ))}
      </div>
    </div>
  );
}
