"""把 Vite 构建出来的控制台打包回**单个 HTML 文件**。

    python build_web.py               -> dist/console.html
    python build_web.py out.html      -> 指定输出路径

为什么两套形态都要留：

  · **开发时用构建版**（`src/<pkg>/web/ui/` + `npm run dev`）：能热更、
    能 typecheck、能被单测覆盖、diff 也小。这是"改得动"的前提。
  · **演示时用打包版**：一个文件双击就能看，不依赖任何服务与构建链 ——
    发给别人、塞进 U 盘、贴到聊天窗口都不需要额外说明。

2026-10-23 前端从"手写三文件"换成 React + Tailwind 之后，这个脚本的做法
从"按固定字符串替换两个 tag"改成了"扫描构建产物的 idx.html，把它引用的
assets 全部内联"。原因很直接：文件名带内容哈希（`app-C3Z45RQg.js`），
固定字符串在这个世界里的寿命只有一次构建。

规则仍然只有一个：**任何一处该内联却没找到，就报错退出**。
宁可炸，也不要静默产出一个少了样式或跑不起来的页面。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Vite 的产物引用形如：
#   <script type="module" crossorigin src="/assets-app/assets/app-C3Z45RQg.js"></script>
#   <link rel="stylesheet" crossorigin href="/assets-app/assets/index-CkIm-Tiw.css">
# 属性顺序由 Vite 决定，这里只认 src/href 的值，不认整行写法。
_SCRIPT_RE = re.compile(r'<script\b[^>]*\bsrc="(/assets-app/[^"]+)"[^>]*></script>')
_LINK_RE = re.compile(r'<link\b[^>]*\bhref="(/assets-app/[^"]+)"[^>]*>')


def find_dist_dir() -> Path:
    """自动定位 `src/<pkg>/web/dist/index.html`（两个项目都能用同一份脚本）。"""
    hits = list((ROOT / "src").glob("*/web/dist/index.html"))
    if len(hits) != 1:
        raise SystemExit(
            f"找不到唯一的控制台构建产物，实际找到 {len(hits)} 个：{hits}\n"
            f"  先在 src/*/web/ui 下执行：npm ci && npm run build"
        )
    return hits[0].parent


def _read_asset(dist: Path, url: str) -> str:
    """把 /assets-app/xxx 映射回 dist/xxx，读不到就失败。"""
    rel = url[len("/assets-app/"):]
    target = dist / rel
    if not target.is_file():
        raise SystemExit(f"index.html 引用了 {url}，但 {target} 不存在 —— 构建产物不完整？")
    return target.read_text(encoding="utf-8")


def main() -> int:
    dist = find_dist_dir()
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "console.html"

    html = (dist / "index.html").read_text(encoding="utf-8")

    scripts = _SCRIPT_RE.findall(html)
    links = _LINK_RE.findall(html)
    if not scripts:
        raise SystemExit(f"{dist / 'index.html'} 里没有任何 /assets-app/*.js 引用 —— 构建规则变了？")
    if not links:
        raise SystemExit(f"{dist / 'index.html'} 里没有任何 /assets-app/*.css 引用 —— 构建规则变了？")

    # 逐个内联。css 包成 <style>，js 包成 <script>。
    # 注意 js 必须保留 type="module" 的语义：Vite 产物里用的是 ESM 语法，
    # 剥掉 type 会让浏览器按脚本解析并直接语法报错。
    for url in links:
        css = _read_asset(dist, url)
        html = _LINK_RE.sub(
            lambda m, _u=url, _c=css: f"<style>\n{_c.rstrip()}\n</style>" if m.group(1) == _u else m.group(0),
            html,
            count=0,
        )
    for url in scripts:
        js = _read_asset(dist, url)
        html = _SCRIPT_RE.sub(
            lambda m, _u=url, _j=js: f'<script type="module">\n{_j.rstrip()}\n</script>' if m.group(1) == _u else m.group(0),
            html,
            count=0,
        )

    if "/assets-app/" in html:
        leftover = sorted(set(re.findall(r'"/assets-app/[^"]+"', html)))
        raise SystemExit(f"仍然有未内联的资源引用：{leftover} —— 打包规则没有覆盖到")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8", newline="\n")

    kb = len(html.encode("utf-8")) / 1024
    print(f"已打包：{out}")
    print(f"  来源：{dist}")
    print(f"  内联：{len(links)} 个样式、{len(scripts)} 个脚本")
    print(f"  大小：{kb:.1f} KB（{html.count(chr(10)) + 1} 行）")
    print("  注意：单文件版里 /assets-app/* 不再被引用；接口仍走同源，需先起服务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
