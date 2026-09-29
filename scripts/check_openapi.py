"""把 OpenAPI schema 落盘，并在 CI 里检查它有没有漂移。

    python scripts/check_openapi.py          # 更新 docs/openapi.json
    python scripts/check_openapi.py --check  # 只比对，不一致就退出码 1

## 为什么需要这个

前后端契约目前是**手写**的：`schemas.py` 696 行人工维护，前端按它拼请求。
字段改名、必填改可选、枚举少一个值 —— 这些改动**编译期没有任何提示**，
而前端拿到的是 422 或一个静默取不到的字段。

真被击穿的样子是"上线之后才发现某个面板一直空着"。把 schema 落进仓库、
再让 CI 比对一次，这类改动就从一个看不见的行为差异，变成一次**明确的红灯**。

## 为什么落盘而不是存一个哈希

存哈希只能告诉你"变了"，落盘能告诉你"**哪里变了**" ——
diff 出来直接就是前后端要一起改的地方。文件不大，进版本库完全值得。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TARGET = ROOT / "docs" / "openapi.json"


def build_schema() -> dict:
    from lagent.api import app  # 延迟导入：要先把 src 加进 sys.path

    return app.openapi()


def main() -> int:
    check_only = "--check" in sys.argv
    schema = build_schema()
    text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    if check_only:
        if not TARGET.exists():
            print(f"✗ 缺少 {TARGET}。先跑 `python scripts/check_openapi.py` 生成一份。")
            return 1
        old = TARGET.read_text(encoding="utf-8")
        if old == text:
            n = len(schema.get("paths", {}))
            print(f"✓ OpenAPI 契约未漂移（{n} 条路径）")
            return 0
        print("✗ OpenAPI 契约已漂移。")
        print("  这通常意味着接口改了而前端还没跟上 —— 或者这是**有意**的改动。")
        print("  看清楚 diff，确认前端一起改完之后跑一次：")
        print("      python scripts/check_openapi.py")
        import difflib
        diff = list(difflib.unified_diff(
            old.splitlines(), text.splitlines(),
            fromfile="docs/openapi.json（仓库里）",
            tofile="docs/openapi.json（当前代码）", lineterm="", n=2))
        for line in diff[:80]:
            print("   " + line)
        if len(diff) > 80:
            print(f"   …（还有 {len(diff) - 80} 行）")
        return 1

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    changed = not TARGET.exists() or TARGET.read_text(encoding="utf-8") != text
    TARGET.write_text(text, encoding="utf-8", newline="\n")
    print(f"{'已更新' if changed else '无变化'}：{TARGET}")
    print(f"  路径 {len(schema.get('paths', {}))} 条 · "
          f"schema {len(schema.get('components', {}).get('schemas', {}))} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
