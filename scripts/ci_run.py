"""跑一条命令；失败时把关键输出抬进 GitHub 注解。

## 为什么需要它

Actions 的**原始日志需要鉴权** —— 无 token 取
``/actions/runs/<id>/logs`` 得到的是 ``403 Must have admin rights``。
而**注解（annotations）是公开可读的**。

于是没有 token 的环境里，"CI 红了"看得见，"为什么红"看不见。只能靠本地复现去猜，
而这恰恰在"本地跑得通、CI 跑不通"时最没有用 —— 那正是最需要看日志的情形。
（这不是假想：本项目就这么耗掉过一整轮排查，最后靠装一个对应版本的 Python
才复现出来。）

``::error::`` 是 Actions 的 workflow command，把它打印到 stdout，
GitHub 就会生成一条注解。这个包装把那几行关键输出挪进注解，
让失败原因在没有凭据的情况下也能拿到。

## 用法

    python scripts/ci_run.py -- python main.py eval
    python scripts/ci_run.py --match '^(FAILED|ERROR)' --tail 20 -- python -m pytest -q

退出码**原样透传**（CI 靠它判红绿），stdout/stderr 照常打印 ——
这个包装不隐藏任何日志，只是**额外**把尾巴抬到注解里。
"""

from __future__ import annotations

import re
import subprocess
import sys

# GitHub 注解的消息体里这几个字符有特殊含义，必须转义
_ESCAPE = (("%", "%25"), ("\r", "%0D"), ("\n", "%0A"))


def escape_annotation(text: str) -> str:
    """把一段文本转成能安全放进 ``::error::`` 的形式。"""
    for raw, encoded in _ESCAPE:
        text = text.replace(raw, encoded)
    return text


def pick_lines(stdout: str, stderr: str, *, match: str, tail: int) -> list[str]:
    """从输出里挑出要抬成注解的那几行。

    给了 ``match`` 就只挑命中的（例如 pytest 的 ``FAILED`` 行）——
    比盲取最后 N 行准得多：pytest 失败时最后几行往往是耗时统计。
    """
    lines = [line for line in (stdout + "\n" + stderr).splitlines() if line.strip()]
    if match:
        pattern = re.compile(match)
        lines = [line for line in lines if pattern.search(line)]
    return lines[-tail:] if tail > 0 else lines


def main() -> int:
    argv = sys.argv[1:]
    match, tail = "", 15

    # 手写解析而不是 argparse.REMAINDER：REMAINDER 在「选项 + -- + 命令」
    # 这种形态下行为很反直觉（第一个位置参数就把后面全吃掉），
    # 而这个脚本的使用者只有 CI 和调试时的人，不需要那层复杂度。
    if "--" in argv:
        split = argv.index("--")
        options, command = argv[:split], argv[split + 1 :]
    else:
        options, command = [], argv

    index = 0
    while index < len(options):
        flag = options[index]
        if flag == "--match" and index + 1 < len(options):
            match = options[index + 1]
            index += 2
        elif flag == "--tail" and index + 1 < len(options):
            tail = int(options[index + 1])
            index += 2
        else:
            print(f"::warning::忽略无法识别的选项 {flag!r}", flush=True)
            index += 1

    if not command:
        print("用法：python scripts/ci_run.py [--match REGEX] [--tail N] -- <命令...>")
        return 2

    label = " ".join(command)
    # 命令来自本仓库的 workflow，不是外部输入，所以这里不设 shell、直接 exec 列表形式。
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)

    if proc.returncode == 0:
        return 0

    # 关键：把原因抬成注解。没有这几行，失败在无 token 时就是不可读的。
    print(f"::error::命令失败（exit={proc.returncode}）：{label}")
    for line in pick_lines(proc.stdout, proc.stderr, match=match, tail=tail):
        print(f"::error::{escape_annotation(line)}")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
