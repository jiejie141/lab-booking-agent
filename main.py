"""统一入口。

    python main.py                    # 启动 Web 控制台
    python main.py doctor             # 环境自检
    python main.py loadtest           # 并发抢坑压测
    python main.py eval               # 跑评测集
    python main.py chat "明天下午两点想用荧光光谱仪两小时" --user 2

之所以要有这个文件：把 src 加进 sys.path 这件事集中在一处，
用户不必先 `pip install -e .` 或手工设 PYTHONPATH 就能跑起来。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

SUBCOMMANDS = {"doctor", "tools", "seed", "chat", "loadtest", "eval", "serve"}


def main() -> int:
    # 入口处先加固输出编码：中文 Windows 的 GBK 控制台重定向时会编不出 ✓/✗ 而崩。
    # 放在这里而不是各子命令里，是因为「python main.py」这条无参路径不走 cli。
    from lagent.console import enable_utf8_output

    enable_utf8_output()

    from lagent.cli import main as cli_main

    argv = sys.argv[1:]
    if argv and argv[0] in SUBCOMMANDS:
        return cli_main(argv)

    from lagent.server import serve

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
