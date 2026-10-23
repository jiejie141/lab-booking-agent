/* 主题与令牌的落盘位置 —— 两个 key 刻意放在**不同的 storage**。
 *
 * 主题进 localStorage：它是偏好，"我上次用深色" 应该跟着这台机器走。
 * 令牌进 sessionStorage：它是凭证，"关掉标签页即失效" 是我们想要的
 * 默认行为。两者的生命周期本来就不同，用同一个 storage 只是省事，
 * 代价是令牌会在共享电脑上活到下次手动登出。
 *
 * 都要 try/catch：隐私模式 / 禁用第三方存储时，访问 sessionStorage
 * 会直接抛异常。不兜住的话第一行就白屏。
 */

const THEME_KEY = "lagent-theme-v2";
const TOKEN_KEY = "lagent-token";

export type Theme = "light" | "dark";

export const tokenStore = {
  get(): string | null {
    try {
      return sessionStorage.getItem(TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(value: string | null): void {
    try {
      if (value) sessionStorage.setItem(TOKEN_KEY, value);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch {
      /* 存不了就算了：内存里的 token 仍然可用，只是刷新后要重新登录 */
    }
  },
};

export function readTheme(): Theme {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    /* 读不到就用默认值 */
  }
  // 默认浅色，**不跟随系统**：这个控制台经常投屏/截图，
  // 系统切到深色时界面突然变黑会让演示翻车。
  return "light";
}

export function writeTheme(theme: Theme): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* 同上 */
  }
  document.documentElement.setAttribute("data-theme", theme);
}
