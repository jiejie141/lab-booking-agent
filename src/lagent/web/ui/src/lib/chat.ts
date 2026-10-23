import { authHeader } from "./api";
import type { ChatRequest, ChatResponse } from "./types";

/* 流式对话。
 *
 * 用 fetch + response.body.getReader() 手工读 SSE，而不是 EventSource：
 *   ① EventSource 只支持 GET，消息体得塞进查询串；
 *   ② 它不能带自定义请求头 —— 而这里要带 Bearer；
 *   ③ 它没有 AbortController，也就没有"停止生成"。
 * 自己读流三个问题一起解决。
 *
 * 帧格式与服务端约定：一段 JSON 一行 data:，事件之间用空行分隔。
 *   {"type":"node","node":"parse","detail":"..."}   进度
 *   {"type":"done","data":{...ChatResponse}}         终态（唯一有结果的一帧）
 *   {"type":"error","detail":"..."}                  失败
 */

export type NodeEvent = { type: "node"; node: string; detail: string; elapsed_ms?: number };

export async function chatStream(
  payload: ChatRequest,
  onNode: (ev: NodeEvent) => void,
  signal: AbortSignal,
): Promise<ChatResponse> {
  const resp = await fetch("/api/agent/chat/stream", {
    method: "POST",
    headers: authHeader(),
    body: JSON.stringify(payload),
    signal,
  });

  if (resp.status === 401) throw new Error("登录状态已失效，请重新登录");
  if (!resp.ok) throw new Error("Agent 执行失败：HTTP " + resp.status);
  if (!resp.body) throw new Error("这个浏览器不支持流式响应");

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let done: ChatResponse | null = null;

  for (;;) {
    const { value, done: eof } = await reader.read();
    if (eof) break;
    buf += decoder.decode(value, { stream: true });

    // 只用**完整**的帧：最后一段可能是半条 JSON，留着等下一批字节。
    const frames = buf.split("\n\n");
    buf = frames.pop() ?? "";
    for (const frame of frames) {
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (!line) continue;
      const ev = JSON.parse(line.slice(6)) as
        | { type: "node"; node: string; detail: string; elapsed_ms?: number }
        | { type: "done"; data: ChatResponse }
        | { type: "error"; detail: string };
      if (ev.type === "node") onNode(ev);
      else if (ev.type === "done") done = ev.data;
      else if (ev.type === "error") throw new Error(ev.detail || "Agent 执行失败");
    }
  }

  // 服务端异常退出时流会正常 EOF 但**没有 done 帧**。
  // 不判这一下，界面会显示一条空回复，看起来像"模型什么都没说"。
  if (!done) throw new Error("流意外中断（没收到终态）");
  return done;
}

export const NODE_LABEL: Record<string, string> = {
  parse: "正在理解你的需求…",
  ask: "还缺几个条件，正在准备追问…",
  negotiate: "正在查可用时段与设备…",
  book: "正在下单…",
  cancel: "正在处理取消…",
  retrieve: "正在查规范文档…",
  compose: "正在组织回复…",
  degrade: "模型不可用，切到引导式表单…",
};
