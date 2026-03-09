"""Tests for checkloop.streaming — JSONL buffer processing (process_jsonl_buffer)."""

from __future__ import annotations

import json
import time
import unittest.mock
from typing import Any

import pytest

from checkloop import streaming


class TestProcessJsonlBuffer:
    """Tests for process_jsonl_buffer() JSONL buffer processing."""

    def test_complete_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray((event + "\n").encode())
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "hello" in capsys.readouterr().out

    def test_partial_line_returned(self) -> None:
        buf = bytearray(b"partial")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray(b"partial")

    def test_multiple_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        e1 = json.dumps({"type": "system", "message": "one"})
        e2 = json.dumps({"type": "system", "message": "two"})
        buf = bytearray(f"{e1}\n{e2}\n".encode())
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        out = capsys.readouterr().out
        assert "one" in out
        assert "two" in out

    def test_invalid_json_debug(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"not json\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=True)
        assert remainder == bytearray()
        assert "not json" in capsys.readouterr().out

    def test_invalid_json_not_debug(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"not json\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "not json" not in capsys.readouterr().out

    def test_empty_line_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"\n\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_line_with_remainder(self) -> None:
        event = json.dumps({"type": "system", "message": "x"})
        buf = bytearray(f"{event}\npartial".encode())
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray(b"partial")

    def test_empty_buffer(self) -> None:
        buf = bytearray(b"")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray(b"")

    def test_whitespace_only_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"   \n\t\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_unicode_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "こんにちは 🎉"})
        buf = bytearray((event + "\n").encode("utf-8"))
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "こんにちは" in capsys.readouterr().out


class TestProcessJsonlBufferInvalidUtf8:
    """Edge case: process_jsonl_buffer with invalid UTF-8 bytes."""

    def test_invalid_utf8_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"\xff\xfe invalid\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=True)
        assert remainder == bytearray()

    def test_valid_utf8_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "café ñ ü"})
        buf = bytearray((event + "\n").encode("utf-8"))
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "café" in capsys.readouterr().out


class TestProcessJsonlBufferLargeInput:
    """Tests for process_jsonl_buffer with large or boundary-sized inputs."""

    def test_single_newline_only(self) -> None:
        buf = bytearray(b"\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_multiple_consecutive_newlines(self) -> None:
        buf = bytearray(b"\n\n\n\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_json_with_escaped_newlines_in_string(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "line1\\nline2"})
        buf = bytearray((event + "\n").encode())
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "line1" in capsys.readouterr().out


class TestProcessJsonlBufferMixedContent:
    """Tests for process_jsonl_buffer with mixed valid/invalid content."""

    def test_mixed_valid_and_invalid(self, capsys: pytest.CaptureFixture[str]) -> None:
        valid_event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray(f"invalid\n{valid_event}\n".encode())
        result = streaming.process_jsonl_buffer(buf, 0.0, False)
        assert result == bytearray(b"")
        output = capsys.readouterr().out
        assert "hello" in output


class TestProcessJsonlBufferEventErrors:
    """Tests for process_jsonl_buffer when _print_event raises TypeError/KeyError/AttributeError."""

    def test_type_error_in_print_event_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        valid_event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray((valid_event + "\n").encode())
        with unittest.mock.patch.object(streaming, "_print_event", side_effect=TypeError("bad type")):
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_key_error_in_print_event_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        valid_event = json.dumps({"type": "tool_use", "tool": "Read"})
        buf = bytearray((valid_event + "\n").encode())
        with unittest.mock.patch.object(streaming, "_print_event", side_effect=KeyError("missing key")):
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_attribute_error_in_print_event_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        valid_event = json.dumps({"type": "result", "result": "done"})
        buf = bytearray((valid_event + "\n").encode())
        with unittest.mock.patch.object(streaming, "_print_event", side_effect=AttributeError("no attr")):
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()


class TestProcessJsonlBufferNonDictJson:
    """Edge cases for process_jsonl_buffer with valid JSON that isn't a dict."""

    def test_non_dict_json_skipped(self) -> None:
        buf = bytearray(b'42\n"hello"\n[1,2]\nnull\ntrue\n')
        with unittest.mock.patch.object(streaming, "_print_event") as mock_print:
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
            mock_print.assert_not_called()
        assert remainder == bytearray()


class TestProcessJsonlBufferMaxBufferDisabled:
    """Test process_jsonl_buffer when max_buffer_size is 0 (disabled)."""

    def test_zero_max_buffer_size_does_not_truncate(self) -> None:
        big_buf = bytearray(b"x" * 100_000)
        result = streaming.process_jsonl_buffer(
            big_buf, 0.0, False, max_buffer_size=0,
        )
        assert len(result) == 100_000


class TestProcessJsonlBufferMaxBufferEnforced:
    """Tests for process_jsonl_buffer when max_buffer_size is exceeded."""

    def test_buffer_cleared_when_exceeding_limit(self) -> None:
        # 500 bytes of incomplete data (no newline) exceeds a 100-byte limit
        big_buf = bytearray(b"x" * 500)
        result = streaming.process_jsonl_buffer(
            big_buf, 0.0, False, max_buffer_size=100,
        )
        assert len(result) == 0

    def test_buffer_not_cleared_when_under_limit(self) -> None:
        small_buf = bytearray(b"partial")
        result = streaming.process_jsonl_buffer(
            small_buf, 0.0, False, max_buffer_size=1000,
        )
        assert result == bytearray(b"partial")

    def test_buffer_cleared_after_processing_complete_lines(self) -> None:
        event = json.dumps({"type": "system", "message": "ok"})
        # Complete line + oversized remainder
        buf = bytearray(f"{event}\n".encode() + b"x" * 500)
        result = streaming.process_jsonl_buffer(
            buf, 0.0, False, max_buffer_size=100,
        )
        assert len(result) == 0
