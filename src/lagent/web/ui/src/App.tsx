import { useEffect, useState } from "react";
import { fetchMe } from "./lib/api";
import { tokenStore } from "./store/storage";
import { AppProvider, useApp } from "./store/app";
import { HealthProvider } from "./store/health";
import { Toaster } from "./components/Toaster";
import { DialogHost } from "./components/Dialog";
import { Gate } from "./components/Gate";
import { Console } from "./components/Console";

/* 启动流程（与原来的 boot() 一一对应）：
 *
 *   1. 没有 token → 直接进闸门，**不发任何请求**；
 *   2. 有 token → 用 /api/auth/me 验证它是不是还活着，活着就直接进工作台；
 *   3. 检查期间显示一个最小骨架，而不是先闪一下闸门再跳走
 *      （那一下闪烁会让人以为"我的登录丢了"）。
 *
 * 令牌存在 sessionStorage 里，所以"刷新页面还登着"只在同一个标签页内成立 ——
 * 关掉标签页等于登出，这是刻意的。
 */
function Shell() {
  const { me, setMe } = useApp();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    let alive = true;
    const hadToken = !!tokenStore.get();
    if (!hadToken) {
      setChecking(false);
      return;
    }
    fetchMe()
      .then((fresh) => {
        if (alive) setMe(fresh);
      })
      .catch(() => {
        /* 令牌过期 / 后端没起：都当作"没登录"，闸门会给出下一步 */
      })
      .finally(() => {
        if (alive) setChecking(false);
      });
    return () => {
      alive = false;
    };
    // 只在挂载时跑一次：登录成功之后不需要再验一遍
  }, [setMe]);

  if (checking) return <Splash />;
  return me ? <Console /> : <Gate />;
}

function Splash() {
  return (
    <div className="flex min-h-[100dvh] items-center justify-center">
      <div className="flex items-center gap-3 text-ink-3">
        <span className="flex h-9 w-9 items-center justify-center rounded-md bg-action font-display text-[13px] text-action-ink">
          LB
        </span>
        <span className="text-sm">正在确认登录状态…</span>
      </div>
    </div>
  );
}

export function App() {
  return (
    <DialogHost>
      <Toaster>
        <AppProvider initialMe={null}>
          <HealthProvider>
            <Shell />
          </HealthProvider>
        </AppProvider>
      </Toaster>
    </DialogHost>
  );
}
