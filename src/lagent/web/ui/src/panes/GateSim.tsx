import { useEffect, useState } from "react";
import { apiGet, apiPost, exportReservations } from "../lib/api";
import { GATE_REASON, fmtStamp, isoDate } from "../lib/format";
import type { AccessIssueResponse, AccessVerifyResponse, InsideEntry } from "../lib/types";
import { useApp } from "../store/app";
import { useUserOptions } from "../components/useUserOptions";
import { useToast } from "../components/Toaster";
import { Empty, Note, Tag } from "../components/bits";

/* 门禁核验 + 在馆名单 + 手工签发。
 *
 * 三处只有踩过才知道的地方：
 *
 *  ① **放行时 reason_code 是空串。** 如果先查 GATE_REASON 表再取默认值，
 *     一次成功的放行会渲染成红色的「—」，看起来像被拒绝了。
 *     正确顺序是先看 granted，granted 为真直接是「放行 / ok」。
 *
 *  ② /api/access/inside 返回的是 {count, inside}，不是裸数组。
 *     按数组写会 undefined.length 抛异常 —— 而"在馆名单"恰恰是
 *     清场时最不能坏的那一屏。
 *
 *  ③ 「刷卡出场」只在 granted && user_id && direction === "in" 时给。
 *     出场必须识别到身份；进场可以只凭凭证，出场不行。
 */

type Tone = "" | "ok" | "warn" | "bad";

export function GateSimPane() {
  const { labs, setLabs } = useApp();
  const users = useUserOptions();
  const { toast } = useToast();

  // ---- 核验 ----
  const [labId, setLabId] = useState("");
  const [userId, setUserId] = useState("");
  const [cred, setCred] = useState("");
  const [direction, setDirection] = useState<"in" | "out">("in");
  const [precheck, setPrecheck] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState<AccessVerifyResponse | null>(null);
  const [verifyErr, setVerifyErr] = useState<string | null>(null);

  // ---- 在馆名单 ----
  const [inside, setInside] = useState<InsideEntry[] | null>(null);
  const [insideErr, setInsideErr] = useState<string | null>(null);

  // ---- 签发 ----
  const [issueUser, setIssueUser] = useState("");
  const [issueDate, setIssueDate] = useState(() => isoDate(0));
  const [issueFrom, setIssueFrom] = useState("09:00");
  const [issueTo, setIssueTo] = useState("18:00");
  const [reason, setReason] = useState("");
  const [issuing, setIssuing] = useState(false);
  const [issued, setIssued] = useState<AccessIssueResponse | null>(null);
  const [issueErr, setIssueErr] = useState<string | null>(null);

  // 实验室下拉：/api/labs 可能还没被 LabsPane 拉过（比如直接点进门禁页）
  useEffect(() => {
    if (labs.length) return;
    apiGet<Parameters<typeof setLabs>[0]>("/api/labs")
      .then(({ data }) => setLabs(data))
      .catch(() => {});
  }, [labs.length, setLabs]);

  const loadInside = async () => {
    setInsideErr(null);
    try {
      const { data } = await apiGet<{ count: number; inside: InsideEntry[] }>("/api/access/inside");
      // 注意这里是 data.inside：接口返回的是 {count, inside}
      setInside(data.inside);
    } catch (e) {
      setInsideErr((e as Error).message);
    }
  };

  const verify = async () => {
    if (!labId) {
      setVerifyErr("先选一个实验室");
      return;
    }
    const uid = userId ? Number(userId) : null;
    if (!uid && !cred.trim()) {
      setVerifyErr("凭证与身份至少给一个 —— 两个都空等于「既没带卡也说不清是谁」");
      return;
    }
    setVerifying(true);
    setVerifyErr(null);
    try {
      const { data } = await apiPost<AccessVerifyResponse>("/api/access/verify", {
        lab_id: Number(labId),
        user_id: uid,
        credential: cred.trim() || null,
        direction,
        gate_id: "console-sim",
        precheck,
      });
      setResult(data);
    } catch (e) {
      setResult(null);
      setVerifyErr("核验失败：" + (e as Error).message);
    } finally {
      setVerifying(false);
    }
  };

  const issue = async () => {
    if (!issueUser) { setIssueErr("先选签发给谁"); return; }
    if (!labId) { setIssueErr("还没选实验室：请在上面「实验室」下拉里选一个（签发要知道凭证对哪个门有效）"); return; }
    if (!issueDate || !issueFrom || !issueTo) { setIssueErr("日期与起止时间都要填"); return; }
    if (!reason.trim()) { setIssueErr("必须写理由：谁能绕过预约流程要有据可查"); return; }
    setIssuing(true);
    setIssueErr(null);
    try {
      const { data } = await apiPost<AccessIssueResponse>("/api/access/issue", {
        user_id: Number(issueUser),
        lab_id: Number(labId),
        date: issueDate,
        valid_from: issueFrom,
        valid_to: issueTo,
        reason: reason.trim(),
      });
      setIssued(data);
      // 明文凭证只返回这一次 —— 直接填进核验框，顺手刷在馆名单，
      // 让人可以立刻点一下「核验」看它是否真的放行。
      setCred(data.credential);
      toast("已签发凭证 #" + data.permit_id, "ok");
      void loadInside();
    } catch (e) {
      setIssueErr("签发失败：" + (e as Error).message);
    } finally {
      setIssuing(false);
    }
  };

  const exportCsv = () => {
    void exportReservations().then(
      () => toast("已导出 CSV", "ok"),
      (e: Error) => toast("导出失败：" + e.message, "err"),
    );
  };

  // granted 优先，**不是**先查表：放行时 reason_code 是空串
  const meta = result
    ? result.granted
      ? { text: "放行", tone: "ok" as Tone }
      : ((): { text: string; tone: Tone } => {
          const hit = GATE_REASON[result.reason_code];
          const tone = (hit?.tone ?? "bad") as Tone;
          return { text: hit?.text ?? result.reason_code ?? "—", tone };
        })()
    : null;

  // 出场必须识别身份；进场可以只凭凭证
  const canCheckOut = !!result && result.granted && !!result.user_id && direction === "in";
  const needIdentityForOut = !!result && result.granted && !result.user_id && direction === "in";

  return (
    <div>
      <Note>
        模拟<b>门禁机</b>发起一次核验。凭证与身份<b>至少给一个</b>；两个都给会交叉校验 —— 这正是挡住「拿别人截图进门」的那一步。
      </Note>

      <div className="mt-3 flex flex-wrap gap-2">
        <select className="fld" aria-label="实验室" value={labId} onChange={(e) => setLabId(e.target.value)}>
          <option value="">（选实验室）</option>
          {labs.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
        </select>
        <select className="fld" aria-label="身份" value={userId} onChange={(e) => setUserId(e.target.value)}>
          <option value="">（不提供身份）</option>
          {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
        </select>
        <select className="fld" aria-label="方向" value={direction} onChange={(e) => setDirection(e.target.value as "in" | "out")}>
          <option value="in">进</option>
          <option value="out">出</option>
        </select>
        <input
          className="fld flex-1"
          placeholder="凭证串（可留空，走身份核验）"
          value={cred}
          onChange={(e) => setCred(e.target.value)}
        />
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label className="fld-label flex items-center gap-1.5">
          <input type="checkbox" checked={precheck} onChange={(e) => setPrecheck(e.target.checked)} />
          仅预检（不核销、不占座）
        </label>
        <span className="flex-1" />
        <button className="btn" disabled={verifying} onClick={() => void verify()}>{verifying ? "核验中…" : "核验"}</button>
        <button className="btn btn-ghost" onClick={() => void loadInside()}>在馆名单</button>
        <button className="btn btn-ghost" onClick={exportCsv}>导出 CSV</button>
      </div>

      {verifyErr ? <div className="empty mt-2">{verifyErr}</div> : null}

      {result && meta ? (
        <div className="mt-3 rounded-md border border-line p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className={"text-md font-medium " + (result.granted ? "text-ok-ink" : "text-bad-ink")}>
              {result.granted ? "✓ 放行" : "✗ 拒绝"}
            </span>
            <Tag tone={meta.tone}>{meta.text}</Tag>
            <span className="mono text-sm text-ink-3">{result.reason_code || "—"}</span>
          </div>
          <div className="mt-1 text-sm text-ink-2">{result.message}</div>
          <div className="mt-1.5 text-sm text-ink-3">
            用户：{result.user_name || "（未识别）"} · 实验室：{result.lab_label || "—"} ·{" "}
            {result.permit_id ? "凭证 #" + result.permit_id : "未签发凭证"}
          </div>
          {canCheckOut ? (
            <button
              className="btn btn-sm mt-2"
              onClick={() => {
                setDirection("out");
                void verify();
              }}
            >
              刷卡出场
            </button>
          ) : null}
          {needIdentityForOut ? (
            <div className="hint mt-2">
              要看出场，请在上面的身份下拉里选人再核验 —— 出场必须识别到身份（进场可以只凭凭证，出场不行）。
            </div>
          ) : null}
        </div>
      ) : null}

      {insideErr ? <div className="empty mt-2">{insideErr}</div> : null}
      {inside ? (
        <div className="mt-3">
          <div className="mb-1.5 text-sm font-medium text-ink-2">在馆 {inside.length} 人</div>
          {inside.length === 0 ? (
            <Empty>现在没有人在馆</Empty>
          ) : (
            <table className="dt">
              <thead>
                <tr><th>用户</th><th>实验室</th><th>有效期</th></tr>
              </thead>
              <tbody>
                {inside.map((it) => (
                  <tr key={it.permit_id}>
                    <td>{it.username}</td>
                    <td>{it.lab_label}</td>
                    <td className="mono text-sm">
                      {fmtStamp(it.valid_from)} → {fmtStamp(it.valid_to)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : null}

      {/* ---- 手工签发 ---- */}
      <div className="mt-5 rounded-md border border-line-soft bg-surface-2 p-3">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="k">手工签发凭证</span>
          <span className="hint">管理员专用 · 必须写理由 · 每次进审计</span>
        </div>

        <div className="mt-2 flex flex-wrap gap-2">
          <select className="fld" aria-label="签发给谁" value={issueUser} onChange={(e) => setIssueUser(e.target.value)}>
            <option value="">（选择用户）</option>
            {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
          </select>
          <input className="fld" type="date" value={issueDate} onChange={(e) => setIssueDate(e.target.value)} />
          <input className="fld" type="time" step={1800} value={issueFrom} onChange={(e) => setIssueFrom(e.target.value)} />
          <input className="fld" type="time" step={1800} value={issueTo} onChange={(e) => setIssueTo(e.target.value)} />
        </div>

        <div className="mt-2 flex flex-wrap gap-2">
          <input
            className="fld flex-1"
            placeholder="理由（必填，会写进审计）"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <button className="btn" disabled={issuing} onClick={() => void issue()}>{issuing ? "签发中…" : "签发"}</button>
        </div>

        <div className="note mt-2">
          {labId
            ? "将签到：" + (labs.find((l) => String(l.id) === labId)?.label ?? "—") + "（换实验室请改上面的下拉）"
            : "⚠ 还没选实验室 —— 签发前请先在上面核验区的「实验室」下拉里选一个。"}
        </div>

        {issueErr ? <div className="empty mt-2">{issueErr}</div> : null}

        {issued ? (
          <div className="mt-2 rounded-md border border-ok-ink/30 bg-ok-bg p-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <Tag tone="ok">✓ 已签发</Tag>
              <span className="mono text-sm">凭证 #{issued.permit_id}</span>
              <span className="text-sm text-ink-3">{issued.valid_from} → {issued.valid_to}</span>
            </div>
            <div className="mt-1.5 rounded-sm bg-surface p-2">
              <code className="mono break-all text-sm">{issued.credential}</code>
            </div>
            <div className="hint mt-1.5">
              ⚠ 明文凭证只返回这一次，系统只留哈希 —— 已自动填入上方核验框，可直接点「核验」验证放行。关掉就看不到了。
            </div>
            {issued.required_certs.length ? (
              <div className="hint mt-1">该实验室要求资质：{issued.required_certs.join(" / ")}</div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
