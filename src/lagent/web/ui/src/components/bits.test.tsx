import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ErrBox } from "./bits";
import { ApiError } from "../lib/api";

/* 错误块的规矩：**断网和 5xx 必须给重试按钮**，4xx 不给。
   4xx 是"你填错了"，重试一次还是同样的错；没有按钮反而更快让人去看正文。
   这条来自真踩过的坑：网络失败时没有重试入口，用户唯一的出路是刷新整页，
   而那会把对话上下文一起清掉。 */

afterEach(cleanup);

function err(over: Partial<ApiError> & { status: number }) {
  return new ApiError(over.message ?? "出错了", over);
}

describe("ErrBox", () => {
  it("断网时给重试按钮", () => {
    render(<ErrBox error={err({ status: 0, network: true, message: "无法连接到服务器" })} onRetry={() => {}} />);
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });

  it("5xx 时给重试按钮", () => {
    render(<ErrBox error={err({ status: 503, message: "服务暂不可用" })} onRetry={() => {}} />);
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeNull();
  });

  it("403 不给重试 —— 换个时间点再点一次还是 403", () => {
    render(<ErrBox error={err({ status: 403, message: "没有权限" })} onRetry={() => {}} />);
    expect(screen.queryByRole("button", { name: "重试" })).toBeNull();
  });

  it("点重试真的触发了回调", () => {
    const onRetry = vi.fn();
    render(<ErrBox error={err({ status: 500, message: "服务内部错误" })} onRetry={onRetry} />);
    screen.getByRole("button", { name: "重试" }).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
