import { useEffect } from "react";
import type { ReactNode } from "react";
import { AppProvider } from "../store/app";
import { Toaster } from "../components/Toaster";
import { DialogHost } from "../components/Dialog";
import { rotateSession } from "../lib/api";
import { useApp } from "../store/app";
import type { LabOut, MeOut } from "../lib/types";

/* 组件测试的最小外壳。顺序必须和 App.tsx 一致：
   DialogHost 在外、Toaster 次之、AppProvider 在内 —— 面板同时要用
   对话框和提示条，反过来拿不到 context 会在第一次点击时炸。 */

export function makeMe(over: Partial<MeOut> = {}): MeOut {
  return {
    id: 2,
    username: "李娜",
    role: "user",
    certs: [],
    rank: 1,
    capabilities: [],
    ...over,
  };
}

export const ADMIN_CAPS = ["user.read.all", "role.manage"];

export function adminMe(over: Partial<MeOut> = {}): MeOut {
  return makeMe({ id: 1, username: "管理员", role: "admin", capabilities: ADMIN_CAPS, ...over });
}

/* 目录数据（实验室 + 设备）只有一个入口：app store 的 setLabs，
   而它只能在 Provider 内部拿到。所以用一个小夹具在挂载时灌进去，
   而不是给 Provider 开一个"测试专用"的 props —— 那种口子迟早会被产品代码用到。 */
function Seed({ labs }: { labs?: LabOut[] }) {
  const { setLabs } = useApp();
  useEffect(() => {
    if (labs) setLabs(labs);
  }, [labs, setLabs]);
  return null;
}

export function Wrap({
  me,
  labs,
  children,
}: {
  me: MeOut | null;
  labs?: LabOut[];
  children: ReactNode;
}) {
  return (
    <DialogHost>
      <Toaster>
        <AppProvider initialMe={me}>
          <Seed labs={labs} />
          {children}
        </AppProvider>
      </Toaster>
    </DialogHost>
  );
}

/* 会话世代（epoch）是模块级的：上一个用例里 rotateSession 过，
   下一个用例的 useAsync 里 sessionSignal() 已经被 abort —— 不重置的话
   第一个请求会安静地死掉，面板永远停在骨架屏，测试报"找不到元素"。
   每次渲染前重置一次，让每个用例都在一个干净的会话里开始。 */
export function resetSession(): void {
  rotateSession();
}

export function makeLabs(): LabOut[] {
  return [
    {
      id: 1,
      label: "光谱实验室",
      building: "A",
      floor: 3,
      room: "301",
      capacity: 8,
      note: "",
      open_hours: "08:00-22:00",
      equipment: [
        { id: 11, code: "SPEC-F7000", name: "荧光光谱仪", category: "光谱", max_hours: 4, status: "normal", requires_training: true, requires_approval: false },
        { id: 12, code: "SPEC-UV1900", name: "紫外分光光度计", category: "光谱", max_hours: 4, status: "maintenance", requires_training: true, requires_approval: false },
      ],
    },
  ];
}
