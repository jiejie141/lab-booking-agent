"""登录限流与失败锁定（P1-7）。

要证明的不是"能返回 429"，而是四件具体的事：

1. **连续失败到阈值就锁**，而且被锁期间**连正确口令也进不去** ——
   否则限流只是让爆破变慢，而不是让它停下来；
2. **锁是"地址 + 账号"维度的**：只按用户名锁的话，任何人都能把别人的
   账号锁死，那个"攻击"比猜口令更简单也更有效；
3. **成功一次就清零**：用户中途试对了一次，不该在之后被莫名其妙锁住；
4. **被锁的请求不写审计** —— 否则攻击者每秒发几十个请求，就能帮你把
   审计表撑爆（触发锁定的那几次失败本身已经在审计里了，证据链完整）。

另外补了一组 :class:`LoginThrottle` 的纯单元用例：锁到期后要能恢复，
且**到期后旧的失败记录必须作废** —— 否则用户刚等到解锁，
又因为窗口里还留着旧记录被立刻再锁一次，等于永远进不来。
"""

from __future__ import annotations

import httpx
import pytest

from lagent.api import create_app
from lagent.audit import ACTION_LOGIN_FAILED
from lagent.ratelimit import LoginThrottle

ZHANGWEI = "张伟"

# 与 config 里的默认值保持一致；用例按它来数次数
MAX_ATTEMPTS = 5


async def try_login(http, username: str, password: str) -> httpx.Response:
    return await http.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


async def wrong_password(http, username: str = ZHANGWEI) -> httpx.Response:
    return await try_login(http, username, "definitely-not-the-password")


async def audit_rows(http, headers) -> list[dict]:
    resp = await http.get("/api/audit", headers=headers)
    assert resp.status_code == 200, resp.text
    return [row for row in resp.json() if row["action"] == ACTION_LOGIN_FAILED]


# ===========================================================================
# 一、锁定行为
# ===========================================================================
class TestLockout:
    async def test_repeated_failures_eventually_lock_the_account(self, http):
        """★ 连续失败到阈值 → 429，而不是一直 401。"""
        for _ in range(MAX_ATTEMPTS):
            resp = await wrong_password(http)
            assert resp.status_code == 401, resp.text

        locked = await wrong_password(http)
        assert locked.status_code == 429, locked.text
        assert "秒后再试" in locked.json()["detail"]

    async def test_the_lock_blocks_even_the_right_password(self, http):
        """★ 被锁期间正确口令也进不去 —— 这条是"限流"和"减速"的分界线。"""
        for _ in range(MAX_ATTEMPTS + 1):
            await wrong_password(http)

        resp = await try_login(http, ZHANGWEI, "zhangwei@123")
        assert resp.status_code == 429, resp.text

    async def test_retry_after_header_tells_the_client_how_long(self, http):
        """429 要带 Retry-After，否则客户端只能瞎重试。"""
        for _ in range(MAX_ATTEMPTS + 1):
            await wrong_password(http)
        resp = await wrong_password(http)
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After", "").isdigit()
        assert int(resp.headers["Retry-After"]) > 0

    async def test_a_successful_login_clears_the_counter(self, http):
        """★ 中途试对了就不该再被锁。

        不清零的话，用户"输错三次 → 想起来 → 登进去"之后，下一次手滑
        就会在完全没做错什么的情况下被锁住。
        """
        for _ in range(MAX_ATTEMPTS - 1):
            assert (await wrong_password(http)).status_code == 401
        assert (await try_login(http, ZHANGWEI, "zhangwei@123")).status_code == 200

        # 再错同样多次也不会立刻锁
        for _ in range(MAX_ATTEMPTS - 1):
            assert (await wrong_password(http)).status_code == 401
        assert (await try_login(http, ZHANGWEI, "zhangwei@123")).status_code == 200

    async def test_another_account_is_not_affected(self, http):
        """锁是"地址 + 账号"维度的：锁了张伟不影响李娜。

        反过来（只按账号锁）的后果是：任何人都能把别人的账号锁死。
        """
        for _ in range(MAX_ATTEMPTS + 1):
            await wrong_password(http, ZHANGWEI)
        assert (await wrong_password(http, ZHANGWEI)).status_code == 429

        assert (await try_login(http, "李娜", "lina@123")).status_code == 200

    async def test_locked_requests_do_not_flood_the_audit_log(self, http, as_user):
        """★ 被锁的请求不写审计。

        否则"每秒发 50 个被锁的登录请求"就等于给了攻击者一个帮你撑爆
        审计表的开关 —— 而审计是事后追查的唯一依据。
        """
        admin = await as_user("管理员")
        for _ in range(MAX_ATTEMPTS + 1):
            await wrong_password(http)
        before = len(await audit_rows(http, admin))

        for _ in range(20):
            assert (await wrong_password(http)).status_code == 429

        after = len(await audit_rows(http, admin))
        assert after == before, f"被锁的 20 次请求往审计里写了 {after - before} 条"

    async def test_the_attempts_that_caused_the_lock_are_recorded(self, http, as_user):
        """触发锁定的那几次失败**必须**在审计里 —— 证据链要完整。"""
        for _ in range(MAX_ATTEMPTS):
            await wrong_password(http)
        admin = await as_user("管理员")
        rows = await audit_rows(http, admin)
        assert len(rows) == MAX_ATTEMPTS
        assert all(row["actor_name"] == ZHANGWEI for row in rows)

    async def test_disabling_the_throttle_restores_plain_401(self, http):
        """限流可关（本地演示/压测）。关掉之后失败应当是普通的 401。"""
        http.app.state.login_throttle.max_attempts = 0
        for _ in range(MAX_ATTEMPTS + 5):
            assert (await wrong_password(http)).status_code == 401


# ===========================================================================
# 二、纯单元：锁的到期与恢复
# ===========================================================================
class TestLoginThrottleUnit:
    def test_disabled_when_max_attempts_is_zero(self):
        throttle = LoginThrottle(0)
        assert throttle.enabled is False
        for _ in range(100):
            assert throttle.check("1.2.3.4#u")[0] is True
            throttle.record_failure("1.2.3.4#u")

    def test_locks_after_the_configured_number_of_failures(self):
        throttle = LoginThrottle(3, window_seconds=60, lock_seconds=30)
        for _ in range(3):
            assert throttle.check("k")[0] is True
            throttle.record_failure("k")
        allowed, retry_after = throttle.check("k")
        assert allowed is False
        assert retry_after == 30

    def test_a_different_key_is_unaffected(self):
        throttle = LoginThrottle(1, lock_seconds=30)
        throttle.record_failure("ip#alice")
        assert throttle.check("ip#alice")[0] is False
        assert throttle.check("ip#bob")[0] is True

    def test_expired_lock_lets_the_user_back_in(self, monkeypatch):
        """★ 锁到期后要能恢复 —— 而且是**真的**恢复。"""
        clock = [0.0]
        throttle = LoginThrottle(2, window_seconds=60, lock_seconds=30)
        monkeypatch.setattr(throttle, "_clock", lambda: clock[0])

        for _ in range(2):
            throttle.record_failure("k")
        assert throttle.check("k")[0] is False

        clock[0] = 31.0  # 锁过期
        assert throttle.check("k")[0] is True, "锁到期了还不让进，等于永久封禁"

    def test_expired_lock_also_discards_the_old_failures(self, monkeypatch):
        """★ 到期后旧的失败记录必须一并作废。

        否则用户刚等到解锁，check 里那段"窗口内还有 N 次失败"会**立刻**
        把他再锁一次 —— 于是他永远进不来，而日志上看起来是"他又失败了"。
        """
        clock = [0.0]
        throttle = LoginThrottle(2, window_seconds=1000, lock_seconds=30)
        monkeypatch.setattr(throttle, "_clock", lambda: clock[0])

        for _ in range(2):
            throttle.record_failure("k")
        assert throttle.check("k")[0] is False

        clock[0] = 31.0
        assert throttle.check("k")[0] is True
        # 解锁后连着试两次失败才会再锁，而不是一次就锁
        throttle.record_failure("k")
        assert throttle.check("k")[0] is True
        throttle.record_failure("k")
        assert throttle.check("k")[0] is False

    def test_reset_clears_everything(self):
        throttle = LoginThrottle(1, lock_seconds=30)
        throttle.record_failure("k")
        assert throttle.check("k")[0] is False
        throttle.reset("k")
        assert throttle.check("k")[0] is True
        assert throttle.locked_keys() == 0

    def test_the_key_space_does_not_grow_forever(self):
        """与 SlidingWindowLimiter 同一个坑：不清理就是内存泄漏。"""
        throttle = LoginThrottle(5, window_seconds=1, max_keys=8)
        for i in range(200):
            throttle.record_failure(f"k{i}")
        assert len(throttle._failures) <= 200  # 有清理，不会无限增长
        throttle.reset()
        assert throttle.locked_keys() == 0


# ===========================================================================
# 三、配置确实接到了行为上
# ===========================================================================
class TestConfigurationIsWired:
    def test_defaults_are_conservative(self):
        """默认值要能挡住脚本试口令，又不至于把正常人锁在外面。"""
        from lagent.config import Settings

        settings = Settings()
        assert settings.login_max_attempts >= 3
        assert settings.login_lock_seconds >= 60

    async def test_the_app_builds_a_throttle_from_settings(self, isolated_db):
        """阈值来自配置，不是写死在代码里 —— 院系要能自己调。"""
        import os

        os.environ["LAB_LOGIN_MAX_ATTEMPTS"] = "7"
        try:
            from lagent.config import reset_settings_cache

            reset_settings_cache()
            app = create_app()
            async with app.router.lifespan_context(app):
                throttle = app.state.login_throttle
                assert throttle.max_attempts == 7
        finally:
            os.environ.pop("LAB_LOGIN_MAX_ATTEMPTS", None)
            from lagent.config import reset_settings_cache

            reset_settings_cache()

    def test_out_of_range_values_are_rejected(self):
        from pydantic import ValidationError

        from lagent.config import Settings

        with pytest.raises(ValidationError):
            Settings(login_max_attempts=-1)
        with pytest.raises(ValidationError):
            Settings(login_lock_seconds=0)
