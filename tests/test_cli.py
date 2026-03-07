"""Comprehensive tests for claudeloop.cli — targeting >=90% line coverage."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

import pytest

from claudeloop import cli


# =============================================================================
# Shared test helpers
# =============================================================================

def _parse_cli(args: list[str]) -> argparse.Namespace:
    """Parse CLI args, auto-adding --dir /tmp if not provided."""
    if "--dir" not in args and "-d" not in args:
        args = ["--dir", "/tmp"] + args
    return cli._build_argument_parser().parse_args(args)


_SHARED_ARG_DEFAULTS: dict[str, Any] = dict(
    pause=0,
    idle_timeout=cli.DEFAULT_IDLE_TIMEOUT,
    verbose=False,
    dangerously_skip_permissions=False,
)


def _make_suite_args(*, dry_run: bool = True, **overrides: Any) -> argparse.Namespace:
    """Build an argparse.Namespace for _run_review_suite / _run_single_pass tests."""
    defaults = {**_SHARED_ARG_DEFAULTS, "dry_run": dry_run}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _make_main_mock_args(*, dry_run: bool = False, **overrides: Any) -> mock.MagicMock:
    """Build a MagicMock with all attributes main() reads from parsed args."""
    args = mock.MagicMock()
    defaults = {
        **_SHARED_ARG_DEFAULTS,
        "debug": False,
        "dir": "/tmp",
        "cycles": 1,
        "converged_at_percentage": cli.DEFAULT_CONVERGENCE_THRESHOLD,
        "all_passes": False,
        "passes": ["readability"],
        "level": None,
        "dry_run": dry_run,
    }
    defaults.update(overrides)
    for key, value in defaults.items():
        setattr(args, key, value)
    return args


@contextlib.contextmanager
def _patch_suite_git(
    sha_sequence: list[str],
    *,
    run_claude_return: int = 0,
    lines_changed: int | None = None,
    total_tracked: int | None = None,
) -> Iterator[None]:
    """Mock common git/claude dependencies for _run_review_suite tests.

    Patches run_claude, _is_git_repo, and _git_head_sha. Optionally patches
    _count_lines_changed and _cached_total_tracked_lines when provided.
    """
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(cli, "run_claude", return_value=run_claude_return))
        stack.enter_context(mock.patch.object(cli, "_is_git_repo", return_value=True))
        stack.enter_context(mock.patch.object(cli, "_git_head_sha", side_effect=sha_sequence))
        if lines_changed is not None:
            stack.enter_context(mock.patch.object(cli, "_count_lines_changed", return_value=lines_changed))
        if total_tracked is not None:
            stack.enter_context(mock.patch.object(cli, "_cached_total_tracked_lines", return_value=total_tracked))
        yield


@contextlib.contextmanager
def _patch_main_pipeline(
    *,
    suite_side_effect: type[BaseException] | BaseException | None = None,
    **arg_overrides: Any,
) -> Iterator[None]:
    """Mock the standard main() pipeline for exception-path tests.

    Patches argument parsing, directory resolution, validation, the pre-run
    warning, and the review suite.  *suite_side_effect* is passed through
    to ``_run_review_suite``'s mock so callers can inject exceptions.
    """
    mock_args = _make_main_mock_args(**arg_overrides)
    suite_kwargs: dict[str, Any] = {}
    if suite_side_effect is not None:
        suite_kwargs["side_effect"] = suite_side_effect
    with mock.patch.object(cli, "_build_argument_parser") as mock_parser:
        mock_parser.return_value.parse_args.return_value = mock_args
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli, "_resolve_working_directory", return_value="/tmp"))
            stack.enter_context(mock.patch.object(cli, "_validate_arguments"))
            stack.enter_context(mock.patch.object(cli, "_display_pre_run_warning"))
            stack.enter_context(mock.patch.object(cli, "_run_review_suite", **suite_kwargs))
            yield


# =============================================================================
# banner / log
# =============================================================================

class TestPrintBanner:
    """Tests for the print_banner() terminal output helper."""

    def test_default_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli.print_banner("Hello")
        out = capsys.readouterr().out
        assert "Hello" in out
        assert cli.CYAN in out
        assert cli.BOLD in out
        assert cli.RESET in out

    def test_custom_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli.print_banner("Title", cli.GREEN)
        out = capsys.readouterr().out
        assert cli.GREEN in out
        assert "Title" in out


class TestPrintStatus:
    """Tests for the print_status() terminal output helper."""

    def test_default_dim(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli.print_status("info")
        out = capsys.readouterr().out
        assert "info" in out
        assert cli.DIM in out

    def test_custom_colour(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli.print_status("warn", cli.YELLOW)
        out = capsys.readouterr().out
        assert cli.YELLOW in out


# =============================================================================
# _format_duration
# =============================================================================

class TestFormatDuration:
    """Tests for _format_duration() time formatting."""

    def test_zero(self) -> None:
        assert cli._format_duration(0) == "0m00s"

    def test_seconds_only(self) -> None:
        assert cli._format_duration(45) == "0m45s"

    def test_minutes_and_seconds(self) -> None:
        assert cli._format_duration(125) == "2m05s"

    def test_exactly_one_hour(self) -> None:
        assert cli._format_duration(3600) == "1h00m00s"

    def test_hours_minutes_seconds(self) -> None:
        assert cli._format_duration(3661) == "1h01m01s"

    def test_large_value(self) -> None:
        assert cli._format_duration(7384) == "2h03m04s"

    def test_very_large_value(self) -> None:
        assert cli._format_duration(360000) == "100h00m00s"

    def test_just_under_one_hour(self) -> None:
        assert cli._format_duration(3599) == "59m59s"

    def test_negative_clamped_to_zero(self) -> None:
        assert cli._format_duration(-5) == "0m00s"

    def test_negative_large_clamped_to_zero(self) -> None:
        assert cli._format_duration(-9999) == "0m00s"

    def test_fractional_seconds(self) -> None:
        assert cli._format_duration(0.9) == "0m00s"

    def test_fractional_just_under_minute(self) -> None:
        assert cli._format_duration(59.999) == "0m59s"


# =============================================================================
# _looks_dangerous
# =============================================================================

class TestLooksDangerous:
    """Tests for the _looks_dangerous() prompt safety guard."""

    def test_safe_prompt(self) -> None:
        assert cli._looks_dangerous("Review all code for quality") is False

    def test_rm_rf_root(self) -> None:
        assert cli._looks_dangerous("rm -rf /") is True

    def test_case_insensitive(self) -> None:
        assert cli._looks_dangerous("DROP DATABASE users") is True

    def test_drop_table(self) -> None:
        assert cli._looks_dangerous("drop table foo") is True

    def test_sudo_rm(self) -> None:
        assert cli._looks_dangerous("sudo rm something") is True

    def test_fork_bomb(self) -> None:
        assert cli._looks_dangerous(":(){:|:&};:") is True

    def test_dd_dev_zero(self) -> None:
        assert cli._looks_dangerous("dd if=/dev/zero of=/dev/sda") is True

    def test_chmod_777_root(self) -> None:
        assert cli._looks_dangerous("chmod 777 /") is True

    def test_delete_all(self) -> None:
        assert cli._looks_dangerous("delete all files") is True

    def test_truncate(self) -> None:
        assert cli._looks_dangerous("truncate the table") is True

    def test_format_keyword(self) -> None:
        assert cli._looks_dangerous("format c: drive") is True
        assert cli._looks_dangerous("format /dev/sda") is True

    def test_wipe(self) -> None:
        assert cli._looks_dangerous("wipe everything") is True

    def test_etc_passwd(self) -> None:
        assert cli._looks_dangerous("cat /etc/passwd") is True

    def test_embedded_safe_word(self) -> None:
        # "mkfs" as a standalone word should match, but not as a substring
        assert cli._looks_dangerous("run mkfs on disk") is True
        assert cli._looks_dangerous("format the code") is False

    def test_format_dev(self) -> None:
        assert cli._looks_dangerous("format /dev/sda") is True

    def test_dd_of_dev(self) -> None:
        assert cli._looks_dangerous("dd of=/dev/sda bs=1M") is True

    def test_empty_string(self) -> None:
        assert cli._looks_dangerous("") is False

    def test_whitespace_only(self) -> None:
        assert cli._looks_dangerous("   \t\n  ") is False

    def test_unicode_prompt(self) -> None:
        assert cli._looks_dangerous("レビューコード 🔍") is False


# =============================================================================
# _summarise_tool_use
# =============================================================================

class TestSummariseToolUse:
    """Tests for _summarise_tool_use() tool event formatting."""

    def test_read_file_path(self) -> None:
        result = cli._summarise_tool_use("Read", {"file_path": "/tmp/foo.py"})
        assert "/tmp/foo.py" in result

    def test_edit_file_path(self) -> None:
        result = cli._summarise_tool_use("Edit", {"file_path": "/a/b.txt"})
        assert "/a/b.txt" in result

    def test_write_file_path(self) -> None:
        result = cli._summarise_tool_use("write_file", {"file_path": "/x.py"})
        assert "/x.py" in result

    def test_bash_short_command(self) -> None:
        result = cli._summarise_tool_use("Bash", {"command": "ls -la"})
        assert "$ ls -la" in result

    def test_bash_long_command_truncated(self) -> None:
        long_cmd = "x" * 100
        result = cli._summarise_tool_use("Bash", {"command": long_cmd})
        assert result.endswith("...")
        assert len(result) < len(long_cmd) + 10

    def test_glob_pattern(self) -> None:
        result = cli._summarise_tool_use("Glob", {"pattern": "**/*.py"})
        assert "**/*.py" in result

    def test_grep_pattern(self) -> None:
        result = cli._summarise_tool_use("Grep", {"pattern": "TODO"})
        assert "/TODO/" in result

    def test_unknown_tool(self) -> None:
        result = cli._summarise_tool_use("SomeTool", {"arg": "val"})
        assert result == ""

    def test_file_path_tool_without_file_path_key(self) -> None:
        result = cli._summarise_tool_use("Read", {"other": "val"})
        assert result == ""

    def test_bash_without_command(self) -> None:
        result = cli._summarise_tool_use("Bash", {})
        assert result == ""

    def test_lowercase_bash(self) -> None:
        result = cli._summarise_tool_use("bash", {"command": "echo hi"})
        assert "$ echo hi" in result

    def test_lowercase_grep(self) -> None:
        result = cli._summarise_tool_use("grep", {"pattern": "foo"})
        assert "/foo/" in result

    def test_lowercase_glob(self) -> None:
        result = cli._summarise_tool_use("glob", {"pattern": "*.md"})
        assert "*.md" in result

    def test_empty_tool_name(self) -> None:
        result = cli._summarise_tool_use("", {"file_path": "/a.py"})
        assert result == ""

    def test_empty_command(self) -> None:
        result = cli._summarise_tool_use("Bash", {"command": ""})
        assert "$ " in result

    def test_bash_command_at_display_limit(self) -> None:
        cmd = "x" * cli._BASH_DISPLAY_LIMIT
        result = cli._summarise_tool_use("Bash", {"command": cmd})
        assert "..." not in result
        assert cmd in result

    def test_bash_command_over_display_limit(self) -> None:
        cmd = "x" * (cli._BASH_DISPLAY_LIMIT + 1)
        result = cli._summarise_tool_use("Bash", {"command": cmd})
        assert result.endswith("...")


# =============================================================================
# _print_event
# =============================================================================

class TestPrintEvent:
    """Tests for _print_event() stream-json event rendering."""

    def _now(self) -> float:
        return time.time()

    def test_assistant_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Found issue"}]},
        }
        cli._print_event(event, self._now())
        assert "Found issue" in capsys.readouterr().out

    def test_assistant_empty_text(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "   "}]},
        }
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_non_text_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "image", "url": "x"}]},
        }
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "tool": "Read", "input": {"file_path": "/a.py"}}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Read]" in out
        assert "/a.py" in out

    def test_tool_use_name_fallback(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Bash]" in out

    def test_system_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "system", "message": "Initialising..."}
        cli._print_event(event, self._now())
        assert "Initialising..." in capsys.readouterr().out

    def test_system_event_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "system", "message": ""}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_result_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "result", "result": "All good"}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "Result" in out
        assert "All good" in out

    def test_result_event_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "result", "result": ""}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_unknown_event_type(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "unknown_type"}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_missing_type(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"data": "something"}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_with_none_input(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "tool": "Read", "input": None}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Read]" in out

    def test_assistant_empty_content_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "assistant", "message": {"content": []}}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_missing_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "assistant"}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_missing_both_tool_and_name(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = {"type": "tool_use", "input": {"file_path": "/a.py"}}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[unknown]" in out


# =============================================================================
# _process_jsonl_buffer (JSONL line processing)
# =============================================================================

class TestProcessJsonlBuffer:
    """Tests for _process_jsonl_buffer() JSONL buffer processing."""

    def test_complete_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray((event + "\n").encode())
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert "hello" in capsys.readouterr().out

    def test_partial_line_returned(self) -> None:
        buf = bytearray(b"partial")
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray(b"partial")

    def test_multiple_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        e1 = json.dumps({"type": "system", "message": "one"})
        e2 = json.dumps({"type": "system", "message": "two"})
        buf = bytearray(f"{e1}\n{e2}\n".encode())
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        out = capsys.readouterr().out
        assert "one" in out
        assert "two" in out

    def test_invalid_json_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"not json\n")
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=True)
        assert remainder == bytearray()
        assert "not json" in capsys.readouterr().out

    def test_invalid_json_not_verbose(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"not json\n")
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        # Should NOT print non-JSON when verbose is False
        assert "not json" not in capsys.readouterr().out

    def test_empty_line_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"\n\n")
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_line_with_remainder(self) -> None:
        event = json.dumps({"type": "system", "message": "x"})
        buf = bytearray(f"{event}\npartial".encode())
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray(b"partial")

    def test_empty_buffer(self) -> None:
        buf = bytearray(b"")
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray(b"")

    def test_whitespace_only_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        buf = bytearray(b"   \n\t\n")
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_unicode_content(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "こんにちは 🎉"})
        buf = bytearray((event + "\n").encode("utf-8"))
        remainder = cli._process_jsonl_buffer(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert "こんにちは" in capsys.readouterr().out


# =============================================================================
# _build_claude_command
# =============================================================================

class TestBuildClaudeCommand:
    """Tests for _build_claude_command() CLI argument assembly."""

    def test_with_skip_permissions(self) -> None:
        cmd = cli._build_claude_command("review code", skip_permissions=True)
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "-p" in cmd
        assert "review code" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_without_skip_permissions(self) -> None:
        cmd = cli._build_claude_command("review code", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd
        assert "review code" in cmd

    def test_default_skip_permissions_is_false(self) -> None:
        """skip_permissions defaults to False (safe by default)."""
        cmd = cli._build_claude_command("review code", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd


# =============================================================================
# _spawn_claude_process
# =============================================================================

class TestSpawnClaudeProcess:
    """Tests for _spawn_claude_process() subprocess creation."""

    def test_file_not_found_exits(self) -> None:
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            with pytest.raises(SystemExit) as exc_info:
                cli._spawn_claude_process(["claude", "-p", "hi"], "/tmp")
            assert exc_info.value.code == 1

    def test_strips_claudecode_env(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "HOME": "/home"}):
            with mock.patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = mock.MagicMock()
                cli._spawn_claude_process(["claude"], "/tmp")
                call_kwargs = mock_popen.call_args
                env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
                assert "CLAUDECODE" not in env

    def test_passes_cwd_and_pipes(self) -> None:
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock.MagicMock()
            cli._spawn_claude_process(["claude"], "/mydir")
            call_kwargs = mock_popen.call_args
            assert call_kwargs.kwargs["cwd"] == "/mydir"
            assert call_kwargs.kwargs["stdout"] == subprocess.PIPE
            assert call_kwargs.kwargs["stderr"] == subprocess.DEVNULL
            assert call_kwargs.kwargs["stdin"] == subprocess.DEVNULL


# =============================================================================
# run_claude
# =============================================================================

class TestRunClaude:
    """Tests for the public run_claude() function."""

    def test_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.run_claude("prompt", "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_normal_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.stdout = io.BytesIO(b'')
        mock_proc.stderr = io.BytesIO(b'')
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0

        with mock.patch.object(cli, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(cli, "_stream_process_output", return_value=time.time()):
                code = cli.run_claude("prompt", "/tmp", dry_run=False)
        assert code == 0

    def test_dry_run_short_prompt_no_ellipsis(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.run_claude("short", "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "short" in out
        assert "short..." not in out

    def test_dry_run_long_prompt_truncated(self, capsys: pytest.CaptureFixture[str]) -> None:
        long_prompt = "x" * 200
        code = cli.run_claude(long_prompt, "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "..." in out

    def test_dry_run_empty_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = cli.run_claude("", "/tmp", dry_run=True)
        assert code == 0

    def test_nonzero_exit(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 1

        with mock.patch.object(cli, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(cli, "_stream_process_output", return_value=time.time()):
                mock_proc.wait.return_value = None
                code = cli.run_claude("prompt", "/tmp", dry_run=False)
        assert code == 1
        out = capsys.readouterr().out
        assert "exited with code 1" in out


# =============================================================================
# _build_argument_parser
# =============================================================================

class TestBuildArgumentParser:
    """Tests for _build_argument_parser() CLI flag parsing."""

    def test_defaults(self) -> None:
        ns = _parse_cli([])
        assert ns.dir == "/tmp"
        assert ns.passes is None
        assert ns.level is None
        assert ns.all_passes is False
        assert ns.cycles == 1
        assert ns.idle_timeout == cli.DEFAULT_IDLE_TIMEOUT
        assert ns.dry_run is False
        assert ns.verbose is False
        assert ns.pause == cli.DEFAULT_PAUSE_SECONDS

    def test_dir_is_required(self) -> None:
        with pytest.raises(SystemExit):
            cli._build_argument_parser().parse_args([])

    def test_dir(self) -> None:
        ns = _parse_cli(["--dir", "/foo"])
        assert ns.dir == "/foo"

    def test_dir_short(self) -> None:
        ns = _parse_cli(["-d", "/bar"])
        assert ns.dir == "/bar"

    def test_passes(self) -> None:
        ns = _parse_cli(["--passes", "readability", "security"])
        assert ns.passes == ["readability", "security"]

    def test_all_passes(self) -> None:
        ns = _parse_cli(["--all-passes"])
        assert ns.all_passes is True

    def test_cycles(self) -> None:
        ns = _parse_cli(["--cycles", "5"])
        assert ns.cycles == 5

    def test_cycles_short(self) -> None:
        ns = _parse_cli(["-c", "3"])
        assert ns.cycles == 3

    def test_idle_timeout(self) -> None:
        ns = _parse_cli(["--idle-timeout", "300"])
        assert ns.idle_timeout == 300

    def test_dry_run(self) -> None:
        ns = _parse_cli(["--dry-run"])
        assert ns.dry_run is True

    def test_verbose(self) -> None:
        ns = _parse_cli(["--verbose"])
        assert ns.verbose is True

    def test_verbose_short(self) -> None:
        ns = _parse_cli(["-v"])
        assert ns.verbose is True

    def test_pause(self) -> None:
        ns = _parse_cli(["--pause", "10"])
        assert ns.pause == 10

    def test_invalid_pass_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_cli(["--passes", "nonexistent"])


# =============================================================================
# _print_run_summary
# =============================================================================

class TestPrintRunSummary:
    """Tests for _print_run_summary() pre-run output."""

    def test_normal_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "readability", "label": "Readability"}]
        cli._print_run_summary("/tmp", passes, 2, 2, 120, False)
        out = capsys.readouterr().out
        assert "claudeloop" in out
        assert "/tmp" in out
        assert "readability" in out
        assert "2" in out

    def test_dry_run_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "tests", "label": "Tests"}]
        cli._print_run_summary("/tmp", passes, 1, 1, 120, True)
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_single_cycle_no_plural(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 1, 1, 60, False)
        out = capsys.readouterr().out
        assert "1 cycle)" in out

    def test_multiple_cycles_plural(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 3, 3, 60, False)
        out = capsys.readouterr().out
        assert "cycles)" in out

    def test_empty_passes_list(self, capsys: pytest.CaptureFixture[str]) -> None:
        cli._print_run_summary("/dir", [], 1, 0, 60, False)
        out = capsys.readouterr().out
        assert "Total steps  : 0" in out


# =============================================================================
# _run_review_suite
# =============================================================================

class TestRunReviewSuite:
    """Tests for _run_review_suite() multi-pass execution."""

    def test_single_pass_single_cycle(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args()
        cli._run_review_suite(passes, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "Readability" in out
        assert "DRY RUN" in out

    def test_multi_cycle_banner(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = _make_suite_args()
        cli._run_review_suite(passes, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out

    def test_dangerous_prompt_skipped(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "evil", "label": "Evil", "prompt": "rm -rf / everything"}]
        args = _make_suite_args(dry_run=False)
        with mock.patch.object(cli, "run_claude") as mock_run:
            cli._run_review_suite(passes, 1, "/tmp", args)
            mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "dangerous" in out.lower() or "Skipping" in out

    def test_nonzero_exit_continues(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [
            {"id": "a", "label": "A", "prompt": "do a"},
            {"id": "b", "label": "B", "prompt": "do b"},
        ]
        args = _make_suite_args(dry_run=False)
        with mock.patch.object(cli, "run_claude", return_value=1):
            cli._run_review_suite(passes, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "exited with code 1" in out
        # Both passes should be attempted
        assert "A" in out
        assert "B" in out

    def test_noop_passes_skipped_on_cycle2(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Passes that made no changes in cycle 1 are skipped in cycle 2."""
        passes = [
            {"id": "readability", "label": "Readability", "prompt": "review code"},
            {"id": "dry", "label": "DRY", "prompt": "check dry"},
        ]
        args = _make_suite_args(dry_run=False)
        sha_sequence = [
            "sha_r_before", "sha_r_after",    # Cycle 1 readability: changed
            "sha_d_before", "sha_d_before",    # Cycle 1 dry: no change
            "sha_r2_before", "sha_r2_after",   # Cycle 2 readability: runs
        ]
        with _patch_suite_git(sha_sequence, lines_changed=10, total_tracked=1000):
            cli._run_review_suite(passes, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Skipping 1 pass(es)" in out

    def test_bookend_passes_always_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Bookend passes (test-fix, test-validate) run even if they made no changes."""
        passes = [
            {"id": "test-fix", "label": "Test Fix", "prompt": "fix tests"},
            {"id": "readability", "label": "Readability", "prompt": "review code"},
            {"id": "test-validate", "label": "Test Validate", "prompt": "validate tests"},
        ]
        args = _make_suite_args(dry_run=False)
        sha_sequence = [
            "s1", "s1",  "s2", "s2",  "s3", "s3",  # Cycle 1: all no-change
            "s4", "s4",  "s5", "s5",                 # Cycle 2: bookends only
        ]
        with _patch_suite_git(sha_sequence):
            cli._run_review_suite(passes, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Skipping 1 pass(es)" in out
        assert out.count("Test Fix") == 2
        assert out.count("Test Validate") == 2
        assert out.count("Readability") == 1

    def test_all_passes_active_no_skip(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When all passes make changes, none are skipped in cycle 2."""
        passes = [
            {"id": "readability", "label": "Readability", "prompt": "review code"},
            {"id": "dry", "label": "DRY", "prompt": "check dry"},
        ]
        args = _make_suite_args(dry_run=False)
        sha_sequence = [
            "a1", "a2",  "b1", "b2",  # Cycle 1: both change
            "c1", "c2",  "d1", "d2",  # Cycle 2: both run again
        ]
        with _patch_suite_git(sha_sequence, lines_changed=10, total_tracked=1000):
            cli._run_review_suite(passes, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Skipping" not in out

    def test_pass_change_stats_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """After each pass that makes changes, lines changed and percentage are printed."""
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["sha1", "sha2"], lines_changed=42, total_tracked=5000):
            cli._run_review_suite(passes, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "42 lines changed" in out
        assert "0.84%" in out

    def test_no_change_stats_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """After a pass with no changes, 'no changes' is printed."""
        passes = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["same", "same"]):
            cli._run_review_suite(passes, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "dry: no changes" in out


# =============================================================================
# main
# =============================================================================

class TestMain:
    """Tests for the main() CLI entry point."""

    def test_nonexistent_dir_exits(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", "/nonexistent_xyz_abc"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_idle_timeout_zero_exits(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--idle-timeout", "0"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_negative_pause_exits(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--pause", "-1"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_dry_run_full(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "All done" in out

    def test_all_passes_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--all-passes", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        for p in cli.REVIEW_PASSES:
            assert p["label"] in out

    def test_cycles_zero_exits(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--cycles", "0"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_negative_cycles_exits(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--cycles", "-1"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_specific_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--passes", "security", "perf", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "Security" in out
        assert "Performance" in out


# =============================================================================
# Module-level constants
# =============================================================================

class TestConstants:
    """Tests for module-level constants and data structures."""

    def test_pass_ids_match(self) -> None:
        assert cli.PASS_IDS == [p["id"] for p in cli.REVIEW_PASSES]

    def test_default_tier_passes_are_valid(self) -> None:
        for pass_id in cli.TIERS[cli.DEFAULT_TIER]:
            assert pass_id in cli.PASS_IDS

    def test_all_passes_have_required_keys(self) -> None:
        for p in cli.REVIEW_PASSES:
            assert "id" in p
            assert "label" in p
            assert "prompt" in p

    def test_file_path_tools_set(self) -> None:
        assert "read" in cli._FILE_PATH_TOOL_NAMES
        assert "edit" in cli._FILE_PATH_TOOL_NAMES
        assert "write" in cli._FILE_PATH_TOOL_NAMES


# =============================================================================
# _stream_process_output (integration-ish, with mocked process)
# =============================================================================

class TestStreamProcessOutput:
    """Tests for _stream_process_output() with mocked subprocesses."""

    def test_process_exits_immediately(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "done"})
        mock_proc.stdout = io.BytesIO(f"{event}\n".encode())
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = 0
        # select.select should indicate data ready
        with mock.patch("select.select", return_value=([mock_proc.stdout], [], [])):
            start = cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        assert isinstance(start, float)
        out = capsys.readouterr().out
        assert "done" in out

    def test_idle_timeout_kills(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        # select never returns data → triggers idle timeout
        with mock.patch("select.select", return_value=([], [], [])):
            with mock.patch("time.time") as mock_time:
                # First call: start time, second: last_output_time, third: now (idle check)
                mock_time.side_effect = [
                    100.0,   # pass_start_time
                    100.0,   # last_output_time
                    250.0,   # now > last_output_time + idle_timeout
                ]
                # stdout.read should return b"" for the finally block
                mock_proc.stdout.read.return_value = b""
                with mock.patch("os.getpgid", return_value=12345):
                    with mock.patch("os.killpg"):
                        cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        out = capsys.readouterr().out
        assert "Idle" in out

    def test_read1_used_when_available(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "hi"})
        data = f"{event}\n".encode()

        mock_stdout = mock.MagicMock()
        mock_stdout.read1 = mock.MagicMock(side_effect=[data, b""])
        mock_stdout.read = mock.MagicMock(return_value=b"")
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.side_effect = [None, None, 0]

        with mock.patch("select.select", side_effect=[
            ([mock_stdout], [], []),
            ([mock_stdout], [], []),
        ]):
            cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        mock_stdout.read1.assert_called()

    def test_os_read_fallback(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "yo"})
        data = f"{event}\n".encode()

        # Stdout without read1 attribute
        mock_stdout = mock.MagicMock(spec=["fileno", "read", "close"])
        mock_stdout.fileno.return_value = 99
        mock_stdout.read.return_value = b""
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.side_effect = [None, 0]

        with mock.patch("select.select", return_value=([mock_stdout], [], [])):
            with mock.patch("os.read", side_effect=[data, b""]):
                cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        out = capsys.readouterr().out
        assert "yo" in out

    def test_process_exits_while_not_ready(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When select returns no ready fds but process has exited, drain remaining stdout."""
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "final"})
        data = f"{event}\n".encode()

        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99
        mock_stdout.read.return_value = b""
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = io.BytesIO(b"")
        # First poll: not ready + process still running, second: not ready + process exited
        mock_proc.poll.side_effect = [None, 0]

        with mock.patch("select.select", return_value=([], [], [])):
            with mock.patch("os.read", side_effect=[data, b""]):
                cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        out = capsys.readouterr().out
        assert "final" in out


# =============================================================================
# _drain_remaining_stdout
# =============================================================================

class TestDrainRemainingStdout:
    """Tests for _drain_remaining_stdout() post-exit data collection."""

    def test_drains_data(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "leftover"})
        data = f"{event}\n".encode()

        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99

        with mock.patch("os.read", side_effect=[data, b""]):
            result = cli._drain_remaining_stdout(
                mock_stdout, bytearray(), time.time(), verbose=False,
            )
        assert result == bytearray()
        out = capsys.readouterr().out
        assert "leftover" in out

    def test_drains_multiple_chunks(self, capsys: pytest.CaptureFixture[str]) -> None:
        e1 = json.dumps({"type": "system", "message": "chunk1"})
        e2 = json.dumps({"type": "system", "message": "chunk2"})

        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99

        with mock.patch("os.read", side_effect=[f"{e1}\n".encode(), f"{e2}\n".encode(), b""]):
            cli._drain_remaining_stdout(
                mock_stdout, bytearray(), time.time(), verbose=False,
            )
        out = capsys.readouterr().out
        assert "chunk1" in out
        assert "chunk2" in out

    def test_empty_stdout(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99

        with mock.patch("os.read", return_value=b""):
            result = cli._drain_remaining_stdout(
                mock_stdout, bytearray(), time.time(), verbose=False,
            )
        assert result == bytearray()


# =============================================================================
# _display_pre_run_warning
# =============================================================================

class TestDisplayPreRunWarning:
    """Tests for _display_pre_run_warning() permission warnings."""

    def test_skip_permissions_true(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("time.sleep"):
            cli._display_pre_run_warning(skip_permissions=True)
        out = capsys.readouterr().out
        assert "dangerously-skip-permissions is ENABLED" in out
        assert f"Starting in {cli._PRE_RUN_WARNING_DELAY} seconds" in out

    def test_skip_permissions_false(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("time.sleep"):
            cli._display_pre_run_warning(skip_permissions=False)
        out = capsys.readouterr().out
        assert "Running without --dangerously-skip-permissions" in out
        assert "Re-run with" in out
        assert "Continuing anyway" in out

    def test_keyboard_interrupt_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("time.sleep", side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                cli._display_pre_run_warning(skip_permissions=True)
            assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Aborted" in out


# =============================================================================
# _fatal
# =============================================================================

class TestFatal:
    """Tests for the _fatal() error-and-exit helper."""

    def test_prints_and_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            cli._fatal("something went wrong")
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "something went wrong" in out


# =============================================================================
# _read_stdout_chunk
# =============================================================================

class TestReadStdoutChunk:
    """Tests for _read_stdout_chunk() read strategy selection."""

    def test_uses_read1_when_available(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.read1.return_value = b"data"
        result = cli._read_stdout_chunk(mock_stdout)
        assert result == b"data"
        mock_stdout.read1.assert_called_once_with(cli._READ_CHUNK_SIZE)

    def test_falls_back_to_os_read(self) -> None:
        mock_stdout = mock.MagicMock(spec=["fileno"])
        mock_stdout.fileno.return_value = 42
        with mock.patch("os.read", return_value=b"fallback"):
            result = cli._read_stdout_chunk(mock_stdout)
        assert result == b"fallback"


# =============================================================================
# main with non-dry-run (covers _display_pre_run_warning call)
# =============================================================================

class TestMainNonDryRun:
    """Tests for main() in non-dry-run mode (with mocked internals)."""

    def test_non_dry_run_calls_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--pause", "0"]):
            with mock.patch.object(cli, "_display_pre_run_warning") as mock_warn:
                with mock.patch.object(cli, "_run_review_suite"):
                    cli.main()
                mock_warn.assert_called_once()

    def test_level_thorough(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--level", "thorough", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "security" in out
        assert "All done" in out


# =============================================================================
# Git helpers (convergence)
# =============================================================================

class TestParseShortstat:
    """Tests for _parse_shortstat() diff output parsing."""

    def test_insertions_and_deletions(self) -> None:
        text = " 3 files changed, 20 insertions(+), 10 deletions(-)"
        assert cli._parse_shortstat(text) == 30

    def test_insertions_only(self) -> None:
        text = " 1 file changed, 5 insertions(+)"
        assert cli._parse_shortstat(text) == 5

    def test_deletions_only(self) -> None:
        text = " 2 files changed, 8 deletions(-)"
        assert cli._parse_shortstat(text) == 8

    def test_empty_string(self) -> None:
        assert cli._parse_shortstat("") == 0

    def test_no_match(self) -> None:
        assert cli._parse_shortstat("nothing here") == 0

    def test_zero_insertions_and_deletions(self) -> None:
        assert cli._parse_shortstat(" 1 file changed, 0 insertions(+), 0 deletions(-)") == 0


class TestIsGitRepo:
    """Tests for _is_git_repo() detection."""

    def test_true_when_git_succeeds(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0)
            assert cli._is_git_repo("/tmp") is True

    def test_false_when_git_fails(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=128)
            assert cli._is_git_repo("/tmp") is False


class TestGitHeadSha:
    """Tests for _git_head_sha() SHA retrieval."""

    def test_returns_sha(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="abc123\n")
            assert cli._git_head_sha("/tmp") == "abc123"

    def test_returns_none_on_failure(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=128, stdout="")
            assert cli._git_head_sha("/tmp") is None


class TestGitCommitCycle:
    """Tests for _git_commit_cycle() post-cycle commit."""

    def test_commits_when_changes_exist(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            # git add succeeds, git diff --cached shows changes, git commit succeeds
            mock_run.side_effect = [
                mock.MagicMock(returncode=0),  # git add
                mock.MagicMock(returncode=1),  # git diff --cached --quiet (changes exist)
                mock.MagicMock(returncode=0),  # git commit
            ]
            assert cli._git_commit_cycle("/tmp", 1) is True

    def test_no_commit_when_clean(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=0),  # git add
                mock.MagicMock(returncode=0),  # git diff --cached --quiet (no changes)
            ]
            assert cli._git_commit_cycle("/tmp", 1) is False

    def test_returns_false_on_error(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert cli._git_commit_cycle("/tmp", 1) is False


class TestComputeChangeStats:
    """Tests for _compute_change_stats() convergence metric."""

    def test_calculates_lines_and_percentage(self) -> None:
        resolved = str(Path("/tmp").resolve())
        cli._total_lines_cache.pop(resolved, None)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=0, stdout=" 2 files changed, 10 insertions(+), 5 deletions(-)"),
                mock.MagicMock(returncode=0, stdout=b"file1.py\0file2.py\0"),
            ]
            file_content = b"line\n" * 1000
            mock_open = mock.mock_open(read_data=file_content)
            with mock.patch("builtins.open", mock_open):
                lines, pct = cli._compute_change_stats("/tmp", "abc123")
                assert lines == 15
                assert 0 < pct < 100
        cli._total_lines_cache.pop(resolved, None)

    def test_zero_when_no_changes(self) -> None:
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="")
            lines, pct = cli._compute_change_stats("/tmp", "abc123")
            assert lines == 0
            assert pct == 0.0

    def test_failed_git_diff_returns_zero(self) -> None:
        with mock.patch.object(cli, "_git_run") as mock_git:
            mock_git.return_value = mock.Mock(returncode=1, stderr="error")
            lines, pct = cli._compute_change_stats("/tmp", "abc123")
            assert lines == 0
            assert pct == 0.0

    def test_cache_hit_skips_line_count(self) -> None:
        """Branch: _total_lines_cache already has the key, so _count_tracked_lines is not called."""
        resolved = str(Path("/tmp").resolve())
        cli._total_lines_cache[resolved] = 500
        try:
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.MagicMock(
                    returncode=0,
                    stdout=" 1 file changed, 10 insertions(+)",
                )
                with mock.patch.object(cli, "_count_tracked_lines") as mock_count:
                    lines, pct = cli._compute_change_stats("/tmp", "abc123")
                    mock_count.assert_not_called()
                    assert lines == 10
                    assert pct == (10 / 500) * 100
        finally:
            cli._total_lines_cache.pop(resolved, None)


class TestConvergedAtPercentageArg:
    """Tests for --converged-at-percentage CLI argument."""

    def test_default(self) -> None:
        ns = _parse_cli(["--dir", "/tmp"])
        assert ns.converged_at_percentage == cli.DEFAULT_CONVERGENCE_THRESHOLD

    def test_custom_value(self) -> None:
        ns = _parse_cli(["--dir", "/tmp", "--converged-at-percentage", "0.5"])
        assert ns.converged_at_percentage == 0.5

    def test_zero_disables(self) -> None:
        ns = _parse_cli(["--dir", "/tmp", "--converged-at-percentage", "0"])
        assert ns.converged_at_percentage == 0.0


class TestConvergenceInSuite:
    """Tests for convergence detection within _run_review_suite."""

    def test_stops_early_when_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["sha1", "sha2", "sha2", "sha3"]), \
             mock.patch.object(cli, "_git_commit_cycle", return_value=True), \
             mock.patch.object(cli, "_compute_change_stats", return_value=(1, 0.05)):
            cli._run_review_suite(passes, 3, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Converged" in out

    def test_continues_when_not_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Branch: convergence check returns False, loop continues to next cycle."""
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args(dry_run=False)
        with _patch_suite_git(["sha1"] * 10), \
             mock.patch.object(cli, "_check_cycle_convergence", return_value=(False, 5.0)):
            cli._run_review_suite(passes, 2, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out

    def test_no_convergence_without_git(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = _make_suite_args()
        cli._run_review_suite(passes, 2, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        # Should run both cycles without convergence checks (dry run)
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out


class TestKillProcessGroup:
    """Tests for _kill_process_group() cleanup."""

    def test_kills_process_group(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        with mock.patch("os.getpgid", return_value=12345) as mock_getpgid:
            with mock.patch("os.killpg") as mock_killpg:
                cli._kill_process_group(mock_proc)
                mock_getpgid.assert_called_once_with(12345)
                mock_killpg.assert_called_with(12345, signal.SIGTERM)

    def test_sigkill_on_timeout(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 99
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)
        with mock.patch("os.getpgid", return_value=99):
            with mock.patch("os.killpg") as mock_killpg:
                cli._kill_process_group(mock_proc)
                # Should have been called with SIGTERM then SIGKILL
                calls = [c for c in mock_killpg.call_args_list]
                signals_sent = [c[0][1] for c in calls]
                assert signal.SIGTERM in signals_sent
                assert signal.SIGKILL in signals_sent

    def test_handles_already_dead_process(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 1
        with mock.patch("os.getpgid", side_effect=OSError("No such process")):
            # Should not raise
            cli._kill_process_group(mock_proc)


class TestMeasureCurrentRssMb:
    """Tests for _measure_current_rss_mb() memory measurement."""

    def test_returns_positive_value(self) -> None:
        rss = cli._measure_current_rss_mb()
        assert rss > 0

    def test_fallback_on_ps_failure(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            rss = cli._measure_current_rss_mb()
            assert rss > 0  # falls back to resource.getrusage


class TestFindChildPids:
    """Tests for _find_child_pids()."""

    def test_returns_empty_when_no_children(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            assert cli._find_child_pids() == []

    def test_returns_pids(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="123\n456\n")):
            assert cli._find_child_pids() == [123, 456]


class TestKillOrphanedChildren:
    """Tests for _kill_orphaned_children()."""

    def test_returns_zero_when_none(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            assert cli._kill_orphaned_children() == 0

    def test_kills_found_children(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="999\n")):
            with mock.patch("os.kill") as mock_kill:
                killed = cli._kill_orphaned_children()
                assert killed == 1
                mock_kill.assert_called_once_with(999, signal.SIGKILL)

    def test_handles_already_dead_child(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="999\n")):
            with mock.patch("os.kill", side_effect=OSError("No such process")):
                killed = cli._kill_orphaned_children()
                assert killed == 0


class TestLogMemoryUsage:
    """Tests for _log_memory_usage() reporting."""

    def test_prints_memory_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(cli, "_measure_current_rss_mb", return_value=42.0):
            with mock.patch.object(cli, "_find_child_pids", return_value=[]):
                cli._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "42MB" in out
        assert "0 child" in out

    def test_kills_orphans_when_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(cli, "_measure_current_rss_mb", return_value=100.0):
            with mock.patch.object(cli, "_find_child_pids", return_value=[123, 456]):
                with mock.patch.object(cli, "_kill_orphaned_children", return_value=2):
                    cli._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "2 child" in out
        assert "Warning" in out
        assert "Killed 2" in out

    def test_orphans_found_but_none_killed(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Branch: child_pids is truthy but _kill_orphaned_children returns 0."""
        with mock.patch.object(cli, "_measure_current_rss_mb", return_value=50.0):
            with mock.patch.object(cli, "_find_child_pids", return_value=[999]):
                with mock.patch.object(cli, "_kill_orphaned_children", return_value=0):
                    cli._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "Killed" not in out


class TestSpawnUsesNewSession:
    """Verify _spawn_claude_process creates a new session for process group isolation."""

    def test_start_new_session(self) -> None:
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock.MagicMock()
            cli._spawn_claude_process(["claude"], "/tmp")
            call_kwargs = mock_popen.call_args.kwargs
            assert call_kwargs["start_new_session"] is True


class TestCommitMessageInstructions:
    """Tests that commit message instructions are appended to prompts."""

    def test_prompt_includes_commit_instructions(self, capsys: pytest.CaptureFixture[str]) -> None:
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = _make_suite_args()
        with mock.patch.object(cli, "run_claude", return_value=0) as mock_run:
            cli._run_review_suite(passes, 1, "/tmp", args)
            prompt_used = mock_run.call_args[0][0]
            assert "commit message rules" in prompt_used
            assert "Do not mention Claude" in prompt_used


# =============================================================================
# _measure_current_rss_mb — exception paths
# =============================================================================

class TestMeasureCurrentRssMbExceptions:
    """Tests for _measure_current_rss_mb() exception fallback paths."""

    def test_oserror_falls_back_to_resource(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            rss = cli._measure_current_rss_mb()
            assert rss > 0  # uses resource.getrusage fallback

    def test_valueerror_falls_back_to_resource(self) -> None:
        with mock.patch("subprocess.run", side_effect=ValueError("bad")):
            rss = cli._measure_current_rss_mb()
            assert rss > 0


# =============================================================================
# _find_child_pids — exception paths
# =============================================================================

class TestFindChildPidsExceptions:
    """Tests for _find_child_pids() exception and edge-case paths."""

    def test_oserror_returns_empty(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            assert cli._find_child_pids() == []

    def test_invalid_pid_lines_skipped(self) -> None:
        """Non-numeric lines in pgrep output are silently skipped."""
        with mock.patch("subprocess.run", return_value=mock.MagicMock(
            returncode=0, stdout="123\nnot_a_number\n456\n"
        )):
            assert cli._find_child_pids() == [123, 456]


# =============================================================================
# _count_tracked_lines — various paths
# =============================================================================

class TestCountTrackedLines:
    """Tests for _count_tracked_lines() line counting."""

    def test_git_ls_files_failure_returns_1(self) -> None:
        with mock.patch.object(cli, "_git_run", return_value=mock.MagicMock(returncode=1)):
            assert cli._count_tracked_lines("/tmp") == 1

    def test_skips_binary_files(self, tmp_path: Path) -> None:
        # Create a binary file (contains null bytes)
        binary_file = tmp_path / "binary.dat"
        binary_file.write_bytes(b"\x00\x01\x02\x03")
        # Create a text file with 3 lines
        text_file = tmp_path / "hello.txt"
        text_file.write_text("line1\nline2\nline3\n")

        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"binary.dat\x00hello.txt\x00",
        )
        with mock.patch.object(cli, "_git_run", return_value=ls_result):
            count = cli._count_tracked_lines(str(tmp_path))
            assert count == 3  # only text file lines

    def test_large_file_multi_chunk(self, tmp_path: Path) -> None:
        """Files larger than the initial header read are processed in chunks."""
        large_file = tmp_path / "big.txt"
        # Create a file with content larger than _READ_CHUNK_SIZE
        line = "x" * 100 + "\n"  # 101 bytes per line
        num_lines = 200  # ~20200 bytes total, well beyond 8192
        large_file.write_text(line * num_lines)

        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"big.txt\x00",
        )
        with mock.patch.object(cli, "_git_run", return_value=ls_result):
            count = cli._count_tracked_lines(str(tmp_path))
            assert count == num_lines

    def test_empty_repo_returns_minimum_one(self) -> None:
        """Even an empty repo should return at least 1 to avoid division by zero."""
        with mock.patch.object(cli, "_git_run") as mock_git:
            mock_git.return_value = mock.Mock(returncode=0, stdout=b"")
            result = cli._count_tracked_lines("/tmp")
            assert result == 1

    def test_oserror_on_file_open(self, tmp_path: Path) -> None:
        """OSError when opening a file is silently skipped."""
        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"nonexistent.txt\x00",
        )
        with mock.patch.object(cli, "_git_run", return_value=ls_result):
            count = cli._count_tracked_lines(str(tmp_path))
            assert count == 1  # max(0, 1)


# =============================================================================
# _read_stdout_chunk — OSError path
# =============================================================================

class TestReadStdoutChunkOSError:
    """Tests for _read_stdout_chunk() OSError handling."""

    def test_oserror_returns_empty_bytes(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.read1 = mock.MagicMock(side_effect=OSError("broken pipe"))
        result = cli._read_stdout_chunk(mock_stdout)
        assert result == b""


# =============================================================================
# _drain_remaining_stdout — OSError path
# =============================================================================

class TestDrainRemainingStdoutOSError:
    """Tests for _drain_remaining_stdout() OSError handling."""

    def test_oserror_returns_buffer_unchanged(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.side_effect = OSError("bad fd")
        buf = bytearray(b"existing")
        result = cli._drain_remaining_stdout(mock_stdout, buf, time.time(), False)
        assert result == bytearray(b"existing")


# =============================================================================
# _stream_process_output — select() failure and stdout close failure
# =============================================================================

class TestStreamProcessOutputExceptions:
    """Tests for exception paths in _stream_process_output()."""

    def test_select_oserror_breaks_loop(self) -> None:
        mock_proc = mock.MagicMock()
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 5
        mock_stdout.read1 = None
        mock_proc.stdout = mock_stdout
        mock_proc.pid = 9999
        mock_proc.poll.return_value = None

        with mock.patch("select.select", side_effect=OSError("fd closed")):
            with mock.patch("os.read", return_value=b""):
                cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        # Should not raise — just breaks out of loop

    def test_stdout_close_oserror_handled(self) -> None:
        mock_proc = mock.MagicMock()
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 5
        mock_stdout.read1 = None
        mock_proc.stdout = mock_stdout
        mock_proc.pid = 9999
        mock_proc.poll.return_value = 0  # process already exited

        with mock.patch("select.select", return_value=([], [], [])):
            with mock.patch("os.read", return_value=b""):
                mock_stdout.close.side_effect = OSError("close failed")
                cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        # Should not raise


# =============================================================================
# _kill_process_group — SIGKILL OSError path
# =============================================================================

class TestKillProcessGroupSigkillOSError:
    """Tests for _kill_process_group() SIGKILL OSError handling."""

    def test_sigkill_oserror_handled(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)

        with mock.patch("os.getpgid", return_value=12345):
            with mock.patch("os.killpg") as mock_killpg:
                # SIGTERM succeeds, but SIGKILL raises OSError
                def killpg_side_effect(pgid: int, sig: signal.Signals) -> None:
                    if sig == signal.SIGKILL:
                        raise OSError("no such process")
                mock_killpg.side_effect = killpg_side_effect
                cli._kill_process_group(mock_proc)
        # Should not raise


# =============================================================================
# run_claude — process.wait() timeout path
# =============================================================================

class TestRunClaudeWaitTimeout:
    """Tests for run_claude() when process.wait() times out."""

    def test_wait_timeout_triggers_kill(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 120)
        mock_proc.returncode = -9

        with mock.patch.object(cli, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(cli, "_stream_process_output", return_value=time.time()):
                with mock.patch.object(cli, "_kill_process_group") as mock_kill:
                    cli.run_claude("test prompt", "/tmp", idle_timeout=1)
                    # Called once for timeout and once for safety net
                    assert mock_kill.call_count == 2


# =============================================================================
# _check_cycle_convergence — all three return paths
# =============================================================================

class TestCheckCycleConvergence:
    """Tests for _check_cycle_convergence() convergence detection."""

    def test_no_changes_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When SHA hasn't changed, report convergence."""
        with mock.patch.object(cli, "_git_commit_cycle"):
            with mock.patch.object(cli, "_git_head_sha", return_value="abc123"):
                should_stop, pct = cli._check_cycle_convergence(
                    "/tmp", cycle=1, base_sha="abc123",
                    convergence_threshold=0.1, prev_change_pct=None,
                )
        assert should_stop is True
        assert pct is None
        assert "converged" in capsys.readouterr().out.lower()

    def test_oscillation_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When changes increase, print oscillation warning."""
        with mock.patch.object(cli, "_git_commit_cycle"), \
             mock.patch.object(cli, "_git_head_sha", return_value="def456"), \
             mock.patch.object(cli, "_compute_change_stats", return_value=(50, 5.0)):
            should_stop, pct = cli._check_cycle_convergence(
                "/tmp", cycle=2, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=2.0,
            )
        assert should_stop is False
        assert pct == 5.0
        assert "oscillation" in capsys.readouterr().out.lower()

    def test_not_converged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """When changes are above threshold, return False."""
        with mock.patch.object(cli, "_git_commit_cycle"), \
             mock.patch.object(cli, "_git_head_sha", return_value="def456"), \
             mock.patch.object(cli, "_compute_change_stats", return_value=(15, 1.5)):
            should_stop, pct = cli._check_cycle_convergence(
                "/tmp", cycle=1, base_sha="abc123",
                convergence_threshold=0.1, prev_change_pct=None,
            )
        assert should_stop is False
        assert pct == 1.5


# =============================================================================
# _resolve_working_directory — OSError path
# =============================================================================

class TestResolveWorkingDirectoryOSError:
    """Tests for _resolve_working_directory() error handling."""

    def test_oserror_calls_fatal(self) -> None:
        with mock.patch.object(Path, "resolve", side_effect=OSError("bad path")):
            with pytest.raises(SystemExit):
                cli._resolve_working_directory("/nonexistent/\x00path")


# =============================================================================
# main() — KeyboardInterrupt path
# =============================================================================

class TestMainKeyboardInterrupt:
    """Tests for main() KeyboardInterrupt handling."""

    def test_keyboard_interrupt_exits_130(self, capsys: pytest.CaptureFixture[str]) -> None:
        with _patch_main_pipeline(suite_side_effect=KeyboardInterrupt):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 130
        assert "Interrupted" in capsys.readouterr().out


# =============================================================================
# __main__ guard
# =============================================================================

class TestMainGuard:
    """Test the if __name__ == '__main__' block."""

    def test_main_guard_calls_main(self) -> None:
        """Verify the __main__ guard invokes main() when __name__ == '__main__'."""
        with mock.patch.dict("sys.modules", {"claudeloop.cli": cli}):
            with mock.patch.object(cli, "main") as mock_main:
                # Simulate running "python -m claudeloop.cli"
                exec(
                    compile('if __name__ == "__main__": main()', cli.__file__, "exec"),
                    {"__name__": "__main__", "main": cli.main},
                )
                mock_main.assert_called_once()


# =============================================================================
# Edge cases and boundary conditions
# =============================================================================

class TestValidateArguments:
    """Tests for _validate_arguments()."""

    def test_cycles_zero_exits(self) -> None:
        args = argparse.Namespace(idle_timeout=120, pause=0, cycles=0, converged_at_percentage=0.1)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_negative_cycles_exits(self) -> None:
        args = argparse.Namespace(idle_timeout=120, pause=0, cycles=-5, converged_at_percentage=0.1)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_negative_convergence_exits(self) -> None:
        args = argparse.Namespace(idle_timeout=120, pause=0, cycles=1, converged_at_percentage=-0.5)
        with pytest.raises(SystemExit) as exc_info:
            cli._validate_arguments(args)
        assert exc_info.value.code == 1

    def test_valid_arguments_no_exit(self) -> None:
        args = argparse.Namespace(idle_timeout=1, pause=0, cycles=1, converged_at_percentage=0.0)
        cli._validate_arguments(args)  # should not raise


class TestResolveSelectedPasses:
    """Tests for _resolve_selected_passes()."""

    def test_level_basic(self) -> None:
        args = argparse.Namespace(all_passes=False, passes=None, level="basic")
        result = cli._resolve_selected_passes(args)
        ids = [p["id"] for p in result]
        assert ids == cli.TIER_BASIC

    def test_all_passes(self) -> None:
        args = argparse.Namespace(all_passes=True, passes=None, level=None)
        result = cli._resolve_selected_passes(args)
        assert len(result) == len(cli.REVIEW_PASSES)

    def test_passes_override_level(self) -> None:
        args = argparse.Namespace(all_passes=False, passes=["security"], level="exhaustive")
        result = cli._resolve_selected_passes(args)
        assert len(result) == 1
        assert result[0]["id"] == "security"


class TestMainEmptyPassesExit:
    """Test that main() exits if no passes are resolved."""

    def test_empty_passes_exits(self) -> None:
        mock_args = _make_main_mock_args(dry_run=True, passes=None, level=None)
        with mock.patch.object(cli, "_build_argument_parser") as mock_parser:
            mock_parser.return_value.parse_args.return_value = mock_args
            with mock.patch.object(cli, "_resolve_working_directory", return_value="/tmp"):
                with mock.patch.object(cli, "_validate_arguments"):
                    with mock.patch.object(cli, "_resolve_selected_passes", return_value=[]):
                        with pytest.raises(SystemExit) as exc_info:
                            cli.main()
                        assert exc_info.value.code == 1


class TestMainNegativeConvergenceExit:
    """Test that main() exits with negative convergence percentage."""

    def test_negative_converged_at_percentage_exits(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--converged-at-percentage", "-1"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1


class TestGitRun:
    """Tests for _git_run()."""

    def test_empty_args(self) -> None:
        """_git_run with no args should just run 'git'."""
        result = cli._git_run("/tmp")
        assert result.returncode != 0

    def test_git_not_found(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(FileNotFoundError):
                cli._git_run("/tmp", "status")

    def test_oserror_reraised(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("permission denied")):
            with pytest.raises(OSError):
                cli._git_run("/tmp", "status")


class TestCountTrackedLinesPathTraversal:
    """Test that path traversal is blocked in _count_tracked_lines."""

    def test_symlink_outside_workdir_skipped(self, tmp_path: Path) -> None:
        """Files that resolve outside the workdir are skipped."""
        real_file = tmp_path / "real.txt"
        real_file.write_text("line1\nline2\n")

        ls_result = mock.MagicMock(
            returncode=0,
            stdout=b"real.txt\x00../../../etc/passwd\x00",
        )
        with mock.patch.object(cli, "_git_run", return_value=ls_result):
            count = cli._count_tracked_lines(str(tmp_path))
            assert count == 2  # only real.txt lines


class TestRunClaudeReturnCodeNone:
    """Test run_claude when process.returncode is None."""

    def test_returncode_none_returns_negative_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        mock_proc.returncode = None
        mock_proc.wait.return_value = None

        with mock.patch.object(cli, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(cli, "_stream_process_output", return_value=time.time()):
                with mock.patch.object(cli, "_kill_process_group"):
                    with mock.patch.object(cli, "_log_memory_usage"):
                        code = cli.run_claude("test", "/tmp")
        assert code == -1
        out = capsys.readouterr().out
        assert "exited with code -1" in out


class TestMainFileNotFoundError:
    """Test main() FileNotFoundError handling."""

    def test_file_not_found_during_suite(self) -> None:
        with _patch_main_pipeline(suite_side_effect=FileNotFoundError("claude")):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1


class TestMainUnexpectedException:
    """Test main() generic exception handling."""

    def test_unexpected_error_reraises(self) -> None:
        with _patch_main_pipeline(suite_side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                cli.main()


class TestMainVerboseLogging:
    """Test main() with --verbose flag sets INFO log level."""

    def test_verbose_sets_info_level(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--verbose", "--dry-run", "--pause", "0"]):
            with mock.patch("logging.basicConfig") as mock_log:
                with mock.patch.object(cli, "_run_review_suite"):
                    cli.main()
                mock_log.assert_called_once()
                assert mock_log.call_args.kwargs["level"] == logging.INFO

    def test_debug_sets_debug_level(self) -> None:
        with mock.patch("sys.argv", ["claudeloop", "--dir", ".", "--debug", "--dry-run", "--pause", "0"]):
            with mock.patch("logging.basicConfig") as mock_log:
                with mock.patch.object(cli, "_run_review_suite"):
                    cli.main()
                mock_log.assert_called_once()
                assert mock_log.call_args.kwargs["level"] == logging.DEBUG


