import { useMemo, useState } from "react";
import { apiPost } from "../lib/api";
import { BK_TIMES, isoDate } from "../lib/format";
import type { BookingOutcome } from "../lib/types";
import { useApp } from "../store/app";
import { useToast } from "../components/Toaster";
import { Note } from "../components/bits";

/* 直接预约：**不经过模型**的下单表单。
 *
 * 对话入口很好用，但它是唯一入口时，"模型不在" = "预约功能停摆"。
 * 这个表单走的 POST /api/reservations 与对话底层是同一套不变式，
 * 只是省掉了"把中文解析成参数"这一步 —— 额度用完、断网、端点挂了，
 * 这条路照样能走。
 */

export function BookPane({ onBooked }: { onBooked: () => void }) {
  const { equipment } = useApp();
  const { toast } = useToast();

  const [equipmentId, setEquipmentId] = useState("");
  const [date, setDate] = useState(() => isoDate(1));
  const [start, setStart] = useState("10:00");
  const [end, setEnd] = useState("12:00");
  const [purpose, setPurpose] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const list = useMemo(() => Array.from(equipment.values()), [equipment]);

  const submit = async () => {
    if (!equipmentId) {
      toast("先选一台设备");
      return;
    }
    if (!date) {
      toast("先选日期");
      return;
    }
    if (start >= end) {
      toast("结束时间要晚于开始时间");
      return;
    }
    setBusy(true);
    setResult(null);
    try {
      const { data } = await apiPost<BookingOutcome>("/api/reservations", {
        equipment_id: Number(equipmentId),
        date,
        start,
        end,
        purpose,
      });
      const res = data.reservation;
      // 真正的预约在 .reservation 里；.ok 只说明这次调用被接受。
      const needReview = !!res && res.status === "pending";
      setResult({
        ok: data.ok,
        text: res
          ? (needReview ? "已提交，等待管理员审批" : "预约成功") + " · #" + res.id + " · " + res.slot
          : data.message,
      });
      if (data.ok) {
        onBooked();
        toast(needReview ? "已提交，等待审批" : "预约成功", "ok");
      }
    } catch (e) {
      // 失败**原样**显示服务端给的原因，再按状态码补一句"下一步该干什么"。
      // 把 409 说成"预约失败"是没用的 —— 用户需要知道是换个时段还是换个设备。
      const err = e as Error & { status?: number };
      let text = err.message;
      if (err.status === 409) text += "（这个时段被占了，换个时段再试）";
      else if (err.status === 403) text += "（这不是时段问题，是资格问题：资质 / 违约限制）";
      else if (err.status === 422) text += "（填的内容有问题）";
      setResult({ ok: false, text });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <Note>
        选设备 → 选日期与时段 → 提交。<b>这条路径不经过模型</b>，Agent 不可用（额度用完 / 断网 / 端点挂了）时照样能约。
      </Note>

      <label className="fld-label mt-3 block" htmlFor="bk-equip">设备</label>
      <select id="bk-equip" className="fld mt-1 w-full" value={equipmentId} onChange={(e) => setEquipmentId(e.target.value)}>
        <option value="">{list.length ? "（选一台设备）" : "（设备目录尚未加载）"}</option>
        {list.map((e) => {
          const off = !!e.status && e.status !== "normal";
          return (
            <option key={e.id} value={e.id} disabled={off}>
              {e.name + " · " + e.code + (off ? "（" + e.status + "）" : e.requires_approval ? "（需审批）" : "")}
            </option>
          );
        })}
      </select>

      <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div>
          <label className="fld-label block" htmlFor="bk-date">日期</label>
          <input id="bk-date" className="fld mt-1 w-full" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        </div>
        <div>
          <label className="fld-label block" htmlFor="bk-start">开始</label>
          <select id="bk-start" className="fld mt-1 w-full" value={start} onChange={(e) => setStart(e.target.value)}>
            {BK_TIMES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div>
          <label className="fld-label block" htmlFor="bk-end">结束</label>
          <select id="bk-end" className="fld mt-1 w-full" value={end} onChange={(e) => setEnd(e.target.value)}>
            {BK_TIMES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
      </div>

      <label className="fld-label mt-3 block" htmlFor="bk-purpose">用途（可选）</label>
      <input
        id="bk-purpose"
        className="fld mt-1 w-full"
        placeholder="例如：课程实验 · 样品测定"
        value={purpose}
        onChange={(e) => setPurpose(e.target.value)}
      />

      <div className="mt-3 flex gap-2">
        <button className="btn" disabled={busy} onClick={() => void submit()}>
          {busy ? "提交中…" : "提交预约"}
        </button>
        <button
          className="btn btn-ghost"
          onClick={() => {
            setDate(isoDate(1));
            setStart("10:00");
            setEnd("12:00");
            toast("已填成明天上午十点到十二点");
          }}
        >
          明天上午
        </button>
      </div>

      {result ? (
        <div className="mt-3 flex items-start gap-2 text-sm">
          <span className={"tag " + (result.ok ? "ok" : "bad")}>{result.ok ? "成功" : "失败"}</span>
          <span className="text-ink-2">{result.text}</span>
        </div>
      ) : null}
    </div>
  );
}
