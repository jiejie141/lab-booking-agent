import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { bindAuth, rotateSession, sessionSignal } from "../lib/api";
import type { EquipmentOut, LabOut, MeOut, UserOut } from "../lib/types";
import { readTheme, tokenStore, writeTheme } from "./storage";
import type { Theme } from "./storage";

interface AppState {
  me: MeOut | null;
  token: string | null;
  theme: Theme;
  /* 对话的 session_id。换新会话 = 换这个值，而不是清空服务端上下文
     （服务端按 session_id 存历史，换一个名字就是一条新线）。 */
  sessionId: string;
  labs: LabOut[];
  /* 设备按 id 索引：下单下拉和门禁面板都要按 id 反查，
     而且两处都必须拿到**同一份**目录 —— 否则会出现"这个设备在列表里
     有、在下拉里没有"。 */
  equipment: Map<number, EquipmentOut>;
  users: UserOut[];
  userTotal: number;
  /* 能力点判定。角色字符串只用于显示，一切功能开关走 capabilities ——
     这样后端加一个角色时，前端不需要改任何 if。 */
  can: (capability: string) => boolean;
  isAdmin: boolean;
  isSysadmin: boolean;
  signal: AbortSignal;
  setMe: (me: MeOut | null) => void;
  signIn: (token: string, me: MeOut) => void;
  signOut: () => void;
  toggleTheme: () => void;
  newSession: () => void;
  setLabs: (labs: LabOut[]) => void;
  setUsers: (users: UserOut[], total: number) => void;
}

const Ctx = createContext<AppState | null>(null);

export function useApp(): AppState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp 必须在 AppProvider 内使用");
  return ctx;
}

function randomSession(): string {
  // crypto.randomUUID 在非 HTTPS 的局域网地址上不可用（不是安全上下文）——
  // 这个控制台经常被人在内网用 http:// 打开，所以留一条退路。
  const raw =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : Math.random().toString(36).slice(2, 10);
  return "web-" + raw;
}

export function AppProvider({ children, initialMe }: { children: ReactNode; initialMe: MeOut | null }) {
  const [me, setMeState] = useState<MeOut | null>(initialMe);
  const [token, setToken] = useState<string | null>(() => tokenStore.get());
  const [theme, setTheme] = useState<Theme>(() => readTheme());
  const [sessionId, setSessionId] = useState<string>(() => randomSession());
  const [labs, setLabsState] = useState<LabOut[]>([]);
  const [equipment, setEquipment] = useState<Map<number, EquipmentOut>>(() => new Map());
  const [users, setUsersState] = useState<UserOut[]>([]);
  const [userTotal, setUserTotal] = useState(0);
  const [signal, setSignal] = useState<AbortSignal>(() => sessionSignal());

  const tokenRef = useRef<string | null>(token);
  tokenRef.current = token;

  const signOutInternal = useCallback(() => {
    const next = rotateSession();
    tokenStore.set(null);
    tokenRef.current = null;
    setToken(null);
    setSignal(next);
    setMeState(null);
    setLabsState([]);
    setEquipment(new Map());
    setUsersState([]);
    setUserTotal(0);
    setSessionId(randomSession());
  }, []);

  // api 模块不认识 React，所以这里把两个能力注入进去：
  // 取 token、以及"401 时该怎么办"。
  useMemo(() => {
    bindAuth(() => tokenRef.current, signOutInternal);
    return null;
  }, [signOutInternal]);

  const setMe = useCallback((next: MeOut | null) => {
    setMeState(next);
    if (!next) signOutInternal();
  }, [signOutInternal]);

  const signIn = useCallback((nextToken: string, nextMe: MeOut) => {
    tokenStore.set(nextToken);
    tokenRef.current = nextToken;
    setToken(nextToken);
    setMeState(nextMe);
    // 登录成功后换一条干净的请求世代：登录前那些"未登录"的失败请求
    // 不该在新会话里继续报错。
    setSignal(rotateSession());
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme((cur) => {
      const next: Theme = cur === "dark" ? "light" : "dark";
      writeTheme(next);
      return next;
    });
  }, []);

  const newSession = useCallback(() => setSessionId(randomSession()), []);

  const setLabs = useCallback((next: LabOut[]) => {
    setLabsState(next);
    setEquipment((prev) => {
      const merged = new Map(prev);
      next.forEach((lab) => lab.equipment.forEach((e) => merged.set(e.id, e)));
      return merged;
    });
  }, []);

  const setUsers = useCallback((next: UserOut[], total: number) => {
    setUsersState(next);
    setUserTotal(total);
  }, []);

  const can = useCallback(
    (capability: string) => !!me && me.capabilities.includes(capability),
    [me],
  );

  const value = useMemo<AppState>(
    () => ({
      me,
      token,
      theme,
      sessionId,
      labs,
      equipment,
      users,
      userTotal,
      can,
      isAdmin: can("user.read.all"),
      isSysadmin: can("role.manage"),
      signal,
      setMe,
      signIn,
      signOut: signOutInternal,
      toggleTheme,
      newSession,
      setLabs,
      setUsers,
    }),
    [me, token, theme, sessionId, labs, equipment, users, userTotal, can, signal, setMe, signIn, signOutInternal, toggleTheme, newSession, setLabs, setUsers],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
