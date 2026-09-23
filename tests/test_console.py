"""控制台编码加固。

这个 bug 的形态值得单独记住：本地交互式跑没问题，一重定向就崩 ——
所以不能只靠「我这边跑过了」来确认，必须有一个真的用 GBK 流去验的测试。
"""

from __future__ import annotations

import io

import pytest

from lagent.console import enable_utf8_output


class TestEnableUtf8Output:
    def test_gbk_stream_would_crash_without_hardening(self):
        """先确认问题真实存在：GBK 流写 ✓ 会抛 UnicodeEncodeError。"""
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

    def test_doctor_output_is_encodable_after_hardening(self, capsys):
        """doctor 打印的整段文本（含 ✓/✗/→）在 UTF-8 下必须都能编码。"""
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk")
        enable_utf8_output(stream)
        for text in ("  ✓ 模型             : mock", "  ✗ 检索不可用", "  ⚠ 检索已降级",
                     "trace: parse → negotiate → book → compose"):
            stream.write(text + "\n")
        stream.flush()
        assert "✓" in buffer.getvalue().decode("utf-8")
