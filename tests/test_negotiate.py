"""协商：冲突时给出的是「可比较的取舍」，不是一句「不可预约」。

这一层是本项目与「表单 + 抽槽位」写法最本质的分界，所以测得细。
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from lagent.clock import now_local
from lagent.db import session_scope
from lagent.domain.negotiate import negotiate
from lagent.models import User
from lagent.schemas import Requirement

TOMORROW = now_local().date() + dt.timedelta(days=1)
# 种子数据：荧光光谱仪（设备 1）明天 14:00-16:00 与 16:30-18:00 已被占
BUSY_START, BUSY_END = dt.time(14, 0), dt.time(16, 0)


async def _run(user_id: int, req: Requirement):
    async with session_scope() as session:
        user = cast(User, await session.get(User, user_id))
        return await negotiate(session, user, req)


def _req(**over) -> Requirement:
    data: dict[str, Any] = {"date": TOMORROW}
    data.update(over)
    return Requirement(**data).normalized()


class TestExact:
    async def test_satisfied_when_free(self, isolated_db):
        result = await _run(2, _req(start=dt.time(10, 0), end=dt.time(12, 0),
                                    equipment_name="荧光光谱仪"))
        assert result.satisfied
        assert result.proposals
        assert all(p.kind == "exact" for p in result.proposals)
        assert all(not p.relaxations for p in result.proposals)
        assert all(p.score == 1.0 for p in result.proposals)

    async def test_exact_has_no_blockers(self, isolated_db):
        result = await _run(2, _req(start=dt.time(10, 0), end=dt.time(11, 0),
                                    equipment_name="紫外可见分光光度计"))
        assert result.satisfied
        assert not result.blockers


class TestConflictNegotiation:
    async def test_conflict_yields_suggestions_not_refusal(self, isolated_db):
        """被占用时必须给备选，而不是只回一句「不可预约」。"""
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        assert not result.satisfied
        assert result.proposals, "冲突时应当给出备选方案"
        assert all(p.relaxations for p in result.proposals), "每个备选都必须说明放宽了什么"

    async def test_blockers_explain_the_reason(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        assert any("占用" in text for text in result.blockers)

    async def test_shift_is_closest_to_requested_time(self, isolated_db):
        """要 14:00 时，最近的替代是 12:00-14:00，而不是窗口开头的 08:00。

        这是协商质量的核心：同一个「可用」判定，落点选得差就等于没给方案。
        """
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        top = result.proposals[0]
        assert top.equipment_name == "荧光光谱仪"
        assert top.start == dt.time(12, 0), f"首选落点应为 12:00，实际 {top.start}"
        assert top.end == dt.time(14, 0)
        assert any("12:00" in note for note in top.relaxations)

    async def test_switch_equipment_is_offered_and_labelled(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        switches = [p for p in result.proposals if p.kind == "switch_equipment"]
        assert switches, "同实验室应有同类设备可替代"
        assert all(any("改用同类设备" in note for note in p.relaxations) for p in switches)

    async def test_shorten_uses_same_equipment(self, isolated_db):
        """16:00-16:30 那个空隙：同设备、时长缩到 0.5 小时。"""
        result = await _run(2, _req(start=BUSY_START, end=dt.time(18, 0),
                                    equipment_name="荧光光谱仪"))
        shorts = [p for p in result.proposals if p.kind == "shorten"]
        assert shorts
        assert all(p.equipment_name == "荧光光谱仪" for p in shorts)
        assert all(any("缩短为" in note for note in p.relaxations) for p in shorts)

    async def test_date_shift_is_labelled(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        dated = [p for p in result.proposals if p.date != TOMORROW]
        assert dated, "应当考虑顺延/提前一天"
        assert all(
            any("日期由" in note for note in p.relaxations) for p in dated
        ), "换了日期就必须写进放宽说明"

    async def test_score_ordering(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        scores = [p.score for p in result.proposals]
        assert scores == sorted(scores, reverse=True)

    async def test_scores_are_bounded(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        assert all(0.0 < p.score <= 1.0 for p in result.proposals)

    async def test_no_duplicate_slots(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        keys = [(p.equipment_id, p.date, p.start, p.end) for p in result.proposals]
        assert len(keys) == len(set(keys)), "同一时段被不同阶梯重复产出，去重没生效"

    async def test_proposals_are_future_only(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        today = now_local().date()
        assert all(p.date >= today for p in result.proposals)


class TestSelectionDiversity:
    """截断策略：每种放宽类别至少留一条。

    只按分数截断会让「挪时间」霸榜，用户看到一屏同质选项，误以为没有别的路可走 ——
    「缩短时长就能用上这台」这种真正有价值的替代会被挤掉。
    """

    def _props(self):
        from lagent.schemas import Proposal

        def make(kind, score, start_hour):
            return Proposal(
                kind=kind, equipment_id=1, equipment_name="荧光光谱仪", lab_label="分析楼 301",
                date=TOMORROW, start=dt.time(start_hour, 0),
                end=dt.time(start_hour + 1, 0), hours=1.0,
                relaxations=["某条放宽说明"], score=score,
            )

        # 三条同类的 shift 分数很高，两类真正的替代分低 —— 正是会被截掉的形态
        return [
            make("shift", 0.95, 8),
            make("shift", 0.90, 9),
            make("shift", 0.85, 10),
            make("shorten", 0.50, 11),
            make("switch_equipment", 0.45, 12),
        ]

    def test_every_kind_survives_truncation(self):
        from lagent.domain.negotiate import _select_diverse

        picked = _select_diverse(self._props(), 3)
        kinds = {p.kind for p in picked}
        assert kinds == {"shift", "shorten", "switch_equipment"}, (
            f"截断后类别被压平了：{kinds}"
        )

    def test_fills_remaining_slots_by_score(self):
        from lagent.domain.negotiate import _select_diverse

        picked = _select_diverse(self._props(), 4)
        assert len(picked) == 4
        assert [p.score for p in picked] == sorted(
            [p.score for p in picked], reverse=True
        ), "返回顺序必须仍按分数降序"

    def test_no_truncation_when_under_limit(self):
        from lagent.domain.negotiate import _select_diverse

        props = self._props()
        assert len(_select_diverse(props, 10)) == len(props)

    async def test_real_conflict_exposes_all_available_kinds(self, isolated_db):
        """端到端复现：14:00-18:00 冲突时，shorten 曾被纯分数截断挤掉。

        那时候用户看到的全是「换个时间」，而「16:00-16:30 缩短到半小时还能用这台」
        明明算得出来却不展示 —— 方案算对了，却死在最后一步截断上。
        """
        result = await _run(2, _req(start=BUSY_START, end=dt.time(18, 0),
                                    equipment_name="荧光光谱仪"))
        kinds = {p.kind for p in result.proposals}
        assert "shift" in kinds
        assert "shorten" in kinds, f"缩短时长的替代被截断了，实际只给出 {kinds}"
        assert "switch_equipment" in kinds, f"换同类设备被截断了，实际只给出 {kinds}"


class TestBlockerKind:
    """「为什么不行」的分类要结构化的往外传，回复层才能对症下药。"""

    async def test_none_when_satisfied(self, isolated_db):
        result = await _run(2, _req(start=dt.time(10, 0), end=dt.time(12, 0),
                                    equipment_name="荧光光谱仪"))
        assert result.satisfied
        assert result.blocker_kind == "none"

    async def test_no_date(self, isolated_db):
        async with session_scope() as session:
            user = cast(User, await session.get(User, 2))
            result = await negotiate(session, user, Requirement(equipment_name="荧光光谱仪"))
        assert result.blocker_kind == "no_date"

    async def test_no_target(self, isolated_db):
        result = await _run(2, _req(start=dt.time(10, 0), end=dt.time(11, 0),
                                    equipment_name="电子显微镜"))
        assert result.blocker_kind == "no_target"

    async def test_constraint_when_blocked_by_permission(self, isolated_db):
        result = await _run(1, _req(start=dt.time(10, 0), end=dt.time(11, 0),
                                    equipment_name="高速离心机"))
        assert result.blocker_kind == "constraint"
        failed = [c for c in result.checks if not c.passed]
        assert any(c.name == "training" for c in failed), "缺资质必须体现在 checks 里"

    async def test_constraint_when_conflict(self, isolated_db):
        result = await _run(2, _req(start=BUSY_START, end=BUSY_END,
                                    equipment_name="荧光光谱仪"))
        assert result.blocker_kind == "constraint"


class TestMaxHoursNegotiation:
    async def test_over_limit_yields_shorter_option(self, isolated_db):
        """要 6 小时而设备上限 2 小时：正确做法是给「2 小时可以」，而不是直接拒绝。"""
        result = await _run(
            2, _req(start=dt.time(9, 0), end=dt.time(15, 0), equipment_name="高速离心机")
        )
        shorts = [p for p in result.proposals if p.kind == "shorten"]
        assert shorts, "超出单次上限时应给出缩短时长的替代"
        assert all(p.hours <= 2.0 for p in shorts)
        assert all(any("缩短为" in note for note in p.relaxations) for p in shorts)


class TestPermissionBlocking:
    async def test_missing_cert_blocks_with_reason(self, isolated_db):
        """张伟没有「离心」资质：不该硬凑替代方案，而要如实说明原因。"""
        result = await _run(
            1, _req(start=dt.time(10, 0), end=dt.time(11, 0), equipment_name="高速离心机")
        )
        assert not result.satisfied
        assert not result.proposals
        assert any("资质" in text for text in result.blockers)

    async def test_same_request_passes_with_cert(self, isolated_db):
        result = await _run(
            2, _req(start=dt.time(10, 0), end=dt.time(11, 0), equipment_name="高速离心机")
        )
        assert result.satisfied


class TestUnknownTarget:
    async def test_unknown_equipment_reports_clearly(self, isolated_db):
        result = await _run(2, _req(start=dt.time(10, 0), end=dt.time(11, 0),
                                    equipment_name="电子显微镜"))
        assert not result.satisfied
        assert not result.proposals
        assert any("没有找到" in text for text in result.blockers)

    async def test_no_date_reports_clearly(self, isolated_db):
        result = await _run(2, Requirement(equipment_name="荧光光谱仪"))
        assert not result.satisfied
        assert any("日期" in text for text in result.blockers)


class TestCategoryScope:
    async def test_category_restricts_alternatives(self, isolated_db):
        """说了「细胞培养类」，就不该给出光谱类设备的替代。"""
        result = await _run(
            2, _req(start=dt.time(10, 0), end=dt.time(12, 0), category="细胞培养")
        )
        assert result.proposals
        assert all(p.equipment_name in {"CO2 培养箱", "生物安全柜"} for p in result.proposals)
