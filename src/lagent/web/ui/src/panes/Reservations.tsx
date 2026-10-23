import { useMemo, useState } from "react";
import { apiGet, apiPatch, apiPost, exportReservations } from "../lib/api";
import { STATUS_CN, STATUS_TAG } from "../lib/format";
import type { ReservationOut, ReservationStatus } from "../lib/types";
import { useApp } from "../store/app";
import { useAsync } from "../components/useAsync";
import { useDialog } from "../components/Dialog";
import { useToast } from "../components/Toaster";
import { Empty, ErrBox, Skel, Tag } from "../components/bits";

/* 预约记录。
 *
 * 两个刻意的选择：
 *  ① 状态筛选在**前端**做。列表最多几百条，一次拿全比每换一个筛选就
 *     打一次请求更快，也不会因为连续点击而出现"后发的请求先回来"
 *     把列表刷成旧数据；
 *  ② 改期走 PATCH，**不是**"取消再重约"。两者之间有一个窗口：
 *     取消成功的下一秒，那个时段可能已经被别人拿走。
 */

const STATUSES: ReservationStatus[] = ["pending", "confirmed", "cancelled", "completed", "expired"];

export function ReservationsPane() {
  const { isAdmin } = useApp();
  const { toast } = useToast();
  const dialog = useDialog();
  const [userFilter, setUserFilter] = useState("");
  const [status, setStatus] = useState("");
  const [tick, setTick] = useState(0);

  const state = useAsync<{ rows: ReservationOut[]; users: { id: number; name: string }[] }>(
    async (signal) => {
      const qs = userFilter ? "?user_id=" + encodeURIComponent(userFilter) : "";
      const [{ data: rows }, users] = await Promise.all([
        apiGet<ReservationOut[]>("/api/reservations" + qs, signal),
        isAdmin
          ? apiGet<{ id: number; username: string }[]>("/api/users?limit=200", signal)
              .then((r) => r.data.map((u) => ({ id: u.id, name: u.username })))
              .catch(() => [])
          : Promise.resolve([]),
      ]);
      return { rows, users };
    },
    // tick 让"延后一点再拉一次"能复用同一个 loader
    [userFilter, isAdmin, tick],
  );

  const rows = useMemo(() => {
    const all = state.data?.rows ?? [];
    return status ? all.filter((r) => r.status === status) : all;
  }, [state.data, status]);

  const counts = useMemo(() => {
    const all = state.data?.rows ?? [];
    const out: Record<string, number> = {};
    STATUSES.forEach((s) => { out[s] = all.filter((r) => r.status === s).length; });
    out.__total = all.length;
    return out;
  }, [state.data]);

  const afterChange = () => {
    state.reload();
    setTick((n) => n + 1);
  };

  const move = async (row: ReservationOut) => {
    const cur = row.slot.trim().slice(-11);
    // 格式不对就**重新弹**并保留用户刚敲的内容 —— 弹个 toast 让输入消失，
    // 等于让用户从头再敲一遍。
    let value = cur;
    for (;;) {
      const raw = await dialog.prompt("改期 #" + row.id, value, {
        body: "新的时段，格式 HH:MM-HH:MM（原 " + cur + "）",
        placeholder: "14:00-16:00",
        okText: "改期",
      });
      if (raw === null) return;
      const parts = String(raw).split(/[-–—～~]/).map((s) => s.trim());
      if (parts.length === 2 && parts.every((p) => /^\d{2}:\d{2}$/.test(p))) {
        try {
          const { data } = await apiPatch<{ message?: string }>("/api/reservations/" + row.id, {
            start: parts[0] + ":00",
            end: parts[1] + ":00",
          });
          toast(data.message || "已改期 #" + row.id, "ok");
          afterChange();
        } catch (e) {
          toast("改期失败：" + (e as Error).message, "err");
        }
        return;
      }
      value = String(raw);
    }
  };

  const cancel = async (row: ReservationOut) => {
    const ok = await dialog.confirm("取消预约 #" + row.id, row.equipment_name + " · " + row.slot, {
      okText: "取消这条预约",
      danger: true,
    });
    if (!ok) return;
    try {
      // 身份取自令牌：管理员代别人取消要传 as_user_id，
      // 普通用户传了会 403 —— 这条不变量值得在界面上也保持。
      await apiPost("/api/reservations/cancel", { reservation_id: row.id, reason: "控制台取消" });
      toast("已取消预约 #" + row.id, "ok");
      afterChange();
    } catch (e) {
      toast("取消失败：" + (e as Error).message, "err");
    }
  };

  if (state.loading && !state.data) return <Skel rows={3} />;
  if (state.error) return <ErrBox error={state.error} onRetry={state.reload} what="加载预约记录" />;

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {isAdmin ? (
          <select className="fld" value={userFilter} onChange={(e) => setUserFilter(e.target.value)}>
            <option value="">全部用户</option>
            {(state.data?.users ?? []).map((u) => <option key={u.id} value={u.id}>{u.name}</option>)}
          </select>
        ) : null}
        <select className="fld" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部状态</option>
          {STATUSES.map((s) => <option key={s} value={s}>{STATUS_CN[s]}</option>)}
        </select>
        <button
          className="btn btn-ghost btn-sm"
          onClick={() => {
            void exportReservations().catch((e: Error) => toast("导出失败：" + e.message, "err"));
          }}
        >
          导出 CSV
        </button>
      </div>

      {counts.__total ? <Distribution counts={counts} /> : null}

      {rows.length === 0 ? (
        <Empty>没有匹配的预约记录</Empty>
      ) : (
        <table className="dt">
          <thead>
            <tr><th>#</th><th>设备</th><th>时段</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td className="mono text-ink-3">{r.id}</td>
                <td>
                  {r.equipment_name}
                  <div className="text-sm text-ink-3">{r.lab_label}</div>
                </td>
                <td className="mono text-sm">{r.slot}</td>
                <td><Tag tone={(STATUS_TAG[r.status] || "") as "" | "ok" | "warn" | "bad"}>{STATUS_CN[r.status]}</Tag></td>
                <td>
                  {r.status === "pending" || r.status === "confirmed" ? (
                    <div className="flex gap-1.5">
                      <button className="btn btn-ghost btn-sm" onClick={() => void move(r)}>改期</button>
                      <button className="btn btn-ghost btn-sm" onClick={() => void cancel(r)}>取消</button>
                    </div>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/* 状态分布条。不做饼图：五个计数已经很说明问题，
   一条按状态着色的横条能同时表达"有多少"和"什么占比"。 */
function Distribution({ counts }: { counts: Record<string, number> }) {
  const total = counts.__total || 1;
  const color: Record<string, string> = {
    pending: "var(--c-warn-ink)",
    confirmed: "var(--c-ok-ink)",
    cancelled: "var(--c-ink-3)",
    completed: "var(--c-accent)",
    expired: "var(--c-bad-ink)",
  };
  return (
    <div className="mb-3 grid grid-cols-[44px_1fr] items-center gap-2">
      <span className="text-micro uppercase tracking-[0.08em] text-ink-3">分布</span>
      <div className="flex h-2 overflow-hidden rounded-pill bg-surface-3">
        {STATUSES.map((s) => {
          const n = counts[s] || 0;
          if (!n) return null;
          return (
            <span
              key={s}
              title={STATUS_CN[s] + " " + n}
              style={{ width: (n / total) * 100 + "%", background: color[s] }}
            />
          );
        })}
      </div>
    </div>
  );
}
