import { useEffect, useRef, useState } from "react";
import { login as apiLogin, register as apiRegister } from "../lib/api";
import { useApp } from "../store/app";
import { useToast } from "./Toaster";
import { BookingStory } from "./BookingStory";

/* 登录闸门。
 *
 * 左侧不是装饰：这个项目真正想让人看见的东西 ——「约不上时告诉你放宽什么」——
 * 在登录之后才看得到，但**决定要不要登录**是在这之前做的。
 * 所以把三件最能说明它和普通表单系统不同的能力放在闸门左边。
 */

interface DemoAccount {
  label: string;
  username: string;
  password: string;
  certs: string;
  can: string;
  cant: string;
  /* 演示账号的说明直接写成结构化数据而不是一段文案：
     改一个字段就够，不用去正则里抠句子。 */
}

const DEMO: DemoAccount[] = [
  {
    label: "张伟 · 仅光谱",
    username: "张伟",
    password: "zhangwei@123",
    certs: "光谱",
    can: "光谱类设备",
    cant: "离心机、色谱仪、细胞培养",
  },
  {
    label: "李娜 · 资质齐全",
    username: "李娜",
    password: "lina@123",
    certs: "光谱 / 色谱 / 细胞培养 / 离心",
    can: "全部设备",
    cant: "无",
  },
  {
    label: "管理员 · 可看全量",
    username: "管理员",
    password: "admin@123",
    certs: "（管理员看全量）",
    can: "全部设备 + 审批与用户管理",
    cant: "无",
  },
];

export function Gate() {
  const { signIn } = useApp();
  const { toast } = useToast();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [demo, setDemo] = useState<DemoAccount | null>(null);

  const userRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    userRef.current?.focus();
  }, []);

  /* 登录/注册**不走 api 的 request()**：401 会触发会话过期、把闸门重置掉，
     于是刚填的用户名密码连同错误提示一起消失 —— 用户只会看到"点了没反应"。
     lib/api.ts 里 login/register 已经是裸 fetch，这里直接用。 */
  const submit = async () => {
    if (busy) return;
    setError("");
    if (!username.trim() || !password) {
      setError(mode === "login" ? "请填写用户名与密码" : "请填写用户名、邮箱与密码");
      return;
    }
    if (mode === "register" && !email.trim()) {
      setError("请填写用户名、邮箱与密码");
      return;
    }
    setBusy(true);
    try {
      const res =
        mode === "login"
          ? await apiLogin(username.trim(), password)
          : await apiRegister(username.trim(), email.trim(), password);
      // 登录/注册都直接给 token + user，不用再打一次 /api/auth/me
      signIn(res.access_token, res.user);
      toast(mode === "login" ? "已登录：" + res.user.username : "注册成功，欢迎：" + res.user.username, "ok");
    } catch (e) {
      setError((e as Error).message || "登录失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto grid min-h-[100dvh] max-w-[1180px] items-center gap-[clamp(40px,7vw,110px)] px-8 py-10 lg:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
      <aside>
        <div className="mb-8 flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-md bg-[#264e3e] font-display text-[15px] text-white">
            LB
          </span>
          <div className="leading-tight">
            <b className="block text-md">lab-booking-agent</b>
            <small className="text-sm text-ink-3">智能实验室预约</small>
          </div>
        </div>

        <p className="text-sm uppercase tracking-[0.12em] text-ink-3">为每一次探索，留好时间</p>
        <h1 className="mt-3 font-display text-[clamp(36px,4.4vw,60px)] leading-[1.08] tracking-[-0.02em]">
          让研究继续。
          <br />
          <strong className="font-normal italic text-[#23694f]">让预约更简单。</strong>
        </h1>
        <p className="mt-4 max-w-[42ch] text-md text-ink-2">
          说出你的实验计划。遇到冲突，一起找到下一种可能。
        </p>

        <BookingStory />

        <div className="mt-7 flex flex-wrap gap-x-5 gap-y-2 text-sm text-ink-3">
          <span>资质先行</span>
          <span>取舍可解释</span>
          <span>过程可追溯</span>
        </div>
      </aside>

      <div className="w-full max-w-[420px] justify-self-end rounded-lg border border-line bg-surface p-7 shadow-sm">
        <div className="mb-5">
          <span className="text-micro uppercase tracking-[0.12em] text-ink-3">欢迎回来</span>
          <h2 className="mt-1 text-[25px] leading-tight">
            {mode === "login" ? "进入你的实验工作台" : "注册一个新的账号"}
          </h2>
          <p className="mt-1.5 text-sm text-ink-3">
            {mode === "login" ? "管理预约、发现设备，让实验安排更轻松。" : "注册后直接登录。角色固定为普通用户，提权找管理员。"}
          </p>
        </div>

        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label className="fld-label" htmlFor="lg-user">
            用户名
          </label>
          <input
            id="lg-user"
            ref={userRef}
            className="fld mt-1 w-full"
            style={{ height: 47 }}
            autoComplete="username"
            placeholder="例如：李娜"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />

          {mode === "register" ? (
            <>
              <label className="fld-label mt-3 block" htmlFor="rg-email">
                邮箱
              </label>
              <input
                id="rg-email"
                className="fld mt-1 w-full"
                style={{ height: 47 }}
                type="email"
                autoComplete="email"
                placeholder="用于找回与通知"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </>
          ) : null}

          <label className="fld-label mt-3 block" htmlFor="lg-pass">
            {mode === "login" ? "密码" : "密码（至少 8 位）"}
          </label>
          <input
            id="lg-pass"
            className="fld mt-1 w-full"
            style={{ height: 47 }}
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            placeholder="••••••••"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />

          <div className={error ? "mt-3 text-sm text-bad-ink" : "hidden"} role="alert">
            {error}
          </div>

          <button
            className="btn btn-block mt-4"
            style={{ height: 47, background: "#23694f" }}
            type="submit"
            disabled={busy}
          >
            {busy ? "处理中…" : mode === "login" ? "登录" : "注册并登录"}
          </button>
        </form>

        <p className="mt-3 text-sm">
          <button
            className="underline underline-offset-2"
            type="button"
            onClick={() => {
              setMode(mode === "login" ? "register" : "login");
              setError("");
            }}
          >
            {mode === "login" ? "还没有账号？注册一个" : "已有账号？返回登录"}
          </button>
        </p>

        <div className="mt-6 border-t border-line-soft pt-4">
          <div className="text-micro uppercase tracking-[0.1em] text-ink-3">演示账号 · 点击填充</div>
          {/* 三个账号做成可预览的卡片：点一下不仅填好用户名密码，
              右侧还会**当场演示这个身份能约到什么**。
              原来只有三个静态按钮 + 一句说明，看不出差异；
              差异恰恰是这个项目最想让人看到的东西。 */}
          <div className="mt-2 flex flex-wrap gap-2">
            {DEMO.map((d) => (
              <button
                key={d.username}
                type="button"
                className="btn btn-ghost btn-sm"
                aria-pressed={demo?.username === d.username}
                onClick={() => {
                  setMode("login");
                  setUsername(d.username);
                  setPassword(d.password);
                  setEmail("");
                  setError("");
                  setDemo(d);
                }}
              >
                {d.label}
              </button>
            ))}
          </div>
          <div className="mt-2 min-h-[64px] text-sm text-ink-3" aria-live="polite">
            {demo ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-1">
                <dt>资质</dt>
                <dd className="text-ink-2">{demo.certs}</dd>
                <dt>能约</dt>
                <dd className="text-ok-ink">{demo.can}</dd>
                <dt>会被拦</dt>
                <dd className={demo.cant === "无" ? "text-ink-2" : "text-bad-ink"}>{demo.cant}</dd>
              </dl>
            ) : (
              <div className="hint">
                选一个账号 —— 这里会说明它<b>能约到什么、会被哪条规则拦下</b>。
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
