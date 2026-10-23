import { useCallback, useEffect, useState } from "react";
import { TopBar } from "./TopBar";
import { Chat } from "./Chat";
import { DataCard } from "./DataCard";
import type { TabKey } from "./DataCard";
import { Stats, Welcome } from "./Welcome";
import { useApp } from "../store/app";

/* 主界面骨架：顶栏之外的两段（欢迎区 + 两栏）。
   顶栏放在 App 里，因为它要在闸门页也显示"连接中…"那半条状态。 */

export function Console() {
  const { isAdmin } = useApp();
  const [tab, setTab] = useState<TabKey>("labs");
  const [bookNonce, setBookNonce] = useState(0);

  // 从 laB 页跳到"直接预约"之类的动作：[data-jump]
  const jump = useCallback((next: string) => {
    const key = next as TabKey;
    if (key === "users" || key === "appr" || key === "viol" || key === "notif" || key === "audit" || key === "gate") {
      if (!isAdmin) return;
    }
    setTab(key);
    document.querySelector(".card")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [isAdmin]);

  // 数字快捷键 1/2/3/4 → book/labs/res/kb。输入类控件聚焦时不生效
  // —— 否则在输入框里敲个日期就跳页了。
  useEffect(() => {
    const map: Record<string, TabKey> = { "1": "book", "2": "labs", "3": "res", "4": "kb" };
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      const el = e.target as HTMLElement | null;
      if (el && el.matches("input,textarea,select,button")) return;
      const next = map[e.key];
      if (!next) return;
      e.preventDefault();
      setTab(next);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  return (
    <>
      {/* 顶栏只在进入工作台之后出现：闸门页是独立的一屏，
          在登录前就摆一排"未登录"的徽章是噪音。 */}
      <TopBar />
      <main className="wrap">
      <Welcome onJump={jump} />
      <Stats />
      <div className="cols">
        <Chat onBooked={() => setBookNonce((n) => n + 1)} />
        <DataCard tab={tab} onTab={setTab} onBooked={() => setBookNonce((n) => n + 1)} />
      </div>
      {/* bookNonce 只是用来提醒 DataCard 里那几块"有动静了"；
          真正的刷新由各面板自己的 reload 负责，这里不重复拉。 */}
      <span hidden data-book-nonce={bookNonce} />
      </main>
    </>
  );
}
