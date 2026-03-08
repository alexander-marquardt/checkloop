"""Tests for checkloop.streaming — JSONL parsing and event display."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from checkloop import streaming


class TestSummariseToolUse:
    """Tests for _summarise_tool_use() tool event formatting."""

    def test_read_file_path(self) -> None:
        result = streaming._summarise_tool_use("Read", {"file_path": "/tmp/foo.py"})
        assert "/tmp/foo.py" in result

    def test_edit_file_path(self) -> None:
        result = streaming._summarise_tool_use("Edit", {"file_path": "/a/b.txt"})
        assert "/a/b.txt" in result

    def test_write_file_path(self) -> None:
        result = streaming._summarise_tool_use("write_file", {"file_path": "/x.py"})
        assert "/x.py" in result

    def test_bash_short_command(self) -> None:
        result = streaming._summarise_tool_use("Bash", {"command": "ls -la"})
        assert "$ ls -la" in result

    def test_bash_long_command_truncated(self) -> None:
        long_cmd = "x" * 100
        result = streaming._summarise_tool_use("Bash", {"command": long_cmd})
        assert result.endswith("...")
        assert len(result) < len(long_cmd) + 10

    def test_glob_pattern(self) -> None:
        result = streaming._summarise_tool_use("Glob", {"pattern": "**/*.py"})
        assert "**/*.py" in result

    def test_grep_pattern(self) -> None:
        result = streaming._summarise_tool_use("Grep", {"pattern": "TODO"})
        assert "/TODO/" in result

    def test_unknown_tool(self) -> None:
        result = streaming._summarise_tool_use("SomeTool", {"arg": "val"})
        assert result == ""

    def test_file_path_tool_without_file_path_key(self) -> None:
        result = streaming._summarise_tool_use("Read", {"other": "val"})
        assert result == ""

    def test_bash_without_command(self) -> None:
        result = streaming._summarise_tool_use("Bash", {})
        assert result == ""

    def test_lowercase_bash(self) -> None:
        result = streaming._summarise_tool_use("bash", {"command": "echo hi"})
        assert "$ echo hi" in result

    def test_lowercase_grep(self) -> None:
        result = streaming._summarise_tool_use("grep", {"pattern": "foo"})
        assert "/foo/" in result

    def test_lowercase_glob(self) -> None:
        result = streaming._summarise_tool_use("glob", {"pattern": "*.md"})
        assert "*.md" in result

    def test_empty_tool_name(self) -> None:
        result = streaming._summarise_tool_use("", {"file_path": "/a.py"})
        assert result == ""

    def test_empty_command(self) -> None:
        result = streaming._summarise_tool_use("Bash", {"command": ""})
        assert "$ " in result

    def test_bash_command_at_display_limit(self) -> None:
        cmd = "x" * streaming._BASH_DISPLAY_LIMIT
        result = streaming._summarise_tool_use("Bash", {"command": cmd})
        assert "..." not in result
        assert cmd in result

    def test_bash_command_over_display_limit(self) -> None:
        cmd = "x" * (streaming._BASH_DISPLAY_LIMIT + 1)
        result = streaming._summarise_tool_use("Bash", {"command": cmd})
        assert result.endswith("...")


class TestSummariseToolUseNonStringInputs:
    """Edge case tests for _summarise_tool_use() with non-string tool_input values."""

    def test_bash_command_is_integer(self) -> None:
        result = streaming._summarise_tool_use("Bash", {"command": 42})
        assert "$ 42" in result

    def test_bash_command_is_none(self) -> None:
        result = streaming._summarise_tool_use("Bash", {"command": None})
        assert "$ None" in result

    def test_bash_command_is_list(self) -> None:
        result = streaming._summarise_tool_use("Bash", {"command": ["ls", "-la"]})
        assert "$ [" in result

    def test_empty_tool_input_dict(self) -> None:
        result = streaming._summarise_tool_use("Bash", {})
        assert result == ""

    def test_none_tool_name(self) -> None:
        result = streaming._summarise_tool_use("", {})
        assert result == ""


class TestPrintEvent:
    """Tests for _print_event() stream-json event rendering."""

    def _now(self) -> float:
        return time.time()

    def test_assistant_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Found issue"}]},
        }
        streaming._print_event(event, self._now())
        assert "Found issue" in capsys.readouterr().out

    def test_assistant_empty_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "   "}]},
        }
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_non_text_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "image", "url": "x"}]},
        }
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "tool": "Read", "input": {"file_path": "/a.py"}}
        streaming._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Read]" in out
        assert "/a.py" in out

    def test_tool_use_name_fallback(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
        streaming._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Bash]" in out

    def test_system_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "system", "message": "Initialising..."}
        streaming._print_event(event, self._now())
        assert "Initialising..." in capsys.readouterr().out

    def test_system_event_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "system", "message": ""}
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_result_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "result", "result": "All good"}
        streaming._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "Result" in out
        assert "All good" in out

    def test_result_event_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "result", "result": ""}
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_unknown_event_type(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "unknown_type"}
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_missing_type(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"data": "something"}
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_with_none_input(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "tool": "Read", "input": None}
        streaming._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Read]" in out

    def test_assistant_empty_content_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "assistant", "message": {"content": []}}
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_missing_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "assistant"}
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_missing_both_tool_and_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "input": {"file_path": "/a.py"}}
        streaming._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[unknown]" in out


class TestPrintEventEdgeCases:
    """Edge case tests for _print_event() with unusual inputs."""

    def test_none_type_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        event: dict[str, Any] = {"type": None}
        streaming._print_event(event, time.time())
        assert capsys.readouterr().out.strip() == ""

    def test_numeric_type_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        event: dict[str, Any] = {"type": 42}
        streaming._print_event(event, time.time())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_with_none_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        event: dict[str, Any] = {"type": "assistant", "message": {"content": None}}
        streaming._print_event(event, time.time())
        assert capsys.readouterr().out.strip() == ""

    def test_result_with_non_string_result(self, capsys: pytest.CaptureFixture[str]) -> None:
        event: dict[str, Any] = {"type": "result", "result": 42}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "42" in out


class TestProcessJsonlBuffer:
    """Tests for _process_jsonl_buffer() JSONL buffer processing."""

    def test_complete_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray((event + "\n").encode())
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "hello" in capsys.readouterr().out

    def test_partial_line_returned(self) -> None:
        buf = bytearray(b"partial")
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray(b"partial")

    def test_multiple_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        e1 = json.dumps({"type": "system", "message": "one"})
        e2 = json.dumps({"type": "system", "message": "two"})
        buf = bytearray(f"{e1}\n{e2}\n".encode())
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        out = capsys.readouterr().out
        assert "one" in out
        assert "two" in out

    def test_invalid_json_debug(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"not json\n")
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=True)
        assert remainder == bytearray()
        assert "not json" in capsys.readouterr().out

    def test_invalid_json_not_debug(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"not json\n")
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "not json" not in capsys.readouterr().out

    def test_empty_line_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"\n\n")
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_line_with_remainder(self) -> None:
        event = json.dumps({"type": "system", "message": "x"})
        buf = bytearray(f"{event}\npartial".encode())
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray(b"partial")

    def test_empty_buffer(self) -> None:
        buf = bytearray(b"")
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray(b"")

    def test_whitespace_only_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"   \n\t\n")
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_unicode_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "こんにちは 🎉"})
        buf = bytearray((event + "\n").encode("utf-8"))
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "こんにちは" in capsys.readouterr().out


class TestProcessJsonlBufferInvalidUtf8:
    """Edge case: _process_jsonl_buffer with invalid UTF-8 bytes."""

    def test_invalid_utf8_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"\xff\xfe invalid\n")
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=True)
        assert remainder == bytearray()

    def test_valid_utf8_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "café ñ ü"})
        buf = bytearray((event + "\n").encode("utf-8"))
        remainder = streaming._process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "café" in capsys.readouterr().out
