"""CI 注解包装（``scripts/ci_run.py``）。

这个脚本的唯一职责是**让失败在无凭据时也可读**。所以这里要验的正是
那件事真的发生了：注解里有退出码、有被挑出来的关键行、
而且特殊字符被正确转义（没转义的注解会被 Actions 截断或解析错）。

顺带验一个容易被写错的地方：包装**不能吞掉退出码** ——
它要是永远返回 0，CI 就再也不会红了，比没有注解严重得多。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_ci_run():
    """按路径加载 scripts/ci_run.py（它不在包内，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("ci_run", ROOT / "scripts" / "ci_run.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_run = _load_ci_run()


def run(monkeypatch, capsys, *argv: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["ci_run.py", *argv])
    code = ci_run.main()
    return code, capsys.readouterr().out


class TestExitCode:
    """★ 最关键的一条：退出码必须原样透传。

    包装一旦把非零码吃掉，CI 就永远不会红 —— 那比没有注解严重得多。
    """

    def test_success_returns_zero(self, monkeypatch, capsys):
        code, out = run(monkeypatch, capsys, "--", sys.executable, "-c", "print('fine')")
        assert code == 0
        assert "fine" in out
        assert "::error::" not in out, "成功时不该产生错误注解"

    def test_failure_returns_the_same_nonzero_code(self, monkeypatch, capsys):
        code, out = run(
            monkeypatch, capsys, "--", sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"
        )
        assert code == 3, "退出码必须原样透传，否则 CI 不会红"
        assert "boom" in out, "原输出要照常打印，包装不隐藏日志"
        assert "::error::" in out, "失败必须产生注解，否则无 token 时看不到原因"

    def test_annotation_carries_the_exit_code(self, monkeypatch, capsys):
        _, out = run(monkeypatch, capsys, "--", sys.executable, "-c", "import sys; sys.exit(7)")
        assert "exit=7" in out

    def test_usage_error_when_no_command(self, monkeypatch, capsys):
        code, out = run(monkeypatch, capsys)
        assert code == 2
        assert "用法" in out


class TestLineSelection:
    def test_match_keeps_only_matching_lines(self):
        stdout = "collected 3 items\n\nFAILED tests/a.py::test_x - assert 1 == 2\n5 passed, 1 failed"
        picked = ci_run.pick_lines(stdout, "", match=r"^(FAILED|ERROR)", tail=20)
        assert picked == ["FAILED tests/a.py::test_x - assert 1 == 2"]

    def test_without_match_it_takes_the_tail(self):
        stdout = "\n".join(f"line {i}" for i in range(50))
        picked = ci_run.pick_lines(stdout, "", match="", tail=3)
        assert picked == ["line 47", "line 48", "line 49"]

    def test_blank_lines_are_skipped(self):
        picked = ci_run.pick_lines("a\n\n\n  \nb", "", match="", tail=10)
        assert picked == ["a", "b"]

    def test_stderr_is_included(self):
        picked = ci_run.pick_lines("", "Traceback: boom", match="boom", tail=5)
        assert picked == ["Traceback: boom"]

    def test_matched_lines_are_used_from_a_failing_command(self, monkeypatch, capsys):
        script = "print('noise'); print('FAILED tests/x.py::test_y'); import sys; sys.exit(1)"
        _, out = run(
            monkeypatch, capsys, "--match", r"^FAILED", "--tail", "10", "--", sys.executable, "-c", script
        )
        assert "FAILED tests/x.py::test_y" in out
        assert "::error::noise" not in out, "不匹配的行不该被抬成注解"


class TestEscaping:
    """没转义的注解会被 Actions 截断或解析错 —— 而且**不报错**，只是内容丢了。"""

    def test_plain_text_is_untouched(self):
        assert ci_run.escape_annotation("FAILED tests/a.py::test_x") == "FAILED tests/a.py::test_x"

    def test_percent_is_escaped_first(self):
        assert ci_run.escape_annotation("100%") == "100%25"

    def test_newline_and_cr_are_escaped(self):
        assert ci_run.escape_annotation("a\nb") == "a%0Ab"
        assert ci_run.escape_annotation("a\rb") == "a%0Db"

    def test_a_percent_before_a_newline_is_not_double_escaped(self):
        """顺序错了会把 ``\\n`` 自己的 ``%0A`` 再转义一遍，变成 ``%250A``。"""
        assert ci_run.escape_annotation("a%\nb") == "a%25%0Ab"

    def test_escaped_line_survives_into_the_annotation(self, monkeypatch, capsys):
        script = "print('progress 50% done'); import sys; sys.exit(1)"
        _, out = run(monkeypatch, capsys, "--tail", "5", "--", sys.executable, "-c", script)
        assert "::error::progress 50%25 done" in out


class TestUnknownOptions:
    def test_unknown_option_warns_but_still_runs(self, monkeypatch, capsys):
        """打错一个选项就整步不跑，会让排查变成"为什么命令没执行"。"""
        code, out = run(monkeypatch, capsys, "--nope", "--", sys.executable, "-c", "print('ran')")
        assert code == 0
        assert "ran" in out
        assert "::warning::" in out


@pytest.mark.parametrize("tail", ["1", "5", "20"])
def test_tail_option_is_accepted(monkeypatch, capsys, tail):
    code, _ = run(monkeypatch, capsys, "--tail", tail, "--", sys.executable, "-c", "print('x')")
    assert code == 0
