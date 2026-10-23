import { useMemo, useState } from "react";
import { apiGet, apiPost } from "../lib/api";
import type { BatchReviewOut, ReservationOut } from "../lib/types";
import { useAsync } from "../components/useAsync";
import { useDialog } from "../components/Dialog";
import { useToast } from "../components/Toaster";
import { Empty, ErrBox, Note, Skel } from "../components/bits";

/* 审批待办。
 *
 * 三条容易做错的地方：
 *  ① 驳回**必须**有理由，但理由允许为空字符串 —— 「取消」和「留空」
 *     是两件不同的事（Dialog 的 prompt 已经把两者分开）；
 *  ② 批量处理要**逐条**呈现失败。后端返回 {approved, failed:[{id,reason}]}，
 *     只报一个"成功 N 条"会把失败的悄悄吞掉；
 *  ③ 驳回会立刻释放时段 —— 这件事值得写进界面，因为它解释了
 *     "为什么我刚驳回，别人马上就约走了"。
 */

export function ApprovalsPane() {
  const { toast } = useToast();
  const dialog = useDialog();
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const state = useAsync<ReservationOut[]>(async (signal) => {
    const { data } = await apiGet<ReservationOut[]>("/api/reservations/pending", signal);
    return data;
  }, []);

  const rows = useMemo(() => state.data ?? [], [state.data]);

  const toggle = (id: number) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const refresh = () => {
    setPicked(new Set());
    state.reload();
  };

  const review = async (row: ReservationOut, kind: "approve" | "reject") => {
    let reason = "";
    if (kind === "reject") {
      const raw = await dialog.prompt("驳回申请 #" + row.id, "", {
        body: row.user_name + " · " + row.equipment_name + " · " + row.slot + "\n理由可以留空，但「取消」与「留空」不是一回事。",
        placeholder: "例如：该时段已安排教学使用",
        okText: "驳回",
        danger: true,
      });
      // null = 用户按了取消/关掉了对话框；"" = 用户就是要留空。
      if (raw === null) return;
      reason = raw;
    } else {
      const ok = await dialog.confirm("通过申请 #" + row.id, row.user_name + " · " + row.equipment_name + " · " + row.slot, {
        okText: "通过",
      });
      if (!ok) return;
    }
    try {
      await apiPost("/api/reservations/" + row.id + "/" + kind, kind === "reject" ? { reason } : undefined);
      toast(kind === "approve" ? "已通过 #" + row.id : "已驳回 #" + row.id, "ok");
      refresh();
    } catch (e) {
      toast("处理失败：" + (e as Error).message, "err");
    }
  };

  const batch = async (approve: boolean) => {
    const ids = Array.from(picked);
    if (!ids.length) {
      toast("还没选申请");
      return;
    }
    let reason = "";
    if (!approve) {
      const raw = await dialog.prompt("批量驳回 " + ids.length + " 条", "", {
        placeholder: "理由（可留空）",
        okText: "全部驳回",
        danger: true,
      });
      if (raw === null) return;
      reason = raw;
    } else {
      const ok = await dialog.confirm("批量通过 " + ids.length + " 条", "通过后这些预约转为已确认。", { okText: "全部通过" });
      if (!ok) return;
    }
    try {
      const { data } = await apiPost<BatchReviewOut>("/api/reservations/batch-review", { ids, approve, reason });
      toast("已处理 " + data.approved + " 条", "ok");
      if (data.failed.length) {
        // 逐条列出来，不合并成"部分失败"
        await dialog.confirm(
          "部分未处理",
          data.failed.map((f) => "#" + f.id + " " + f.reason).join("\n"),
          { okText: "知道了", cancelText: "关闭" },
        );
      }
      refresh();
    } catch (e) {
      toast("批量处理失败：" + (e as Error).message, "err");
    }
  };

  if (state.loading && !state.data) return <Skel rows={2} />;
  if (state.error) return <ErrBox error={state.error} onRetry={state.reload} what="加载待办" />;

  return (
    <div>
      <Note>
        待确认的申请。驳回会<b>立刻释放时段</b>，别人马上能约；通过则转为已确认。
      </Note>

      <div className="my-2 flex flex-wrap items-center gap-2">
        <button className="btn btn-ghost btn-sm" onClick={() => setPicked(new Set(rows.map((r) => r.id)))}>全选</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setPicked(new Set())}>清空</button>
        <button className="btn btn-sm" disabled={!picked.size} onClick={() => void batch(true)}>批量通过</button>
        <button className="btn btn-ghost btn-sm" disabled={!picked.size} onClick={() => void batch(false)}>批量驳回</button>
        <span className="hint">已选 {picked.size} / {rows.length}</span>
      </div>

      {rows.length === 0 ? (
        <Empty>没有待处理的申请</Empty>
      ) : (
        <table className="dt">
          <thead>
            <tr><th></th><th>#</th><th>申请人</th><th>设备</th><th>时段</th><th>用途</th><th>操作</th></tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><input type="checkbox" checked={picked.has(r.id)} onChange={() => toggle(r.id)} /></td>
                <td className="mono text-ink-3">{r.id}</td>
                <td>{r.user_name}</td>
                <td>
                  {r.equipment_name}
                  <div className="text-sm text-ink-3">{r.lab_label}</div>
                </td>
                <td className="mono text-sm">{r.slot}</td>
                <td className="text-sm text-ink-3">{r.purpose || "—"}</td>
                <td>
                  <div className="flex gap-1.5">
                    <button className="btn btn-sm" onClick={() => void review(r, "approve")}>通过</button>
                    <button className="btn btn-ghost btn-sm" onClick={() => void review(r, "reject")}>驳回</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
