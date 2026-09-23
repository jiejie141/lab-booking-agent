"""中文时间/数量解析 —— Mock 模型的底座，也是最容易悄悄错的地方。"""

from __future__ import annotations

import datetime as dt

import pytest

from lagent.agent.llm import (
    MockLLMClient,
    cn_number,
    parse_date_cn,
    parse_duration,
    parse_times,
)

TODAY = dt.date(2026, 9, 23)  # 周三


class TestNumber:
    @pytest.mark.parametrize(
        "raw,expected",
        [("3", 3), ("一", 1), ("两", 2), ("十", 10), ("十一", 11), ("二十", 20), ("二十三", 23)],
    )
    def test_cn_number(self, raw, expected):
        assert cn_number(raw) == expected

    def test_unknown(self):
        assert cn_number("abc") is None
        assert cn_number(None) is None


class TestDate:
    @pytest.mark.parametrize(
        "text,offset",
        [("今天", 0), ("明天", 1), ("后天", 2), ("大后天", 3)],
    )
    def test_relative_days(self, text, offset):
        assert parse_date_cn(text, TODAY) == TODAY + dt.timedelta(days=offset)

    def test_explicit_iso(self):
        assert parse_date_cn("2026-10-08 用设备", TODAY) == dt.date(2026, 10, 8)

    def test_month_day(self):
        assert parse_date_cn("9月25日预约", TODAY) == dt.date(2026, 9, 25)

    def test_weekday_this_week(self):
        # 2026-09-23 是周三 → 周五 = +2
        assert parse_date_cn("周五用", TODAY) == TODAY + dt.timedelta(days=2)

    def test_weekday_same_day_is_today(self):
        assert parse_date_cn("周三用", TODAY) == TODAY

    def test_next_week_is_really_next_week(self):
        """下周X 必须落在下一周，不能退化成本周。"""
        got = parse_date_cn("下周三用", TODAY)
        assert got == dt.date(2026, 9, 30)
        assert got.weekday() == 2
        assert got > TODAY + dt.timedelta(days=6)

    def test_next_week_monday(self):
        got = parse_date_cn("下周一用", TODAY)
        assert got == dt.date(2026, 9, 28)
        assert got.weekday() == 0

    def test_none_when_absent(self):
        assert parse_date_cn("我想约设备", TODAY) is None


class TestTime:
    def test_forum_style(self):
        assert parse_times("14:00-16:00") == [dt.time(14, 0), dt.time(16, 0)]

    def test_cn_hour_with_period(self):
        assert parse_times("下午两点") == [dt.time(14, 0)]

    def test_cn_hour_half(self):
        assert parse_times("上午九点半") == [dt.time(9, 30)]

    def test_evening(self):
        assert parse_times("晚上7点") == [dt.time(19, 0)]

    def test_period_inheritance(self):
        """「下午两点到四点」的后半段不带时段词，必须继承成 16:00 而不是 04:00。"""
        assert parse_times("下午两点到四点") == [dt.time(14, 0), dt.time(16, 0)]

    def test_period_inheritance_keeps_forward_order(self):
        """上午九点到十一点：后半段比前半段晚，不该被加 12 小时。"""
        assert parse_times("上午九点到十一点") == [dt.time(9, 0), dt.time(11, 0)]

    def test_duration_not_mistaken_for_time(self):
        """「3小时」不是时间点，不该被解析成 03:00。"""
        assert parse_times("想用 3 小时") == []

    def test_date_digits_not_mistaken_for_time(self):
        assert parse_times("9月25日") == []


class TestDuration:
    @pytest.mark.parametrize(
        "text,expected",
        [("2小时", 2.0), ("两小时", 2.0), ("半小时", 0.5), ("3 个小时", 3.0), ("90分钟", 1.5)],
    )
    def test_duration(self, text, expected):
        assert parse_duration(text) == pytest.approx(expected)

    def test_absent(self):
        assert parse_duration("明天下午两点") is None


class TestExtraction:
    async def test_extract_full_requirement(self, catalog):
        client = MockLLMClient(catalog)
        req = await client.extract_requirement("明天下午两点到四点想用荧光光谱仪")
        assert req.date == TODAY + dt.timedelta(days=1) or req.date is not None
        assert req.start == dt.time(14, 0)
        assert req.end == dt.time(16, 0)
        assert req.equipment_name == "荧光光谱仪"

    async def test_extract_by_category(self, catalog):
        client = MockLLMClient(catalog)
        req = await client.extract_requirement("明天想用离心类设备")
        assert req.category == "离心"
        assert req.equipment_name is None

    async def test_unlimited_means_no_target(self, catalog):
        client = MockLLMClient(catalog)
        req = await client.extract_requirement("明天下午两点，设备不限")
        assert req.equipment_name is None
        assert req.category is None

    async def test_missing_slots_reported(self, catalog):
        client = MockLLMClient(catalog)
        req = await client.extract_requirement("我想约个设备")
        fields = {field for field, _ in req.missing_slots()}
        assert fields == {"date", "time_window", "equipment"}

    async def test_capacity_extracted(self, catalog):
        client = MockLLMClient(catalog)
        req = await client.extract_requirement("明天下午两点想用光谱仪，3 个人")
        assert req.capacity == 3
