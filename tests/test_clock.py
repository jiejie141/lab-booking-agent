"""时间工具：``window_covering`` 的边界行为。

这个函数存在的唯一理由是"别再各写一遍 now±1h，然后被午夜干掉"。
所以就按它真出事的方式测：**跨午夜的时刻**必须也在窗口内、且窗口不跨日。
"""

from __future__ import annotations

import datetime as dt

import pytest

from lagent.clock import (
    minutes_between,
    overlaps,
    parse_time,
    window_covering,
)

DAY = dt.date(2026, 9, 23)


def at(hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(hour, minute, second))


@pytest.mark.parametrize(
    "when",
    [
        at(0, 10),      # 刚过午夜：不能把 start 算到"昨天 23:10"
        at(9, 30),
        at(12, 0),
        at(14, 30),
        at(23, 0),      # 跨午夜的起点
        at(23, 45),     # ★ 真出事过的时刻：以前 now+1h 会变成 00:45
        at(23, 59, 30),  # 一天最后一刻
    ],
)
def test_window_contains_now_and_never_crosses_the_day(when: dt.datetime) -> None:
    start, end = window_covering(when)
    now_t = when.time()
    assert start <= now_t <= end, f"{now_t} 不在 [{start}, {end}] 内"
    # 两个端点都是 time，天然同日；这里额外守住"日期确实还是今天"这个隐含前提：
    # 只有当 end 被夹到当日上限时才会出现 end < start 式的荒谬值，而那是 bug。
    assert start <= end


def test_late_night_window_stays_inside_today() -> None:
    """23:52 那个案例的直接回归：窗口必须仍落在今天，不能折回 00:xx。"""
    start, end = window_covering(at(23, 52))
    assert end.hour == 23, f"end={end} 折回了第二天，服务端会判凭证已过期"
    assert end >= dt.time(23, 52)
    assert start <= dt.time(23, 52)


def test_early_morning_window_does_not_reach_into_yesterday() -> None:
    """镜像用例：00:05 时 start 不能被算成昨天。"""
    start, end = window_covering(at(0, 5))
    assert start.hour == 0
    assert start <= dt.time(0, 5) <= end


def test_window_is_wider_when_there_is_room() -> None:
    """白天正常时段应当给出完整的 ±1 小时，而不是被夹成窄缝。"""
    start, end = window_covering(at(14, 30))
    assert start == dt.time(13, 30)
    assert end == dt.time(15, 30)


def test_parse_time_and_minutes_between_and_overlaps_still_work() -> None:
    """顺带钉住同模块里被领域层依赖的三个小函数（防重构时被误改）。"""
    assert parse_time("14:00:00") == dt.time(14, 0)
    assert parse_time("1400") == dt.time(14, 0)
    assert minutes_between(dt.time(14, 0), dt.time(16, 0)) == 120
    assert overlaps(dt.time(14, 0), dt.time(16, 0), dt.time(15, 0), dt.time(17, 0))
    # 首尾相接不算重叠 —— 这是预约系统最容易写错的边界
    assert not overlaps(dt.time(14, 0), dt.time(16, 0), dt.time(16, 0), dt.time(18, 0))


class TestFakeNowOverride:
    """时间覆盖（演示 / 培训 / 验收用）。

    为什么要有它：领域层能注入 ``now=``，但 **HTTP 接口一律取真实时间** ——
    于是闭馆时段做不了门禁与预约的演示（只能看到 permit_expired），
    培训与验收都得挑时间。

    ★ 最关键的一条是**不静默回退**：解析不出来就抛。
    演示时你以为自己在 14:00、实际跑在 23:00 的判定上，比直接报错难查得多。
    """

    async def test_empty_means_the_real_clock(self, isolated_db):
        """不设覆盖时，返回的就是真实时钟。

        ⚠️ 对照基准必须用**同一个时区**（``tz()``），不能用
        ``dt.datetime.now()``：``now_local()`` 是**配置时区**的墙上时间，
        而 naive 的 ``datetime.now()`` 是**宿主机**本地时间 —— 两者只在
        「宿主机时区恰好等于配置时区」时才相等。这台机器是 UTC+8 所以绿，
        CI 的 TZ=UTC，差值恰好 28800 − ε 秒，把这条测试打红了
        （2026-09-27，run 36303336159）。用 aware 的 ``datetime.now(tz())``
        之后，断言在任何宿主机上都成立：它量的只剩"两次取钟的间隔"。
        """
        from lagent.clock import now_local, tz

        # 与 now_local 同一个形状：aware → 取墙钟、剥掉 tzinfo。
        # 这样两边都是"配置时区的墙上时间"，差值只剩两次取钟的间隔，
        # 在任何宿主机时区下都成立。
        real = dt.datetime.now(tz()).replace(tzinfo=None)
        assert abs((now_local() - real).total_seconds()) < 120

    async def test_override_is_returned_verbatim(self, isolated_db, monkeypatch):
        from lagent.config import reset_settings_cache

        monkeypatch.setenv("LAB_FAKE_NOW", "2026-10-11 14:30")
        reset_settings_cache()
        from lagent.clock import now_local

        assert now_local() == dt.datetime(2026, 10, 11, 14, 30)

    async def test_iso_t_separator_also_works(self, isolated_db, monkeypatch):
        from lagent.config import reset_settings_cache

        monkeypatch.setenv("LAB_FAKE_NOW", "2026-10-11T09:05:00")
        reset_settings_cache()
        from lagent.clock import now_local

        assert now_local() == dt.datetime(2026, 10, 11, 9, 5)

    async def test_unparsable_value_raises_instead_of_falling_back(self, isolated_db, monkeypatch):
        """★ 不静默回退到真实时钟。"""
        import pytest

        from lagent.config import reset_settings_cache

        monkeypatch.setenv("LAB_FAKE_NOW", "明天下午两点")
        reset_settings_cache()
        from lagent.clock import now_local

        with pytest.raises(ValueError, match="LAB_FAKE_NOW"):
            now_local()
