import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { fetchHealthDetails } from "../lib/api";
import type { HealthDetails } from "../lib/types";
import { useApp } from "./app";
import { useToast } from "../components/Toaster";

/* 顶栏那三个徽章 + 首页四个读数，数据都来自 GET /api/health/details。
 *
 * 这条请求**不走 api() 的常规错误处理**（原来也是裸 fetch），因为
 * "未登录"和"登录已过期"要画成两种不同的颜色，而不是都当成一个红色报错：
 *   - 未登录：灰点，不报错（闸门本来就不需要登录）；
 *   - 401：黄点 + 回闸门（令牌过期了，得重新登）；
 *   - 连不上：红点（后端没起 / 断网）。
 * 把这三件事混成一条 toast，用户就分不清"是我没登录"还是"服务挂了"。
 */

export type HealthStatus = "unknown" | "anon" | "ok" | "expired" | "fail";

interface HealthState {
  health: HealthDetails | null;
  status: HealthStatus;
  refresh: () => Promise<void>;
}

const Ctx = createContext<HealthState | null>(null);

export function useHealth(): HealthState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useHealth 必须在 HealthProvider 内使用");
  return ctx;
}

export function HealthProvider({ children }: { children: ReactNode }) {
  const { me } = useApp();
  const { toast } = useToast();
  const [health, setHealth] = useState<HealthDetails | null>(null);
  const [status, setStatus] = useState<HealthStatus>("unknown");

  const meRef = useRef(me);
  meRef.current = me;

  const warnedRef = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    if (!meRef.current) {
      setStatus("anon");
      setHealth(null);
      return;
    }
    try {
      const h = await fetchHealthDetails();
      setHealth(h);
      setStatus("ok");
      // 降级原因只提示一次：每 30 秒弹一次同样的红条，人就会开始无视提示。
      if (h.retrieval_degraded_reason && warnedRef.current !== h.retrieval_degraded_reason) {
        warnedRef.current = h.retrieval_degraded_reason;
        toast("检索已降级：" + h.retrieval_degraded_reason, "err");
      }
    } catch (e) {
      const err = e as Error & { status?: number };
      if (err.name === "AbortError") return;
      if (err.status === 401) {
        // api 层已经触发过 signOut，这里只负责把顶栏画黄
        setStatus("expired");
        setHealth(null);
        return;
      }
      setStatus("fail");
      setHealth(null);
    }
  }, [toast]);

  useEffect(() => {
    void refresh();
  }, [refresh, me]);

  const value = useMemo<HealthState>(() => ({ health, status, refresh }), [health, status, refresh]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
