import { useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import { fmtStamp } from "../lib/format";
import type { AuditLogOut } from "../lib/types";
import { Empty, ErrBox, Skel, Tag } from "../components/bits";

/* 审计日志。只读 —— 这一页没有任何写操作，也不该有。
   审计如果能被界面改，"审计"这两个字就没有意义了。 */

export function AuditPane() {
  const [action, setAction] = useState("");
  const [rows, setRows] = useState<AuditLogOut[] | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    const qs = "?limit=100" + (action.trim() ? "&action=" + encodeURIComponent(action.trim()) : "");
    apiGet<AuditLogOut[]>("/api/audit" + qs)
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
  }, [action, nonce]);

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          className="fld flex-1"
          placeholder="按动作过滤，例如 reservation.create"
          value={action}
          onChange={(e) => setAction(e.target.value)}
        />
        <button className="btn btn-ghost btn-sm" onClick={() => setNonce((n) => n + 1)}>刷新</button>
      </div>

      {loading ? <Skel rows={3} /> : null}
      {error ? <ErrBox error={error as never} onRetry={() => setNonce((n) => n + 1)} what="加载审计日志" /> : null}

      {rows && !loading ? (
        rows.length === 0 ? (
          <Empty>没有匹配的审计记录</Empty>
        ) : (
          <table className="dt">
            <thead>
              <tr><th>时间</th><th>动作</th><th>操作者</th><th>对象</th><th>结果</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td className="mono text-sm text-ink-3">{fmtStamp(r.created_at)}</td>
                  <td className="mono text-sm">{r.action}</td>
                  <td>{r.actor_name || "—"}</td>
                  <td className="text-sm text-ink-2">
                    {r.target_type ? r.target_type + (r.target_id ? " #" + r.target_id : "") : "—"}
                  </td>
                  <td>
                    <Tag tone={r.outcome === "ok" ? "ok" : "bad"}>{r.outcome === "ok" ? "ok" : "bad"}</Tag>
                    {r.detail ? <div className="text-sm text-ink-3">{r.detail}</div> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
      ) : null}
    </div>
  );
}
