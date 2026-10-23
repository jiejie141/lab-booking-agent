import { useEffect, useState } from "react";
import { apiGet } from "../lib/api";
import type { UserOut } from "../lib/types";
import { useApp } from "../store/app";

/* 需要"选一个用户"的四个面板（违约 / 通知 / 门禁核验 / 手工签发）
   用的是同一个下拉。原来每个面板各自 fillUserSelect 一遍，
   结果只要目录还没加载完，第一个被打开的面板拿到的是空列表，
   而且不会再刷 —— 下拉框永远只有"（选一个用户）"。

   这里统一成：先看 context 里有没有，没有就自己去拉一次。
   拉失败就保持空列表并让调用方把提示写出来，不要静默成"这个人不存在"。 */
export function useUserOptions(): UserOut[] {
  const { users, setUsers } = useApp();
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (users.length || loaded) return;
    let alive = true;
    setLoaded(true);
    apiGet<UserOut[]>("/api/users?limit=200")
      .then(({ data }) => {
        if (alive) setUsers(data, data.length);
      })
      .catch(() => {
        // 无权限或断网：保持空列表。UI 会显示"选一个用户"而不是假数据。
      });
    return () => {
      alive = false;
    };
  }, [users.length, loaded, setUsers]);

  return users;
}
