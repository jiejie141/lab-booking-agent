import { useEffect, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import { fmtStamp } from "../lib/format";
import type { ReservationOut, ViolationsOut } from "../lib/types";
import { useUserOptions } from "../components/useUserOptions";
import { useDialog } from "../components/Dialog";
import { useToast } from "../components/Toaster";
import { Empty, ErrBox, Skel, Tag } from "../components/bits";

/* 违约账。
 *
 * 这一页读的是两组数据：
 *   ① GET /api/users/{uid}/violations → 计数 / 阈值 / 是否启用
 *   ② GET /api/reservations?user_id= → 再在前端筛出 no_show_at 非空的
 * 第二条之所以不在后端加参数，是因为"未到场"不是一个 status，
 * 而是预约上的一个时间戳 —— 一条 completed 的预约也可能被判定过未到场。
 *
 * 豁免接口 **不收理由**，所以界面也不假装收：多一个输入框而服务端丢掉，
 * 比没有输入框更坏 —— 用户会以为理由被记录了。
 */

export function ViolationsPane() {
  const users = useUserOptions();
  const { toast } = useToast();
  const dialog = useDialog();
  const [uid, setUid] = useState("");
  const [data, setData] = useState<ViolationsOut | null>(null);
  const [rows, setRows] = useState<ReservationOut[]>([]);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!uid) {
      setData(null);
      setRows([]);
      return;
    }
    let alive = true;
    setLoading(true);
    setError(null);
    Promise.all([
      apiGet<ViolationsOut>("/api/users/" + uid + "/violations"),
      apiGet<ReservationOut[]>("/api/reservations?user_id=" + uid),
    ])
      .then(([v, r]) => {
        if (!alive) return;
        setData(v.data);
        setRows(r.data.filter((x) => !!x.no_show_at));
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

  const pardon = async (row: ReservationOut) => {
    const ok = await dialog.confirm("豁免 #" + row.id, "把这次「未到场」从违约账里抹掉。", { okText: "豁免" });
    if (!ok) return;
    try {
      await apiPost("/api/reservations/" + row.id + "/pardon");
      toast("已豁免 #" + row.id, "ok");
      setNonce((n) => n + 1);
    } catch (e) {
      toast("豁免失败：" + (e as Error).message, "err");
    }
  };

  const tone = data ? (data.blocked ? "bad" : data.over_threshold ? "warn" : "") : "";
  const stateText = data
    ? data.blocked
      ? "已被限制"
      : data.over_threshold
        ? "已超阈值"
        : "正常"
    : "";

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <select className="fld" value={uid} onChange={(e) => setUid(e.target.value)}>
          <option value="">（选一个用户）</option>
          {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
        </select>
        <button className="btn btn-ghost btn-sm" onClick={() => setNonce((n) => n + 1)}>刷新</button>
      </div>

      {!uid ? <Empty>选一个用户，看他的违约账</Empty> : null}
      {loading && uid ? <Skel rows={2} /> : null}
      {error ? <ErrBox error={error as never} onRetry={() => setNonce((n) => n + 1)} what="加载违约记录" /> : null}

      {data && !loading ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <span className="text-ink-2">当前状态</span>
            <Tag tone={tone as "" | "ok" | "warn" | "bad"}>{stateText}</Tag>
            {data.message ? <span className="hint">{data.message}</span> : null}
          </div>

          <table className="dt">
            <thead>
              <tr><th>窗口内计入次数</th><th>阈值</th><th>窗口天数</th><th>限制是否启用</th></tr>
            </thead>
            <tbody>
              <tr>
                <td className="mono">{data.count}</td>
                <td className="mono">{data.threshold}</td>
                <td className="mono">{data.window_days}</td>
                <td>{data.blocking_enabled ? "是" : <span className="text-ink-3">否（默认只记不罚）</span>}</td>
              </tr>
            </tbody>
          </table>

          <div>
            <div className="mb-1.5 text-sm font-medium text-ink-2">被判定过「未到场」的预约</div>
            {rows.length === 0 ? (
              <Empty>没有被判定过「未到场」的预约</Empty>
            ) : (
              <table className="dt">
                <thead>
                  <tr><th>#</th><th>设备</th><th>时段</th><th>判定时间</th><th>操作</th></tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td className="mono text-ink-3">{r.id}</td>
                      <td>{r.equipment_name}<div className="text-sm text-ink-3">{r.lab_label}</div></td>
                      <td className="mono text-sm">{r.slot}</td>
                      <td className="text-sm text-ink-3">
                        {r.pardoned_at ? <Tag tone="ok">已豁免 {fmtStamp(r.pardoned_at)}</Tag> : fmtStamp(r.no_show_at)}
                      </td>
                      <td>
                        {r.pardoned_at ? null : (
                          <button className="btn btn-ghost btn-sm" onClick={() => void pardon(r)}>豁免</button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
