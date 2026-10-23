import { useEffect, useMemo, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { NOTIFY_CN, fmtStamp } from "../lib/format";
import type { NotificationOut } from "../lib/types";
import { useUserOptions } from "../components/useUserOptions";
import { useToast } from "../components/Toaster";
import { Empty, ErrBox, Skel, Tag } from "../components/bits";

/* 通知投递记录。
 *
 * "重投"是个容易被误解的操作：它只是把记录放回待投递队列，
 * **不是**立刻送达。所以提示文案必须写「已放回 N 条，等待下次投递」，
 * 写「已重发」会让用户以为对方已经收到了。
 */

const tone = (status: string) => (status === "failed" ? "bad" : status === "sent" ? "ok" : "warn");

export function NotificationsPane() {
  const users = useUserOptions();
  const { toast } = useToast();
  const [uid, setUid] = useState("");
  const [rows, setRows] = useState<NotificationOut[] | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!uid) {
      setRows(null);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    apiGet<NotificationOut[]>("/api/notifications?user_id=" + uid)
      .then(({ data }) => {
        if (!alive) return;
        setRows(data);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(e as Error);
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [uid, nonce]);

  const summary = useMemo(() => {
    const all = rows ?? [];
    const by: Record<string, number> = {};
    all.forEach((r) => { by[r.status] = (by[r.status] || 0) + 1; });
    return Object.entries(by).map(([k, n]) => (NOTIFY_CN[k] || k) + " " + n).join(" · ");
  }, [rows]);

  const retry = async () => {
    try {
      const { data } = await apiPost<{ requeued: number }>("/api/notifications/retry", {});
      if (data.requeued) {
        // 注意措辞：放回队列 ≠ 已送达
        toast("已放回 " + data.requeued + " 条，等待下次投递", "ok");
        setNonce((n) => n + 1);
      } else {
        toast("没有需要重投的通知");
      }
    } catch (e) {
      toast("重投失败：" + (e as Error).message, "err");
    }
  };

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select className="fld" value={uid} onChange={(e) => setUid(e.target.value)}>
          <option value="">（选一个用户）</option>
          {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
        </select>
        <button className="btn btn-ghost btn-sm" onClick={() => setNonce((n) => n + 1)}>刷新</button>
        <button className="btn btn-ghost btn-sm" onClick={() => void retry()}>重投失败通知</button>
      </div>

      {!uid ? <Empty>选一个用户看他的通知</Empty> : null}
      {loading && uid ? <Skel rows={2} /> : null}
      {error ? <ErrBox error={error as never} onRetry={() => setNonce((n) => n + 1)} what="加载通知" /> : null}

      {rows && !loading ? (
        rows.length === 0 ? (
          <Empty>这个人没有通知</Empty>
        ) : (
          <>
            <div className="hint mb-2">共 {rows.length} 条 · {summary}</div>
            <table className="dt">
              <thead>
                <tr><th>时间</th><th>类型</th><th>标题</th><th>状态</th></tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td className="mono text-sm text-ink-3">{fmtStamp(r.created_at)}</td>
                    <td className="mono text-sm">{r.kind}</td>
                    <td>
                      {r.title}
                      <div className="text-sm text-ink-3">{r.body}</div>
                    </td>
                    <td><Tag tone={tone(r.status) as "ok" | "warn" | "bad"}>{NOTIFY_CN[r.status] || r.status}</Tag></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        )
      ) : null}
    </div>
  );
}
