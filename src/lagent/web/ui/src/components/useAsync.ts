import { useCallback, useEffect, useState } from "react";
import type { DependencyList } from "react";
import { sessionSignal } from "../lib/api";
import type { ApiError } from "../lib/api";

/* 面板级数据加载。八个面板里"加载 → 失败 → 重试"的形状完全一样，
   写八遍必然会有一处忘了处理断网（表现为骨架屏永远停在那里）。
   抽成这个 hook 之后，失败只有一种呈现：ErrBox，且网络/5xx 一定带重试按钮。
 */
export interface AsyncState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  reload: () => void;
  patch: (next: T) => void;
}

export function useAsync<T>(load: (signal: AbortSignal) => Promise<T>, deps: DependencyList): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [nonce, setNonce] = useState(0);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(load, deps);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    run(sessionSignal())
      .then((d) => {
        if (alive) {
          setData(d);
          setLoading(false);
        }
      })
      .catch((e: unknown) => {
        const err = e as Error & { status?: number };
        if (err.name === "AbortError") return; // 会话切换：安静地死掉
        if (!alive) return;
        setError(e as ApiError);
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [run, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { data, error, loading, reload, patch: setData };
}
