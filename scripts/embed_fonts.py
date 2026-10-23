"""把字体子集内嵌进前端样式表（src/lagent/web/ui/src/fonts.css）。

    python scripts/embed_fonts.py            # 用已下载的 woff2 重新生成
    python scripts/embed_fonts.py --fetch    # 先下载再生成

为什么要一个脚本，而不是"当初手动贴了一次"：
内嵌的 base64 是**生成物**。生成物如果没有生成脚本，下一个人就不知道
它从哪来、怎么更新、以及为什么是这三个文件。半年后要换字体时，
他只能对着一行 15000 字符的 base64 发呆。

为什么是 fonts.css 而不是 app.css：
2026-10 控制台从原生三文件换成了 React + Tailwind（src/lagent/web/ui），
样式入口变成 ui/src/styles.css，它在第一行 `@import "./fonts.css"`。
字体单独成文件是因为**整份都是生成物** —— 生成物和手写内容混在一个文件里，
就得靠锚点标记边界（旧版 embed_fonts.py 正是这么做的：找不到 ANCHOR 就退出）。
分开之后这个脚本直接覆盖整个文件，没有"手写部分"可以误伤。

为什么要把 @font-face 用 @import 引进来、而不是留在 styles.css 顶上：
Vite 会把 @import 展开进最终那份 CSS，构建产物里字体仍然是内嵌的 ——
这条链一直通到 dist/console.html（build_web.py 把整份 CSS 内联进 <style>）。
好处是 src/styles.css 里不再有 50000 多个字符的 base64，diff 终于看得懂。

为什么要内嵌，而不是走 CDN：
这个控制台有一条硬约束 —— dist/console.html 双击就能打开，
不依赖服务、不依赖网络。外链字体一断，页面的身份（衬线读数）就没了。

为什么只有拉丁子集：
汉字由系统字体承担（PingFang SC / 微软雅黑 / 宋体）。把 CJK 塞进
webfont 会让体积从 43 KB 涨到 1 MB 以上，而收益只是"字形统一"——
对内部工具来说这笔账不划算。所以 --font-display 的回退链里
中文那一档必须留着。
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "lagent" / "web"
# 2026-10：控制台换成 React（web/ui）之后，样式表的真实位置搬到了这里。
# 旧的 web/app.css 已随原生三文件一起删除，这个路径没有回退分支 ——
# 找不到文件就应该炸，而不是悄悄写到一个没人读的地方去。
CSS = WEB / "ui" / "src" / "fonts.css"
CACHE = ROOT / "var" / "fonts"

CDN = "https://cdn.jsdelivr.net/npm"

# (字体名, 字重, 远端文件, 本地名)
SOURCES = [
    ("Newsreader", 400, f"{CDN}/@fontsource-variable/newsreader@5/files/newsreader-latin-wght-normal.woff2", "newsreader-var.woff2"),
    ("Geist", 400, f"{CDN}/@fontsource-variable/geist@5/files/geist-latin-wght-normal.woff2", "geist-var.woff2"),
    ("Geist", 500, f"{CDN}/@fontsource-variable/geist@5/files/geist-latin-wght-normal.woff2", "geist-var.woff2"),
]

# 只留界面真正会用到的字符。范围写窄一点，体积就小一点。
UNICODES = "U+0020-007E,U+00A0-00FF,U+2000-206F,U+2190-2193,U+2212"

BANNER = (
    "/* 自动生成，勿手改 —— 见 scripts/embed_fonts.py。\n"
    "   三条 @font-face，拉丁子集，base64 内嵌：不依赖 CDN，\n"
    "   这样 dist/console.html 离线双击打开时字体也在。\n"
    "   汉字不在子集里，由系统字体承担（见 --font-sans / --font-display 的回退链）。\n"
    "   为什么内嵌、以及这个脚本必须是可复现的：见 README「字体」一节。 */\n"
)


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  已缓存 {dest.name}（{dest.stat().st_size} B）")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  下载 {url}")
    with urllib.request.urlopen(url, timeout=60) as r:
        dest.write_bytes(r.read())
    print(f"  写入 {dest.name}（{dest.stat().st_size} B）")
    return dest


def subset(src: Path, weight: int, out: Path) -> bytes:
    """实例化到单一字重再子集化。

    可变字体整条 wght 轴占了大头，而界面只用得到两档 ——
    直接内嵌变量字体等于把 90% 用不上的数据也塞进 HTML。

    为什么要显式关掉 recalcTimestamp：
    fontTools 默认在 save() 时把 head.modified 改写成**当前时间**，
    于是同一个输入每次跑出的字节都不同（实测三次：19316 / 19336 / 19404 B）。
    这会让 fonts.css 在每次重跑后都产生一个无意义的 diff，
    也让"生成物是否与脚本一致"这件事没法用 hash 校验。
    关掉之后实例化是纯函数：同样的输入 → 同样的字节。
    """
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    f = TTFont(src)
    if "fvar" in f:
        f = instancer.instantiateVariableFont(f, {"wght": weight}, inplace=False)
    # 两处都要设：instantiate 会派生一个新 TTFont，属性不保证继承。
    f.recalcTimestamp = False
    f.flavor = "woff2"
    tmp = out.with_suffix(".inst.woff2")
    f.save(tmp)

    # 走子进程而不是 import fontTools.subset：子集化的 CLI 是官方稳定入口，
    # 且能把 --layout-features 这类参数原样传下去，不依赖内部 API 的签名。
    args = [
        str(tmp), f"--unicodes={UNICODES}",
        "--layout-features=kern,liga,tnum,lnum",
        "--flavor=woff2", f"--output-file={out}",
    ]
    subprocess.run([sys.executable, "-m", "fontTools.subset", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp.unlink(missing_ok=True)
    return out.read_bytes()


def build(fetch_first: bool) -> int:
    faces = []
    for name, weight, url, local in SOURCES:
        src = CACHE / local
        if fetch_first or not src.exists():
            src = fetch(url, src)
        out = CACHE / f"{name.lower()}-{weight}-sub.woff2"
        data = subset(src, weight, out)
        b64 = base64.b64encode(data).decode("ascii")
        print(f"  {name} {weight}: {len(data)} B → base64 {len(b64)} B")
        faces.append(
            f"@font-face{{font-family:'{name}';font-style:normal;font-weight:{weight};"
            f"font-display:swap;src:url(data:font/woff2;base64,{b64}) format('woff2')}}"
        )

    CSS.parent.mkdir(parents=True, exist_ok=True)
    # 整个文件都是生成物，所以直接整体覆盖 —— 不需要锚点，也就没有
    # "锚点找不到时该不该继续"这个必须靠人判断的分支。
    CSS.write_text(BANNER + "\n".join(faces) + "\n", encoding="utf-8", newline="\n")
    kb = CSS.stat().st_size / 1024
    print(f"已写入 {CSS}（{kb:.1f} KB）")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="先联网下载原始 woff2")
    raise SystemExit(build(ap.parse_args().fetch))
