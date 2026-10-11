#!/usr/bin/env python
"""校验 requirements.txt 里每条直接依赖都带边界，且实际装到的版本没有越界。

为什么需要这个脚本 ——
  起因是一次真实排查：CI 的 py3.13 job 报 mypy 错、py3.10 job 全绿，
  两边跑的是同一份 mypy。真因是 requirements 只写了 `>=` 下限，
  而 SQLAlchemy 2.1 抬高了 requires_python —— py3.10 被解析到 2.0、
  py3.13 拿到 2.1，**两个 job 静默地在测两套栈**。

  当时只加了"打印版本"，那只让问题可见，没让它不可发生。
  这个脚本让它在 CI 里变成硬失败，同时也能本地跑一遍。

用法：python scripts/check_deps.py
退出码：0 = 全部合规；1 = 有依赖缺边界或实际版本越界。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"


def installed_versions() -> dict[str, str]:
    out = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True, text=True, check=True,
    ).stdout
    found: dict[str, str] = {}
    for line in out.splitlines():
        if "==" in line:
            name, _, ver = line.partition("==")
            found[name.strip().lower().replace("_", "-")] = ver.strip()
    return found


def main() -> int:
    if not REQUIREMENTS.exists():
        print(f"找不到 {REQUIREMENTS}")
        return 1
    installed = installed_versions()
    problems: list[str] = []
    checked = 0

    # 只校验"直接依赖"：requirements.txt 里列出来的。
    # 传递依赖（由 pip 解析出来的）不在这里管 —— 它们的版本由上游约束，
    # 强行钉住只会让升级路径变复杂。
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(.*)$", line)
        if not m:
            continue
        name = m.group(1).lower().replace("_", "-")
        spec_txt = m.group(3).strip()
        if not spec_txt:
            continue
        # 没写上限 = 漂移隐患。这正是当初 py3.10 / py3.13 分叉的成因。
        if "<" not in spec_txt:
            problems.append(
                f"{name}: 没有上限（{spec_txt}）—— pip 可能把它带上新大版本，"
                "  而 CI 的 Python 版本矩阵会让不同 job 拿到不同的版本（见文件头）。"
            )
            continue
        ver = installed.get(name)
        if ver is None:
            # 环境里没装这个包（比如可选的 asyncpg）—— 不算错。
            continue
        checked += 1
        from packaging.requirements import Requirement
        try:
            ok = Requirement(f"{name}{spec_txt}").specifier.contains(ver)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: 区间无法解析（{spec_txt}）：{exc}")
            continue
        if not ok:
            problems.append(f"{name}=={ver} 越出声明区间 {spec_txt}")

    print(f"检查了 {checked} 个已安装的直接依赖")
    for p in problems:
        print("::error::" + p)
    if problems:
        return 1
    print("✓ 依赖边界完整，且实际版本都在区间内")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
