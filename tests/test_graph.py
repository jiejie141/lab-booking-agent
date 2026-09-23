"""LangGraph 编排：路由、多轮合并、可观测性与降级。"""

from __future__ import annotations

import datetime as dt
from typing import cast

from lagent.agent.graph import DEGRADED_REPLY, LabBookingAgent
from lagent.agent.state import SessionStore, pick_proposal
from lagent.clock import now_local
from lagent.schemas import ChatRequest

TOMORROW = now_local().date() + dt.timedelta(days=1)


async def ask(agent, message, user_id=2, session="s", **extra):
    return await agent.ainvoke(
        ChatRequest(message=message, user_id=user_id, session_id=session, **extra)
    )


class TestRouting:
    async def test_availability_with_exact_match(self, agent):
        resp = await ask(agent, "明天上午十点到十一点，荧光光谱仪")
        assert resp.intent == "create_reservation"
        assert resp.proposals

    async def test_admission_goes_to_retrieval(self, agent):
        resp = await ask(agent, "离心机使用有什么安全规范")
        assert resp.intent == "check_admission"
        assert resp.citations
        assert any("离心" in hit.heading for hit in resp.citations)

    async def test_smalltalk(self, agent):
        resp = await ask(agent, "你好")
        assert resp.intent == "smalltalk"
        assert "实验室预约助手" in resp.reply

    async def test_cancel_without_id_lists_reservations(self, agent):
        resp = await ask(agent, "我要取消预约")
        assert resp.intent == "cancel_reservation"
        assert resp.stage in ("cancel_need_id", "cancel_none")
        assert resp.reply


class TestMissingSlots:
    async def test_asks_when_incomplete(self, agent):
        resp = await ask(agent, "我想约个设备")
        assert resp.stage == "awaiting_slots"
        assert set(resp.missing) == {"date", "time_window", "equipment"}
        assert "哪一天" in resp.reply

    async def test_no_proposals_when_incomplete(self, agent):
        resp = await ask(agent, "我想约个设备")
        assert not resp.proposals

    async def test_partial_equipment_only_still_asks(self, agent):
        resp = await ask(agent, "我想约荧光光谱仪")
        assert resp.stage == "awaiting_slots"
        assert "equipment" not in resp.missing
        assert {"date", "time_window"} <= set(resp.missing)


class TestMultiTurnMerge:
    async def test_merge_fills_previous_slots(self, agent):
        """先说不完整的，再补一句 —— 两轮合起来应当齐备并完成下单。"""
        first = await ask(agent, "我想约荧光光谱仪", session="merge")
        assert first.stage == "awaiting_slots"

        second = await ask(agent, "明天晚上八点，一小时", session="merge")
        assert second.stage != "awaiting_slots", f"仍未凑齐槽位：{second.missing}"
        assert second.booking is not None and second.booking.ok
        assert second.booking.reservation.equipment_name == "荧光光谱仪"

    async def test_no_merge_when_not_awaiting(self, agent):
        """★ 回归测试：新请求不得继承上一轮的诉求。

        早期版本无条件跨轮合并，于是「我想约个设备」会继承上一轮已经确定的
        设备和时间，直接跳过追问给出方案 —— 用户会莫名其妙被安排在一台设备上。
        """
        session = "nomerge"
        first = await ask(agent, "明天下午两点想用荧光光谱仪两小时", session=session)
        assert first.stage != "awaiting_slots"  # 上一轮信息齐备，不在等补槽位

        second = await ask(agent, "我想约个设备", session=session)
        assert second.stage == "awaiting_slots", "新请求被上一轮的诉求污染了"
        assert set(second.missing) == {"date", "time_window", "equipment"}

    async def test_awaiting_flag_cleared_after_negotiate(self, agent):
        store: SessionStore = agent.store
        await ask(agent, "我想约荧光光谱仪", session="flag")
        assert store.awaiting("flag") is True
        await ask(agent, "明天晚上八点，一小时", session="flag")
        assert store.awaiting("flag") is False


class TestSelection:
    async def test_pick_proposal_by_index(self, agent):
        first = await ask(agent, "明天下午两点想用荧光光谱仪两小时", session="pick")
        assert first.proposals

        second = await ask(agent, "第 1 个", session="pick")
        assert second.booking is not None and second.booking.ok
        assert second.booking.reservation.equipment_name == first.proposals[0].equipment_name

    async def test_pick_proposal_helper(self):
        from lagent.schemas import Proposal

        props = [
            Proposal(kind="shift", equipment_id=1, equipment_name="A", lab_label="L",
                     date=TOMORROW, start=dt.time(10, 0), end=dt.time(11, 0), hours=1),
            Proposal(kind="shift", equipment_id=2, equipment_name="B", lab_label="L",
                     date=TOMORROW, start=dt.time(12, 0), end=dt.time(13, 0), hours=1),
        ]
        assert cast(Proposal, pick_proposal("第 2 个", props)).equipment_id == 2
        assert cast(Proposal, pick_proposal("2", props)).equipment_id == 2
        assert pick_proposal("算了", props) is None
        assert pick_proposal("第 9 个", props) is None
        assert pick_proposal("好", props) is None

    async def test_pick_out_of_range_falls_back_gracefully(self, agent):
        await ask(agent, "明天下午两点想用荧光光谱仪两小时", session="pick2")
        resp = await ask(agent, "第 9 个", session="pick2")
        # 越界序号不该被当成「选中」，应退回重新协商而不是崩掉
        assert resp.stage != "booked"

    async def test_accept_fields_book_directly(self, agent):
        resp = await ask(
            agent,
            "就这个",
            accept_equipment_id=2,
            accept_date=TOMORROW,
            accept_start=dt.time(10, 0),
            accept_end=dt.time(11, 0),
        )
        assert resp.booking is not None and resp.booking.ok
        assert resp.booking.reservation.equipment_id == 2


class TestAutoBooking:
    async def test_single_exact_solution_books_automatically(self, agent):
        resp = await ask(agent, "明天上午十点到十一点，紫外可见分光光度计")
        assert resp.booking is not None and resp.booking.ok

    async def test_multiple_candidates_require_confirmation(self, agent):
        """有两个可选时不擅自下单，先让用户挑。"""
        resp = await ask(agent, "明天下午三点到五点，细胞培养类设备")
        assert len(resp.proposals) >= 2
        assert resp.booking is None
        assert "回复序号" in resp.reply

    async def test_conflict_does_not_book(self, agent):
        resp = await ask(agent, "明天下午两点想用荧光光谱仪两小时")
        assert resp.booking is None
        assert resp.proposals


class TestTrace:
    async def test_trace_accumulates_every_node(self, agent):
        """★ 回归测试：trace 必须累加。

        LangGraph 默认「同名键后写覆盖」。如果 AgentState 里不把 trace 声明成
        operator.add 累加器，每个节点返回的记录都会把前面的冲掉，最后只剩一步 ——
        图照样跑通、结果也对，只是可观测性静默丢失。
        """
        resp = await ask(agent, "明天上午十点到十一点，紫外可见分光光度计")
        nodes = [step.node for step in resp.trace]
        assert nodes[0] == "parse"
        assert "negotiate" in nodes
        assert "book" in nodes
        assert nodes[-1] == "compose"
        assert len(nodes) >= 4

    async def test_trace_has_timings(self, agent):
        resp = await ask(agent, "你好")
        assert all(step.elapsed_ms >= 0 for step in resp.trace)

    async def test_trace_on_ask_path(self, agent):
        resp = await ask(agent, "我想约个设备")
        assert [step.node for step in resp.trace] == ["parse", "ask"]


class TestDegraded:
    async def test_degraded_when_no_client(self, catalog):
        agent = LabBookingAgent(None, store=SessionStore())
        resp = await ask(agent, "明天下午两点想用荧光光谱仪两小时")
        assert resp.degraded is True
        assert resp.reply == DEGRADED_REPLY
        assert resp.trace[0].node == "degrade"

    async def test_guided_form_format_still_accepted(self, agent):
        """降级提示里教用户直接给结构化输入，这条路径必须真的能用。"""
        resp = await ask(agent, f"{TOMORROW.isoformat()} 10:00-11:00 紫外可见分光光度计")
        assert resp.booking is not None and resp.booking.ok


class TestBlockerExplanations:
    """回复要「对症」：缺资质不能说成时段问题。

    早期版本无论什么原因约不到，都回一句「要不要换个日期或缩短时长再试试？」——
    对冲突场景是对的，对「没有离心资质」就变成了答非所问：
    用户换一百个时间也约不上，真正该做的是去培训。
    """

    async def test_cert_block_is_not_framed_as_a_timing_problem(self, agent):
        resp = await ask(agent, "明天上午十点到十一点，高速离心机", user_id=1)
        assert resp.booking is None
        assert not resp.proposals
        assert "资质" in resp.reply
        assert "换个日期" not in resp.reply
        assert "缩短时长" not in resp.reply

    async def test_unknown_device_is_not_framed_as_a_timing_problem(self, agent):
        resp = await ask(agent, "明天上午十点到十一点，电子显微镜")
        assert not resp.proposals
        assert "设备" in resp.reply
        assert "换个日期" not in resp.reply


class TestComposeWording:
    """直接对 compose 输入不同分类，验证措辞选择本身（不依赖种子数据凑场景）。"""

    def _client(self):
        from lagent.agent.llm import MockLLMClient

        return MockLLMClient([])

    async def test_soft_block_suggests_changing_conditions(self):
        from lagent.schemas import ConstraintCheck

        reply = await self._client().compose({
            "kind": "availability",
            "satisfied": False,
            "proposals": [],
            "blockers": ["已被占用：2026-09-24 14:00-16:00"],
            "checks": [ConstraintCheck(name="conflict", passed=False,
                                       detail="已被占用：2026-09-24 14:00-16:00")],
            "blocker_kind": "constraint",
        })
        assert "换个日期" in reply and "缩短时长" in reply

    async def test_hard_block_says_it_is_not_about_timing(self):
        from lagent.schemas import ConstraintCheck

        reply = await self._client().compose({
            "kind": "availability",
            "satisfied": False,
            "proposals": [],
            "blockers": ["缺少「离心」准入资质，需先通过培训"],
            "checks": [ConstraintCheck(name="training", passed=False,
                                       detail="缺少「离心」准入资质，需先通过培训")],
            "blocker_kind": "constraint",
        })
        assert "资质" in reply
        assert "不是换个时间能解决的" in reply
        assert "换个日期" not in reply

    async def test_no_target_reply_points_at_the_device(self):
        reply = await self._client().compose({
            "kind": "availability", "satisfied": False, "proposals": [],
            "blockers": ["没有找到匹配「电子显微镜」的设备"],
            "blocker_kind": "no_target",
        })
        assert "设备" in reply and "换个日期" not in reply

    async def test_no_date_reply_asks_for_the_date(self):
        reply = await self._client().compose({
            "kind": "availability", "satisfied": False, "proposals": [],
            "blockers": ["还没有确定日期，无法计算任何候选时段"],
            "blocker_kind": "no_date",
        })
        assert "日期" in reply and "换个日期" not in reply


class TestSessionStore:
    def test_eviction_keeps_last_n(self):
        from lagent.schemas import Requirement

        store = SessionStore(max_sessions=2)
        for i in range(4):
            store.save(f"s{i}", [], Requirement())
        assert store.requirement("s0") is None
        assert store.requirement("s1") is None
        assert store.requirement("s3") is not None

    def test_clear(self):
        from lagent.schemas import Requirement

        store = SessionStore()
        store.save("x", [], Requirement(), awaiting=True)
        assert store.awaiting("x")
        store.clear("x")
        assert store.proposals("x") == []
        assert not store.awaiting("x")

    def test_requirement_summary(self):
        from lagent.schemas import Requirement

        req = Requirement(
            date=TOMORROW, start=dt.time(14, 0), end=dt.time(16, 0), equipment_name="荧光光谱仪"
        )
        assert "荧光光谱仪" in req.summary()
        assert "14:00" in req.summary()
