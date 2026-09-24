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
