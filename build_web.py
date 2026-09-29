"""把拆分后的控制台打包回**单个 HTML 文件**。

    python build_web.py               -> dist/console.html
    python build_web.py out.html      -> 指定输出路径

为什么两套形态都要留：

  · **开发时用拆分版**（`src/<pkg>/web/index.html` + `app.css` + `app.js`）：
    能高亮、能被 lint、能被测试覆盖、diff 也小。这是"改得动"的前提。
  · **演示时用打包版**：一个文件双击就能看，不依赖任何服务与构建链 ——
    发给别人、塞进 U 盘、贴到聊天窗口都不需要额外说明。

打包规则与 `_split_web.py` 的拆分规则严格互逆，两者一起改。任何一边改了
标签写法，另一个就要跟着改 —— 所以这里不做正则猜测，直接按**固定字符串**替换，
匹配不到就报错退出（宁可炸，不要静默产出一个少了样式的页面）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LINK_TAG = '<link rel="stylesheet" href="/static/app.css">'
SCRIPT_TAG = '<script src="/static/app.js" defer></script>'


def find_web_dir() -> Path:
    """自动定位 `src/<pkg>/web/index.html`（两个项目都能用同一份脚本）。"""
    hits = list((ROOT / "src").glob("*/web/index.html"))
    if len(hits) != 1:
        raise SystemExit(f"找不到唯一的控制台入口，实际找到 {len(hits)} 个：{hits}")
    return hits[0].parent


def main() -> int:
    web = find_web_dir()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "console.html"

    html = (web / "index.html").read_text(encoding="utf-8")
    css = (web / "app.css").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")

    # 固定字符串替换，不做正则 —— 匹配不到直接失败，避免静默产出残页。
    if LINK_TAG not in html:
        raise SystemExit(f"index.html 里找不到 {LINK_TAG!r}，拆分/打包规则已经不一致了")
    if SCRIPT_TAG not in html:
        raise SystemExit(f"index.html 里找不到 {SCRIPT_TAG!r}，拆分/打包规则已经不一致了")

    html = html.replace(LINK_TAG, "<style>\n" + css.rstrip("\n") + "\n</style>", 1)
    html = html.replace(SCRIPT_TAG, "<script>\n" + js.rstrip("\n") + "\n</script>", 1)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8", newline="\n")

    kb = len(html.encode("utf-8")) / 1024
    print(f"已打包：{out}")
    print(f"  来源：{web}")
    print(f"  大小：{kb:.1f} KB（{html.count(chr(10)) + 1} 行）")
    print("  注意：单文件版里 /static/* 不再被引用；接口仍走同源，需先起服务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
