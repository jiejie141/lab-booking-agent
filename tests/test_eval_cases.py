"""评测集的**日历无关性**守卫。

## 为什么需要这个文件

`eval/cases.yaml` 早就知道要避开绝对日期（用例里一律用 `date_offset`），
但仍然栽了一次：`date_offset: 1` 会随运行日期落到周六或周日，而三个实验室的
**周末开放时间都比工作日短**。于是同一份代码，周二跑 14/14、周六跑 11/14，
红的三条（c03 / c06 / c14）看起来像业务逻辑坏了，实际是评测集自己挑了个
开不了门的钟点。

这类 bug 的危险在于它**伪装成代码缺陷**：CI 上红一次，人去翻 diff，
发现那两次运行之间只有一行文档改动 —— 然后开始怀疑人生。
真正的修法不是把三条用例调通，是让"挑时段"这件事从此有约束。

## 守卫的是什么

凡是用 `date_offset` 的用例（也就是日期会随运行日漂移的那些），
它声明的时段必须落在目标实验室**「工作日 ∩ 周末」的交集**里。
落在交集里 = 不管哪天跑都合法。用 `下周三` 这类写死星期几的用例不受此限，
因为它们不会漂。

## 顺带钉住的一条

`date_offset` 必须 ≥ 1。用 0（今天）的话，同一条用例上午跑和晚上跑
结果不同 —— 那是比星期几更细的一种漂移。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from lagent.seed import EQUIPMENT, LABS

CASES_PATH = pathlib.Path(__file__).resolve().parents[1] / "eval" / "cases.yaml"


def _minutes(hhmm: str) -> int:
    hour, minute = hhmm.split(":")
    return int(hour) * 60 + int(minute)


def safe_window(lab_index: int) -> tuple[int, int]:
    """该实验室「工作日 ∩ 周末」的开放窗口（分钟）。

    取交集而不是分别判，是因为评测用例的日期会漂 —— 它必须**同时**
    在两种日子下都合法，才谈得上"与星期几无关"。
    """
    hours = LABS[lab_index]["open_hours"]
    w_open, w_close = hours["weekday"]
    e_open, e_close = hours["weekend"]
    return max(_minutes(w_open), _minutes(e_open)), min(_minutes(w_close), _minutes(e_close))


def labs_for(expect: dict) -> set[int]:
    """这条用例可能落到哪几个实验室。

    只给类别时（如"光谱类设备"）会有多台设备、可能跨房间 —— 那就要求
    **每一个**候选实验室都装得下这个时段，否则解析到哪一台全看运气。
    """
    found: set[int] = set()
    needle = expect.get("equipment_contains")
    if needle:
        found |= {item["lab"] for item in EQUIPMENT if needle in item["name"]}
    category = expect.get("category")
    if category:
        found |= {item["lab"] for item in EQUIPMENT if item["category"] == category}
    return found


def check_window(expect: dict) -> str | None:
    """返回违规说明；合法则返回 None。

    抽成纯函数是为了能被下面那条"守卫本身有效吗"的用例直接调用 ——
    一个从没红过的守卫，和没有守卫是一样的。
    """
    if "date_offset" not in expect:
        return None
    labs = labs_for(expect)
    if not labs:
        return None  # 没点名设备也没说类别（追问类用例），无窗口可判
    start, end = _minutes(expect["start"]), _minutes(expect["end"])
    for lab in sorted(labs):
        low, high = safe_window(lab)
        if start < low or end > high:
            name = f"{LABS[lab]['building']}{LABS[lab]['room']}"
            return (
                f"{name} 的工作日∩周末窗口是 {low // 60:02d}:{low % 60:02d}-"
                f"{high // 60:02d}:{high % 60:02d}，"
                f"而用例要 {expect['start']}-{expect['end']}"
            )
    return None


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))["cases"]


class TestCalendarIndependence:
    def test_every_drifting_case_fits_the_intersection_window(self, cases):
        """★ 用 date_offset 的用例，时段必须哪天跑都合法。

        这条一旦红，说明新加的用例会在某些星期几上失败 ——
        而它失败时会伪装成业务 bug。
        """
        offenders = []
        for case in cases:
            expect = case.get("expect_slots") or {}
            problem = check_window(expect)
            if problem:
                offenders.append(f"{case['id']}: {problem}")
        assert not offenders, "以下用例的时段会随星期几失效：\n  " + "\n  ".join(offenders)

    def test_date_offset_is_never_today(self, cases):
        """0 = 今天：上午跑和晚上跑结果不同，比星期几更细的一种漂移。"""
        for case in cases:
            expect = case.get("expect_slots") or {}
            if "date_offset" in expect:
                assert int(expect["date_offset"]) >= 1, (
                    f"{case['id']} 用了 date_offset="
                    f"{expect['date_offset']}（今天），结果会随当天时刻变化"
                )

    def test_the_guard_actually_catches_the_bug_it_was_written_for(self):
        """守卫本身要有效 —— 拿改动前那三条真实用例回放一遍。

        一个从没红过的断言和没有断言是一回事。这里直接喂进当初让 CI 变红的
        三个时段（09:00-12:00 进生物楼、20:00-21:00 用分析楼、15:00-17:00
        在生物楼），**必须**全部被拦下。
        """
        known_bad = [
            {"date_offset": 1, "start": "09:00", "end": "12:00", "equipment_contains": "CO2"},
            {"date_offset": 1, "start": "20:00", "end": "21:00", "equipment_contains": "荧光光谱仪"},
            {"date_offset": 1, "start": "15:00", "end": "17:00", "category": "细胞培养"},
        ]
        for expect in known_bad:
            assert check_window(expect) is not None, f"守卫漏掉了 {expect}"

    def test_the_guard_does_not_fire_on_legitimate_times(self):
        """反向：合法时段不能被误拦 —— 否则守卫会被人绕过去或干脆删掉。"""
        legit = [
            {"date_offset": 1, "start": "10:00", "end": "13:00", "equipment_contains": "CO2"},
            {"date_offset": 1, "start": "10:00", "end": "11:00", "equipment_contains": "荧光光谱仪"},
            {"date_offset": 1, "start": "13:00", "end": "15:00", "category": "细胞培养"},
            {"date_offset": 1, "start": "09:00", "end": "15:00", "equipment_contains": "高速离心机"},
        ]
        for expect in legit:
            assert check_window(expect) is None, f"误拦了合法时段 {expect}"

    def test_every_expected_outcome_is_a_known_label(self, cases):
        """``expect_outcome`` 拼错了会静默改变判定口径。

        （c04 的 ``blocked`` 是**资质**拦截，与开放时间无关，所以它用
        ``date_offset`` 完全合理 —— 别把这两种"被拦下"混为一谈。）
        """
        from lagent.evaluation import OUTCOMES

        allowed = set(OUTCOMES)
        for case in cases:
            got = case.get("expect_outcome")
            assert got in allowed, f"{case['id']} 的 expect_outcome={got!r} 不在 {sorted(allowed)} 里"
