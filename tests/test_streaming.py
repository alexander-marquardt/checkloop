"""Tests for checkloop.streaming — JSONL parsing and event display."""

from __future__ import annotations

import json
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
        """A buffer with only a newline produces no output and clears."""
        buf = bytearray(b"\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_multiple_consecutive_newlines(self) -> None:
        """Multiple newlines are treated as empty lines and skipped."""
        buf = bytearray(b"\n\n\n\n")
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_json_with_escaped_newlines_in_string(self, capsys: pytest.CaptureFixture[str]) -> None:
        """JSON value containing escaped newlines should parse correctly."""
        event = json.dumps({"type": "system", "message": "line1\\nline2"})
        buf = bytearray((event + "\n").encode())
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        assert "line1" in capsys.readouterr().out


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


class TestProcessJsonlBufferMixedContent:
    """Tests for process_jsonl_buffer with mixed valid/invalid content."""

    def test_mixed_valid_and_invalid(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Mix of valid and invalid lines should process valid ones."""
        valid_event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray(f"invalid\n{valid_event}\n".encode())
        result = streaming.process_jsonl_buffer(buf, 0.0, False)
        assert result == bytearray(b"")
        output = capsys.readouterr().out
        assert "hello" in output


class TestProcessJsonlBufferEventErrors:
    """Tests for process_jsonl_buffer when _print_event raises TypeError/KeyError/AttributeError."""

    def test_type_error_in_print_event_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When _print_event raises TypeError, the line is logged and processing continues."""
        valid_event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray((valid_event + "\n").encode())
        with unittest.mock.patch.object(streaming, "_print_event", side_effect=TypeError("bad type")):
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_key_error_in_print_event_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When _print_event raises KeyError, the line is logged and processing continues."""
        valid_event = json.dumps({"type": "tool_use", "tool": "Read"})
        buf = bytearray((valid_event + "\n").encode())
        with unittest.mock.patch.object(streaming, "_print_event", side_effect=KeyError("missing key")):
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_attribute_error_in_print_event_logged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When _print_event raises AttributeError, the line is logged and processing continues."""
        valid_event = json.dumps({"type": "result", "result": "done"})
        buf = bytearray((valid_event + "\n").encode())
        with unittest.mock.patch.object(streaming, "_print_event", side_effect=AttributeError("no attr")):
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()


class TestProcessJsonlBufferNonDictJson:
    """Edge cases for process_jsonl_buffer with valid JSON that isn't a dict."""

    def test_json_array_skipped_without_exception(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A JSON array is valid JSON but not a stream event dict — skipped via type check."""
        buf = bytearray(b'[1, 2, 3]\n')
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()
        # Verify no output was produced (the non-dict value is silently skipped)
        assert capsys.readouterr().out == ""

    def test_json_number_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A JSON number is valid JSON but not a dict."""
        buf = bytearray(b'42\n')
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_json_string_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A JSON string is valid JSON but not a dict."""
        buf = bytearray(b'"hello"\n')
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_json_null_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A JSON null is valid JSON but not a dict."""
        buf = bytearray(b'null\n')
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

    def test_json_boolean_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b'true\n')
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)

    def test_non_dict_json_does_not_call_print_event(self) -> None:
        """Non-dict JSON values should be skipped before reaching _print_event."""
        buf = bytearray(b'42\n"hello"\n[1,2]\nnull\ntrue\n')
        with unittest.mock.patch.object(streaming, "_print_event") as mock_print:
            remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
            mock_print.assert_not_called()
        assert remainder == bytearray()


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
