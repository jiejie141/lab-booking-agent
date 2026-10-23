/* 极简 markdown：只认 **粗体** 与 `行内代码`。
 *
 * 为什么不用 marked / markdown-it：模型回复里唯一出现的标记形式就是这两种，
 * 而任何一个完整 markdown 库都会处理原始 HTML —— 那是把模型输出直接当 DOM
 * 插进页面的口子。这里**先转义再套标签**，顺序不能反。
 */

const ESCAPE: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function esc(value: unknown): string {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ESCAPE[ch] ?? ch);
}

export function mdInline(text: string): string {
  const safe = esc(text);
  // 先粗体后代码：粗体的定界符是 **，代码是单个反引号，
  // 若先处理代码，**a** 里的内容会先被包成 code 再匹配不到粗体。
  return safe
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
