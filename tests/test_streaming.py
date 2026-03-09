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
        """When tool_input causes an internal error, the function returns ''."""
        # Pass a tool_input whose __contains__ raises, triggering the except branch
        bad_input: Any = unittest.mock.MagicMock()
        bad_input.__contains__ = unittest.mock.MagicMock(side_effect=TypeError("boom"))
        bad_input.__getitem__ = unittest.mock.MagicMock(side_effect=TypeError("boom"))
        result = streaming._summarise_tool_use("Bash", bad_input)
        assert result == ""


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

    def test_assistant_content_is_string_not_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Content should be a list; a string is silently ignored."""
        event: dict[str, Any] = {"type": "assistant", "message": {"content": "plain text"}}
        streaming._print_event(event, time.time())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_content_contains_non_dict_items(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-dict items in content list are safely skipped."""
        event: dict[str, Any] = {"type": "assistant", "message": {"content": ["string", 42, None, {"type": "text", "text": "real"}]}}
        streaming._print_event(event, time.time())
        assert "real" in capsys.readouterr().out

    def test_result_with_non_string_result(self, capsys: pytest.CaptureFixture[str]) -> None:
        event: dict[str, Any] = {"type": "result", "result": 42}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "42" in out


class TestSummariseToolUseAdditionalEdgeCases:
    """Additional edge case tests for _summarise_tool_use."""

    def test_bash_command_one_under_limit(self) -> None:
        """Command one char under the limit should not be truncated."""
        cmd = "x" * (streaming._BASH_DISPLAY_LIMIT - 1)
        result = streaming._summarise_tool_use("Bash", {"command": cmd})
        assert "..." not in result

    def test_read_file_with_empty_file_path(self) -> None:
        """Empty file_path string is still returned."""
        result = streaming._summarise_tool_use("Read", {"file_path": ""})
        assert result == " "

    def test_grep_with_empty_pattern(self) -> None:
        """Empty grep pattern produces valid output."""
        result = streaming._summarise_tool_use("Grep", {"pattern": ""})
        assert result == " //"

    def test_case_variants_of_file_tools(self) -> None:
        """read_file, EDIT, Write_File etc. all match via lowercasing."""
        assert streaming._summarise_tool_use("READ_FILE", {"file_path": "/a"}) == " /a"
        assert streaming._summarise_tool_use("EDIT", {"file_path": "/b"}) == " /b"
        assert streaming._summarise_tool_use("WRITE", {"file_path": "/c"}) == " /c"


class TestPrintToolUseEventEdgeCases:
    """Edge cases for _print_tool_use_event() with unusual input types."""

    def test_non_dict_input_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When event['input'] is a string, should not crash."""
        event: dict[str, Any] = {"type": "tool_use", "tool": "bash", "input": "not a dict"}
        streaming._print_tool_use_event(event, "[0m00s] ")
        output = capsys.readouterr().out
        assert "bash" in output

    def test_list_input_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When event['input'] is a list, should not crash."""
        event: dict[str, Any] = {"type": "tool_use", "tool": "read", "input": [1, 2, 3]}
        streaming._print_tool_use_event(event, "[0m00s] ")
        output = capsys.readouterr().out
        assert "read" in output

    def test_missing_input_field(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When event has no 'input' key, should not crash."""
        event: dict[str, Any] = {"type": "tool_use", "tool": "bash"}
        streaming._print_tool_use_event(event, "[0m00s] ")
        output = capsys.readouterr().out
        assert "bash" in output


class TestPrintEventEdgeCasesAdditional:
    """Additional edge cases for _print_event()."""

    def test_assistant_message_is_none(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When message is None (not a dict), should not crash."""
        event: dict[str, Any] = {"type": "assistant", "message": None}
        streaming._print_event(event, time.time())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_message_is_string(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When message is a string (not a dict), should not crash."""
        event: dict[str, Any] = {"type": "assistant", "message": "just a string"}
        streaming._print_event(event, time.time())
        assert capsys.readouterr().out.strip() == ""

    def test_result_event_with_dict_result(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When result is a dict, it should be printed (via str conversion)."""
        event: dict[str, Any] = {"type": "result", "result": {"key": "value"}}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "Result" in out
        assert "key" in out

    def test_system_event_with_non_string_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When system message is a non-string truthy value, it should be printed."""
        event: dict[str, Any] = {"type": "system", "message": 42}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "42" in out


class TestPrintResultEventFalsyValues:
    """Edge cases for _print_result_event with falsy but valid result values."""

    def test_result_zero_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Numeric 0 is a valid result and should be printed."""
        event: dict[str, Any] = {"type": "result", "result": 0}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "Result" in out
        assert "0" in out

    def test_result_false_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Boolean False is a valid result and should be printed."""
        event: dict[str, Any] = {"type": "result", "result": False}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "Result" in out
        assert "False" in out

    def test_result_none_is_not_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """None result should not be printed."""
        event: dict[str, Any] = {"type": "result", "result": None}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "Result" not in out

    def test_result_empty_string_is_not_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Empty string result should not be printed."""
        event: dict[str, Any] = {"type": "result", "result": ""}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "Result" not in out

    def test_result_missing_key_is_not_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When 'result' key is missing entirely, nothing should be printed."""
        event: dict[str, Any] = {"type": "result"}
        streaming._print_event(event, time.time())
        out = capsys.readouterr().out
        assert "Result" not in out


