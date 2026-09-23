"""域层纯函数：区间运算、窗口减法、六条约束的判定。"""

from __future__ import annotations

import datetime as dt

import pytest

from lagent.clock import minutes_between, overlaps
from lagent.domain.availability import (
    EquipmentView,
    busy_intervals,
    evaluate,
    free_windows,
    matches_target,
    open_window,
    place_slots,
)
from lagent.models import Equipment, Laboratory, Reservation, User
from lagent.schemas import Requirement

WEEKDAY = dt.date(2026, 9, 24)   # 周四
SATURDAY = dt.date(2026, 9, 26)  # 周六


def make_lab(**over):
    data = dict(
        id=1,
        building="分析楼",
        floor=3,
        room="301",
        capacity=6,
        open_hours={"weekday": ["08:00", "22:00"], "weekend": ["09:00", "18:00"]},
    )
    data.update(over)
    return Laboratory(**data)


def make_equipment(**over):
    data = dict(
        id=1,
        lab_id=1,
        name="荧光光谱仪",
        model="F-7000",
        code="SPEC-F7000",
        category="光谱",
        status="normal",
        max_hours=4,
        requires_training=True,
    )
    data.update(over)
    return Equipment(**data)


def make_user(**over):
    data = dict(id=1, username="张伟", email="z@e.com", role="user", certs=["光谱"])
    data.update(over)
    return User(**data)


def make_reservation(start, end, status="confirmed", equipment_id=1):
    return Reservation(
        id=99,
        user_id=1,
        equipment_id=equipment_id,
        date=WEEKDAY,
        start_time=start,
        end_time=end,
        status=status,
        purpose="",
    )


class TestIntervals:
    def test_overlap_open_interval(self):
        assert overlaps(dt.time(14, 0), dt.time(16, 0), dt.time(15, 0), dt.time(17, 0))
        assert not overlaps(dt.time(14, 0), dt.time(16, 0), dt.time(16, 0), dt.time(18, 0))
        assert not overlaps(dt.time(14, 0), dt.time(16, 0), dt.time(12, 0), dt.time(14, 0))

    def test_minutes_between(self):
        assert minutes_between(dt.time(9, 30), dt.time(12, 0)) == 150

    def test_busy_merges_overlapping(self):
        rows = [
            make_reservation(dt.time(14, 0), dt.time(16, 0)),
            make_reservation(dt.time(15, 0), dt.time(17, 0)),
            make_reservation(dt.time(18, 0), dt.time(19, 0)),
        ]
        assert busy_intervals(rows) == [
            (dt.time(14, 0), dt.time(17, 0)),
            (dt.time(18, 0), dt.time(19, 0)),
        ]

    def test_busy_ignores_inactive(self):
        rows = [
            make_reservation(dt.time(14, 0), dt.time(16, 0), status="cancelled"),
            make_reservation(dt.time(18, 0), dt.time(19, 0), status="confirmed"),
        ]
        assert busy_intervals(rows) == [(dt.time(18, 0), dt.time(19, 0))]

    def test_free_windows_subtracts(self):
        window = (dt.time(8, 0), dt.time(22, 0))
        busy = [(dt.time(14, 0), dt.time(16, 0)), (dt.time(16, 30), dt.time(18, 0))]
        assert free_windows(window, busy) == [
            (dt.time(8, 0), dt.time(14, 0)),
            (dt.time(16, 0), dt.time(16, 30)),
            (dt.time(18, 0), dt.time(22, 0)),
        ]

    def test_free_windows_with_no_window(self):
        assert free_windows(None, []) == []

    def test_free_windows_covers_full_when_free(self):
        assert free_windows((dt.time(8, 0), dt.time(22, 0)), []) == [
            (dt.time(8, 0), dt.time(22, 0))
        ]


class TestOpenWindow:
    def test_weekday_vs_weekend(self):
        lab = make_lab()
        assert open_window(lab, WEEKDAY) == (dt.time(8, 0), dt.time(22, 0))
        assert open_window(lab, SATURDAY) == (dt.time(9, 0), dt.time(18, 0))

    def test_missing_config(self):
        assert open_window(make_lab(open_hours={}), WEEKDAY) is None

    def test_inverted_config_rejected(self):
        assert open_window(make_lab(open_hours={"weekday": ["20:00", "08:00"]}), WEEKDAY) is None


class TestPlaceSlots:
    def test_exact_window_must_fit(self):
        req = Requirement(date=WEEKDAY, start=dt.time(14, 0), end=dt.time(16, 0)).normalized()
        free = [(dt.time(8, 0), dt.time(14, 0)), (dt.time(16, 0), dt.time(22, 0))]
        assert place_slots(free, req, 4.0) == []  # 14-16 正好被切掉

    def test_exact_window_inside(self):
        req = Requirement(date=WEEKDAY, start=dt.time(14, 0), end=dt.time(16, 0)).normalized()
        assert place_slots([(dt.time(8, 0), dt.time(18, 0))], req, 4.0) == [
            (dt.time(14, 0), dt.time(16, 0))
        ]

    def test_duration_only_takes_window_start(self):
        req = Requirement(date=WEEKDAY, duration_hours=2).normalized()
        assert place_slots([(dt.time(8, 0), dt.time(12, 0))], req, 4.0) == [
            (dt.time(8, 0), dt.time(10, 0))
        ]

    def test_duration_capped_by_max_hours(self):
        # 要 10 小时、设备单次上限 4 小时 → 原诉求本身不可行，严格模式直接判无解，
        # 由协商层去给「缩短到 4 小时」的替代（见 test_negotiate.TestMaxHoursNegotiation）
        req = Requirement(date=WEEKDAY, duration_hours=10).normalized()
        assert place_slots([(dt.time(8, 0), dt.time(22, 0))], req, 4.0) == []

    def test_start_plus_duration_must_fit_whole_window(self):
        """★ 严格匹配：要 3 小时而窗口只剩 2 小时 → 无解。

        早期版本会「尽量塞」到 22:00 并谎称这是精确解，用户要 3 小时却只拿到 2 小时，
        而且系统一句解释都没有 —— 这正是本项目要消灭的静默降级。
        """
        req = Requirement(date=WEEKDAY, start=dt.time(20, 0), duration_hours=3).normalized()
        assert place_slots([(dt.time(8, 0), dt.time(22, 0))], req, 4.0) == []

    def test_start_only_without_duration_uses_max_hours(self):
        """只说起始时间、不说时长 → 按设备单次上限补足，这是显式约定而非静默截断。"""
        req = Requirement(date=WEEKDAY, start=dt.time(14, 0)).normalized()
        got = place_slots([(dt.time(8, 0), dt.time(22, 0))], req, 4.0)
        assert got == [(dt.time(14, 0), dt.time(18, 0))]


class TestTargetMatch:
    def test_name_substring_both_ways(self):
        ev = EquipmentView(equipment=make_equipment(), lab=make_lab())
        assert matches_target(ev, Requirement(equipment_name="荧光光谱仪"))
        assert matches_target(ev, Requirement(equipment_name="光谱仪"))

    def test_different_name_rejected(self):
        ev = EquipmentView(equipment=make_equipment(), lab=make_lab())
        assert not matches_target(ev, Requirement(equipment_name="离心机"))

    def test_category_match(self):
        ev = EquipmentView(equipment=make_equipment(), lab=make_lab())
        assert matches_target(ev, Requirement(category="光谱"))


class TestEvaluate:
    def _view(self, **over):
        return EquipmentView(equipment=make_equipment(**over), lab=make_lab())

    def _names(self, checks):
        return {c.name: c for c in checks}

    def test_all_pass(self):
        req = Requirement(
            date=WEEKDAY, start=dt.time(10, 0), end=dt.time(12, 0), equipment_name="荧光光谱仪"
        ).normalized()
        checks = self._names(evaluate(self._view(), make_user(), req, []))
        assert checks["equipment_status"].passed
        assert checks["training"].passed
        assert checks["open_hours"].passed
        assert checks["max_hours"].passed
        assert checks["conflict"].passed

    def test_training_blocks_when_missing_cert(self):
        req = Requirement(date=WEEKDAY, start=dt.time(10, 0), end=dt.time(12, 0)).normalized()
        checks = self._names(evaluate(self._view(), make_user(certs=[]), req, []))
        assert not checks["training"].passed
        assert "资质" in checks["training"].detail

    def test_no_training_required_passes_without_cert(self):
        req = Requirement(date=WEEKDAY, start=dt.time(10, 0), end=dt.time(12, 0)).normalized()
        view = self._view(requires_training=False, category="光谱")
        checks = self._names(evaluate(view, make_user(certs=[]), req, []))
        assert checks["training"].passed

    def test_maintenance_blocks(self):
        req = Requirement(date=WEEKDAY, start=dt.time(10, 0), end=dt.time(12, 0)).normalized()
        checks = self._names(evaluate(self._view(status="maintenance"), make_user(), req, []))
        assert not checks["equipment_status"].passed

    def test_closed_day_blocks(self):
        req = Requirement(date=WEEKDAY, start=dt.time(10, 0), end=dt.time(12, 0)).normalized()
        view = EquipmentView(equipment=make_equipment(), lab=make_lab(open_hours={}))
        checks = self._names(evaluate(view, make_user(), req, []))
        assert not checks["open_hours"].passed

    def test_max_hours_blocks(self):
        req = Requirement(date=WEEKDAY, start=dt.time(9, 0), end=dt.time(15, 0)).normalized()
        checks = self._names(evaluate(self._view(), make_user(), req, []))
        assert not checks["max_hours"].passed
        assert "超出上限" in checks["max_hours"].detail

    def test_capacity_blocks(self):
        req = Requirement(
            date=WEEKDAY, start=dt.time(10, 0), end=dt.time(12, 0), capacity=10
        ).normalized()
        checks = self._names(evaluate(self._view(), make_user(), req, []))
        assert not checks["capacity"].passed

    def test_capacity_absent_when_not_requested(self):
        req = Requirement(date=WEEKDAY, start=dt.time(10, 0), end=dt.time(12, 0)).normalized()
        checks = self._names(evaluate(self._view(), make_user(), req, []))
        assert "capacity" not in checks

    def test_conflict_blocks_and_names_the_holder(self):
        req = Requirement(date=WEEKDAY, start=dt.time(15, 0), end=dt.time(17, 0)).normalized()
        rows = [make_reservation(dt.time(14, 0), dt.time(16, 0))]
        checks = self._names(evaluate(self._view(), make_user(), req, rows))
        assert not checks["conflict"].passed
        assert "已被占用" in checks["conflict"].detail

    def test_conflict_ignores_non_overlapping(self):
        req = Requirement(date=WEEKDAY, start=dt.time(16, 0), end=dt.time(17, 0)).normalized()
        rows = [make_reservation(dt.time(14, 0), dt.time(16, 0))]
        checks = self._names(evaluate(self._view(), make_user(), req, rows))
        assert checks["conflict"].passed

    def test_past_time_blocks(self):
        now = dt.datetime(2026, 9, 24, 12, 0)
        req = Requirement(date=WEEKDAY, start=dt.time(9, 0), end=dt.time(10, 0)).normalized()
        checks = self._names(evaluate(self._view(), make_user(), req, [], now=now))
        assert not checks["conflict"].passed
        assert "已经过去" in checks["conflict"].detail
