"""控制台输出的编码加固。

中文 Windows 的控制台默认是 cp936(GBK)，而 ✓ ✗ ⚠ 这类字符**不在 GBK 里**。
后果很反直觉：交互式跑 `python main.py doctor` 一切正常（终端自己会兜），
一旦把输出重定向到文件或接进管道，print 就抛 UnicodeEncodeError 直接崩。

实测踩过：doctor 在 PowerShell 里 `| Out-File` 时挂在打印「✓ 模型」那一行。
这类 bug 的特点是「本地看着好好的，进 CI/管道就炸」，所以统一在入口处加固。
"""

from __future__ import annotations

import sys
from contextlib import suppress
from typing import TextIO


def enable_utf8_output(*streams: TextIO) -> None:
    """让标准输出在 GBK 控制台下也不炸。

    两步走：
      1. 把 Windows 控制台代码页切到 UTF-8 —— 成功则中文与符号都能正常显示；
      2. 把输出流的编码设为 utf-8 且 ``errors="replace"`` —— 兜底，
         即使第 1 步失败（例如不是控制台、是纯管道），也只会显示成替代字符，
         而不会抛异常中断整个命令。
    """
    if sys.platform == "win32":
        with suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)

    for stream in streams or (sys.stdout, sys.stderr):
        with suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
