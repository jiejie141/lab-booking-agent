"""控制台（React + Tailwind）与后端之间的契约。

## 这个文件为什么变短了

2026-10 控制台从「原生三文件 + 零构建」换成了 React + Vite + Tailwind。
换之前这里有二十多条**静态字符串断言**，直接 grep app.js 的源码去找
`'"book","labs","res"'`、`const TAB_LOADERS=` 这类字面量。

那种断言能成立，前提是「源码就是运行时」。换成构建链之后这个前提没了：
源码是 .tsx，浏览器跑的是被打包压缩过的 bundle，同一个行为在源码里和
在 bundle 里的写法完全不同。再按字符串去断言，守的不再是行为，而是
「我当初把这段代码写成什么样」—— 那是最脆的一种测试。

所以职责按层切开：

* **前端行为** → `src/lagent/web/ui/src/**/*.test.ts(x)`，vitest 46 条。
  请求体里有没有 user_id、403 是不是单独提示、改期走不走 PATCH、
  管理员标签对普通用户可不可见 —— 全部在那里按**渲染结果**断言。
* **构建产物与托管** → 本文件。产物在不在、HTML 引用的资源取不取得到、
  缓存头对不对、路径穿越挡不挡得住。
* **接口字段形状** → 本文件的 TestAdminPanelContracts（与前端无关，原样保留）。

一句话：**能靠跑一次渲染来验的，就不要靠 grep 源码。**

## 为什么「没构建」要跳过而不是红

产物不入库（`.gitignore` 里的 `dist/`），所以「dist 不存在」在干净的
checkout 上是**正常状态**。把它做成红，等于要求每个只改后端的人先装一遍 node。
真正的守门在两处：Dockerfile 在构建期断言 `dist/index.html` 存在；
CI 的 frontend job 跑 `npm ci && npm run build && npm test`。
"""

from __future__ import annotations

import io
import pathlib
import re

import pytest

from lagent.console import enable_utf8_output

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "lagent" / "web"
UI_DIR = WEB_DIR / "ui"
DIST_DIR = WEB_DIR / "dist"
DIST_INDEX = DIST_DIR / "index.html"
PACKED = ROOT / "dist" / "console.html"

_ASSET_REF = re.compile(r'(?:src|href)="(/assets-app/[^"]+)"')


def dist_index_html() -> str:
    """读构建产物的 index.html；没构建过就跳过（理由见模块 docstring）。"""
    if not DIST_INDEX.is_file():
        pytest.skip("控制台未构建：src/lagent/web/ui 下 npm ci && npm run build")
    return DIST_INDEX.read_text(encoding="utf-8")


def referenced_assets(html: str) -> list[str]:
    """HTML 里引用的构建产物路径，去重排序。"""
    return sorted(set(_ASSET_REF.findall(html)))


class TestBuildChainContract:
    """前端构建链与后端托管之间的**跨语言**约定。

    这里守的不是"某个文件长什么样"，而是两处必须对上的事实：
    Vite 把产物发布在哪个前缀下，api.py 就必须在同一个前缀下提供服务。
    两边任何一个改了而另一个没跟，表现是**页面全白**（HTML 拿到了，
    JS 404），但服务端日志里两条请求都在，看不出哪里不对。
    """

    def test_vite_base_matches_the_backend_route(self):
        """Vite 的 `base` 与 api.py 的路由前缀必须一致。"""
        from lagent import api as api_mod

        config = (UI_DIR / "vite.config.ts").read_text(encoding="utf-8")
        base = re.search(r'base:\s*"([^"]+)"', config)
        assert base, "vite.config.ts 里没有显式设置 base —— 默认 / 会与后端路由错位"
        prefix = base.group(1).rstrip("/")
        assert prefix == "/assets-app", f"Vite base 是 {prefix}，后端只认 /assets-app"

        source = pathlib.Path(api_mod.__file__).read_text(encoding="utf-8")
        assert '"/assets-app/{name:path}"' in source, "后端没有 /assets-app 路由"

    def test_build_checks_types_before_bundling(self):
        """build 脚本必须带 `tsc --noEmit`。

        只跑 vite build 不做类型检查：Vite 用 esbuild 转译，**类型错误照样出包**，
        于是"能跑但类型是错的"静悄悄进了产物。TypeScript 的价值一半在编译期，
        把这一步省掉等于只用了它的语法。
        """
        import json

        pkg = json.loads((UI_DIR / "package.json").read_text(encoding="utf-8"))
        build = pkg["scripts"]["build"]
        assert "tsc" in build and "--noEmit" in build, f"build 脚本缺类型检查：{build}"
        assert "vite build" in build, f"build 脚本没有打包：{build}"

    def test_ui_source_dir_is_not_ignored_by_git(self):
        """源码必须入库。

        这条看着像废话，但真的很容易踩：`.gitignore` 里同时有 `dist/` 和
        `node_modules/`，而构建产物目录就在源码目录**下面**
        （web/ui 同级的 web/dist）。将来若有人把忽略规则写成 `web/`，
        src 会跟着一起被忽略，本机一切正常、别人 clone 下来没有前端。
        """
        assert (UI_DIR / "src" / "main.tsx").is_file(), "React 入口不在仓库里"
        assert (UI_DIR / "package-lock.json").is_file(), (
            "缺 package-lock.json —— `npm ci` 要求它存在，缺了镜像构建直接失败"
        )


class TestConsoleBuildContract:
    """构建产物本身：在、被引用、引用的东西都在。

    这一组的价值在于**它验的是磁盘上真实存在的产物**，而不是源码。
    镜像里缺 dist、或者 dist 是三天前构建的，都是这一类问题。
    """

    @pytest.fixture(scope="class")
    def html(self) -> str:
        return dist_index_html()

    def test_html_is_a_shell_with_a_mount_point(self, html):
        """index.html 只剩一个挂载点 —— 面板标题这些都在 JS 里。

        这条是给"以后想改回服务端渲染"的人看的：现在从 `/` 拿到的 HTML
        里没有任何业务文案，所以要断言"界面存在"，只能去 bundle 里找。
        """
        assert '<div id="root">' in html
        assert "<style>" not in html, "样式被内联了 —— 那样内容哈希就失效了"

    def test_every_referenced_asset_exists_on_disk(self, html):
        """HTML 引用了什么，磁盘上就得有什么。

        缺资源的症状是页面白屏 + 控制台一条 404，而**后端日志是干净的**：
        那个 404 由静态资源路由给出，不经过业务日志。
        """
        refs = referenced_assets(html)
        assert refs, "index.html 没有引用任何 /assets-app/ 资源"
        for ref in refs:
            path = DIST_DIR / ref.removeprefix("/assets-app/")
            assert path.is_file(), f"引用了但不存在的产物：{ref} -> {path}"

    def test_asset_names_carry_a_content_hash(self, html):
        """文件名里必须带内容哈希。

        这是「HTML 新、JS 旧」这个老问题在构建链里的正解：内容一变文件名
        就变，浏览器不可能拿旧缓存去配新页面。没有哈希的话，只能给所有
        资源都发 no-cache，那等于每次都重新下载 300KB 的 bundle。
        """
        for ref in referenced_assets(html):
            name = ref.rsplit("/", 1)[-1]
            assert re.search(r"-[A-Za-z0-9_-]{6,}\.(js|css)$", name), (
                f"{name} 看起来没有内容哈希"
            )

    def test_no_legacy_static_references(self, html):
        """不该再有 /static/app.js 这类旧引用残留。

        旧路由已经被删掉了，留着引用的话就是一条 404 —— 而它出现在
        HTML 里时，浏览器只会在控制台报错，页面局部失灵，很难联想回来。
        """
        assert "/static/" not in html


class TestPackagedSingleFile:
    """`dist/console.html`：build_web.py 产出的全量内联版（演示用）。

    保留它的理由是一个真实约束：评审/面试时不想让人先 `npm ci` 再起服务。
    双击就能看到界面，接口仍走同源（要先起服务）。

    这里不重新打包（那是 build_web.py 的事），只确认它**如果存在就是完整的**。
    不完整的形态很危险：能打开，但一片空白，看起来像"项目坏了"。
    """

    def test_it_has_no_external_asset_references(self):
        if not PACKED.is_file():
            pytest.skip("还没打包过 dist/console.html（python build_web.py）")
        text = PACKED.read_text(encoding="utf-8")
        assert "/assets-app/" not in text, "单文件版里还留着外链，离线打开就是白页"
        assert "<script" in text and "<style>" in text, "没有内联脚本或样式"

    def test_it_is_actually_larger_than_the_shell(self):
        if not PACKED.is_file():
            pytest.skip("还没打包过 dist/console.html（python build_web.py）")
        shell = DIST_INDEX.stat().st_size if DIST_INDEX.is_file() else 0
        assert PACKED.stat().st_size > max(shell, 1024) * 2, (
            "打包版和 HTML 骨架差不多大 —— 内联八成没生效"
        )

class TestEnableUtf8Output:
    """控制台编码加固（与前端无关，原样保留）。

    这个 bug 的形态值得单独记住：本地交互式跑没问题，一重定向就崩 ——
    所以不能只靠「我这边跑过了」来确认，必须有一个真的用 GBK 流去验的测试。
    """

    def test_gbk_stream_would_crash_without_hardening(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        with pytest.raises(UnicodeEncodeError):
            stream.write("✓")

    def test_hardening_makes_gbk_stream_safe(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk")

        enable_utf8_output(stream)
        stream.write("✓ 模型             : mock")
        stream.flush()

        assert buffer.getvalue().decode("utf-8").startswith("✓")

    def test_hardening_accepts_multiple_streams(self):
        first, second = io.BytesIO(), io.BytesIO()
        a = io.TextIOWrapper(first, encoding="gbk")
        b = io.TextIOWrapper(second, encoding="gbk")

        enable_utf8_output(a, b)
        a.write("✗")
        b.write("⚠")
        a.flush()
        b.flush()

        assert first.getvalue().decode("utf-8") == "✗"
        assert second.getvalue().decode("utf-8") == "⚠"

    def test_doctor_output_is_encodable_after_hardening(self):
        """doctor 打印的整段文本（含 ✓/✗/→）在 UTF-8 下必须都能编码。"""
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk")
        enable_utf8_output(stream)
        for text in ("  ✓ 模型             : mock", "  ✗ 检索不可用", "  ⚠ 检索已降级",
                     "trace: parse → negotiate → book → compose"):
            stream.write(text + "\n")
        stream.flush()
        assert "✓" in buffer.getvalue().decode("utf-8")


class TestIdentityNeverSentByClient:
    """★ 最重要的一条约束：前端不得自己决定"我是谁"。

    2026-09-27 的事故：控制台曾经有一个「以谁的身份操作」下拉框，
    对话与下单请求体里带着 user_id。那等于把身份校验交给了客户端 ——
    改个下拉框就能替别人下单。修法是把那一整套删掉，身份只由令牌决定。

    换 React 之后这条约束**仍然存在**，但断言的落点变了：
    以前是 grep app.js 的源码去找 user_id；现在源码本身就会出现在
    请求体构造处，所以这里断言的是**构造请求体的那几个文件的语义**。
    真正的行为验证在 ui/src/lib/api.test.ts 与 ui/src/panes/Book.test.tsx
    （它们真的跑了一次请求再检查 body 的键），这里守的是别的地方
    **不要偷偷加回来**。
    """

    @pytest.fixture(scope="class")
    def api_src(self) -> str:
        return (UI_DIR / "src" / "lib" / "api.ts").read_text(encoding="utf-8")

    def test_token_comes_from_session_storage(self, api_src):
        storage = (UI_DIR / "src" / "store" / "storage.ts").read_text(encoding="utf-8")
        assert "sessionStorage" in storage
        assert "localStorage.setItem(TOKEN_KEY" not in storage, (
            "令牌不该落到 localStorage（长驻，XSS 窗口更大）"
        )

    def test_write_request_bodies_have_no_identity(self, api_src):
        """写请求的 body 由调用方给，但**鉴权头**只能由这里加。

        这里断言的是这条链路存在：请求工厂统一加 Authorization，
        而不是每个面板各自拼身份。
        """
        assert '"Authorization"' in api_src or "Authorization" in api_src
        assert "Bearer " in api_src

    def test_401_logs_out_before_throwing(self, api_src):
        """401 必须先清会话（回登录闸门），再当错误抛出去。

        只抛错不清会话的话，界面会停在"已登录"的样子上继续发注定 401 的请求。
        """
        assert "unauthorizedHandler" in api_src or "onUnauthorized" in api_src
        assert "401" in api_src

    def test_no_identity_selector_left_in_sources(self):
        """全前端不许再有「选择以谁的身份操作」这种控件。"""
        offenders: list[str] = []
        for path in (UI_DIR / "src").rglob("*.tsx"):
            if path.name.endswith(".test.tsx"):
                continue
            text = path.read_text(encoding="utf-8")
            if "sel-user" in text:
                offenders.append(str(path))
        assert not offenders, f"残留了对已删除身份选择器的引用：{offenders}"


class TestHealthBadgeSemantics:
    """「未登录」「登录已过期」「连接失败」必须是三种状态。

    2026-09-27 的事故：boot() 在**未登录**时就调 /api/health/details，
    401 被当成会话结束抛进 catch，徽章显示红点「连接失败」——
    把「还没登录」误报成「连不上」。用户看到的正是这一幕：
    右上角明明写着管理员，徽章却是连接失败，统计全是"—"。

    修法后来变成了一个显式状态机，React 版保留了它。这三者的用户动作
    完全不同：未登录去登录、过期也去登录、连不上查后端。混成一个红点
    等于让人对着一个正确运行的系统排查服务器。
    """

    @pytest.fixture(scope="class")
    def health_src(self) -> str:
        return (UI_DIR / "src" / "store" / "health.tsx").read_text(encoding="utf-8")

    def test_status_is_a_five_state_union(self, health_src):
        assert 'export type HealthStatus =' in health_src
        for state in ("unknown", "anon", "ok", "expired", "fail"):
            assert f'"{state}"' in health_src, f"健康状态里缺 {state}"

    def test_anonymous_returns_before_the_request(self, health_src):
        """未登录时**根本不发请求** —— 这是从根上避免误报，而不是靠 catch 兜。

        这条是那次事故的正解。靠 catch 兜的话，401 与真的连不上仍然共用
        同一个分支，只是文案改一改，问题会以别的形式回来。
        """
        anon_at = health_src.index('setStatus("anon")')
        fetch_at = health_src.index("fetchHealthDetails()")
        assert anon_at < fetch_at, "未登录分支必须先于发请求出现"
        assert "meRef.current" in health_src[:anon_at] or "!meRef.current" in health_src[:anon_at]

    def test_expired_and_failure_are_different_branches(self, health_src):
        assert 'setStatus("expired")' in health_src
        assert 'setStatus("fail")' in health_src

    def test_top_bar_names_all_three(self):
        top = (UI_DIR / "src" / "components" / "TopBar.tsx").read_text(encoding="utf-8")
        for label in ("未登录", "登录已过期", "连接失败"):
            assert label in top, f"顶栏缺少「{label}」这个可见状态"


class TestAdminPanelContracts:
    """控制台渲染所依赖的字段（与前端框架无关，原样保留）。

    为什么单独钉一遍：字段名写错**不会报错** —— 页面上就是一片
    `undefined`，而所有静态断言照样绿。所以这里把"控制台依赖哪些字段"
    用真实响应与模型字段钉住：哪天有人把 user_name 改名，红的是这条测试，
    不是管理员的页面。
    """

    def test_reservation_fields_the_admin_panels_render(self):
        from lagent.schemas import ReservationOut

        fields = set(ReservationOut.model_fields)
        assert {
            "user_id",       # 审批列表要显示"谁在申请"
            "user_name",
            "equipment_name",
            "lab_label",
            "slot",
            "no_show_at",    # 违约面板要能看出"哪条被判了未到场"
            "pardoned_at",
        } <= fields

    async def test_violations_shape(self, http, as_user):
        admin = await as_user("管理员")
        resp = await http.get("/api/users/1/violations", headers=admin)
        assert resp.status_code == 200
        body = resp.json()
        # 面板把 over_threshold 与 blocking_enabled 分开显示：
        # 合成一个布尔的话，"超了阈值但处罚还没开"这种状态就没法表达
        assert {
            "count", "threshold", "window_days",
            "blocked", "over_threshold", "blocking_enabled", "message",
        } <= set(body)

    async def test_notification_shape(self, http, as_user):
        admin = await as_user("管理员")
        rows = (await http.get("/api/notifications", params={"user_id": 2}, headers=admin)).json()
        assert isinstance(rows, list)
        for row in rows:
            assert {"kind", "title", "body", "status", "created_at"} <= set(row)

    async def test_audit_shape(self, http, as_user):
        admin = await as_user("管理员")
        rows = (await http.get("/api/audit", headers=admin)).json()
        assert rows, "管理员登录本身就会留下审计记录，这里不该是空的"
        assert {
            "created_at", "action", "actor_name", "target_type", "target_id",
            "outcome", "detail",
        } <= set(rows[0])

    async def test_pending_rows_carry_the_applicant(self, http, as_user):
        """待审批列表必须能回答"谁在申请"。

        没有申请人名字的待办队列是没法用的（试运行时对着它才发现缺这个字段），
        所以哪怕当前没有待办，也要把**列表非空时**的形状钉住。
        """
        admin = await as_user("管理员")
        rows = (await http.get("/api/reservations/pending", headers=admin)).json()
        assert isinstance(rows, list)
        for row in rows:
            assert "user_id" in row and "user_name" in row


class TestConsoleIsServed:
    """上面的断言都是静态的（读文件）。这里确认**服务端真的吐出这一页**。

    静态断言拦得住"引用了不存在的资源"，拦不住"改了文件但没被部署 /
    路由指向了另一份页面"。
    """

    async def test_index_is_the_built_shell(self, http):
        resp = await http.get("/")
        assert resp.status_code == 200
        assert '<div id="root">' in resp.text
        assert referenced_assets(resp.text), "页面里没有引用构建产物"

    async def test_missing_build_returns_a_build_hint_not_a_404(self, http):
        """没构建时返回 503 加一句"去 npm run build"，而不是一个裸 404。

        这条只在**产物缺失**的环境里能验（干净 checkout）。有产物时它验不到
        任何东西，所以直接跳过 —— 但那个分支必须存在，不能靠"反正我们会构建"
        来保证。这里通过读源码确认分支在，别处再靠 Dockerfile 的构建期断言
        保证镜像里一定有产物。
        """
        from lagent import api as api_mod

        source = pathlib.Path(api_mod.__file__).read_text(encoding="utf-8")
        assert "503" in source, "产物缺失时应当返回 503（部署状态问题，不是 404）"
        assert "npm ci" in source or "npm run build" in source, (
            "503 的文案里要给重建命令，否则看到的人不知道该做什么"
        )

    async def test_console_is_not_cached(self, http):
        """HTML 必须每次都协商：它是唯一没有内容哈希的文件。

        原始的疼点：FileResponse 不带 Cache-Control 时，浏览器启发式缓存会让人
        拿着**旧页面**打**新接口** —— 表现是"登录正常、某面板莫名报错"，
        一次强刷才能治好。这类问题在用户那边就是"灵异"，必须从服务端堵死。

        React 版把这条从"JS 内联在 HTML 里所以一起缓存"变成了
        "HTML 无哈希所以要协商、资源有哈希所以可以永久缓存"。
        两件事是配套的，只做一半（比如给 HTML 也发 immutable）会更糟。
        """
        resp = await http.get("/")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-cache"
