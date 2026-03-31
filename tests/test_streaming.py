"""Tests for checkloop.streaming — event display and tool-use summaries.

Buffer processing tests (process_jsonl_buffer) live in test_streaming_buffer.py.
"""

from __future__ import annotations

import time
import unittest.mock
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


class TestSummariseToolUseExceptionHandling:
    """Test that _summarise_tool_use catches exceptions gracefully."""

    def test_tool_input_raising_exception_returns_empty(self) -> None:
        bad_input: Any = unittest.mock.MagicMock()
        bad_input.__contains__ = unittest.mock.MagicMock(side_effect=TypeError("boom"))
        bad_input.__getitem__ = unittest.mock.MagicMock(side_effect=TypeError("boom"))
        result = streaming._summarise_tool_use("Bash", bad_input)
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

    def test_result_event_not_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Result text is not printed because it duplicates streamed assistant events."""
        event = {"type": "result", "result": "All good"}
        streaming._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

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



class TestSummariseToolUseAdditionalEdgeCases:
    """Additional edge case tests for _summarise_tool_use."""

    def test_bash_command_one_under_limit(self) -> None:
        cmd = "x" * (streaming._BASH_DISPLAY_LIMIT - 1)
        result = streaming._summarise_tool_use("Bash", {"command": cmd})
        assert "..." not in result

    def test_read_file_with_empty_file_path(self) -> None:
        result = streaming._summarise_tool_use("Read", {"file_path": ""})
        assert result == " "

    def test_grep_with_empty_pattern(self) -> None:
        result = streaming._summarise_tool_use("Grep", {"pattern": ""})
        assert result == " //"

    def test_case_variants_of_file_tools(self) -> None:
        assert streaming._summarise_tool_use("READ_FILE", {"file_path": "/a"}) == " /a"
        assert streaming._summarise_tool_use("EDIT", {"file_path": "/b"}) == " /b"
        assert streaming._summarise_tool_use("WRITE", {"file_path": "/c"}) == " /c"




