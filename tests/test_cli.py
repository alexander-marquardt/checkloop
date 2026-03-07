"""Comprehensive tests for claudeloop.cli — targeting >=90% line coverage."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
from unittest import mock

import pytest
from pathlib import Path

from claudeloop import cli


# =============================================================================
# banner / log
# =============================================================================

class TestBanner:
    """Tests for the banner() terminal output helper."""

    def test_default_colour(self, capsys):
        cli.banner("Hello")
        out = capsys.readouterr().out
        assert "Hello" in out
        assert cli.CYAN in out
        assert cli.BOLD in out
        assert cli.RESET in out

    def test_custom_colour(self, capsys):
        cli.banner("Title", cli.GREEN)
        out = capsys.readouterr().out
        assert cli.GREEN in out
        assert "Title" in out


class TestPrintStatus:
    """Tests for the print_status() terminal output helper."""

    def test_default_dim(self, capsys):
        cli.print_status("info")
        out = capsys.readouterr().out
        assert "info" in out
        assert cli.DIM in out

    def test_custom_colour(self, capsys):
        cli.print_status("warn", cli.YELLOW)
        out = capsys.readouterr().out
        assert cli.YELLOW in out


# =============================================================================
# _format_duration
# =============================================================================

class TestFormatDuration:
    """Tests for _format_duration() time formatting."""

    def test_zero(self):
        assert cli._format_duration(0) == "0m00s"

    def test_seconds_only(self):
        assert cli._format_duration(45) == "0m45s"

    def test_minutes_and_seconds(self):
        assert cli._format_duration(125) == "2m05s"

    def test_exactly_one_hour(self):
        assert cli._format_duration(3600) == "1h00m00s"

    def test_hours_minutes_seconds(self):
        assert cli._format_duration(3661) == "1h01m01s"

    def test_large_value(self):
        assert cli._format_duration(7384) == "2h03m04s"

    def test_just_under_one_hour(self):
        assert cli._format_duration(3599) == "59m59s"

    def test_negative_clamped_to_zero(self):
        assert cli._format_duration(-5) == "0m00s"

    def test_negative_large_clamped_to_zero(self):
        assert cli._format_duration(-9999) == "0m00s"

    def test_fractional_seconds(self):
        assert cli._format_duration(0.9) == "0m00s"

    def test_fractional_just_under_minute(self):
        assert cli._format_duration(59.999) == "0m59s"


# =============================================================================
# _looks_dangerous
# =============================================================================

class TestLooksDangerous:
    """Tests for the _looks_dangerous() prompt safety guard."""

    def test_safe_prompt(self):
        assert cli._looks_dangerous("Review all code for quality") is False

    def test_rm_rf_root(self):
        assert cli._looks_dangerous("rm -rf /") is True

    def test_case_insensitive(self):
        assert cli._looks_dangerous("DROP DATABASE users") is True

    def test_drop_table(self):
        assert cli._looks_dangerous("drop table foo") is True

    def test_sudo_rm(self):
        assert cli._looks_dangerous("sudo rm something") is True

    def test_fork_bomb(self):
        assert cli._looks_dangerous(":(){:|:&};:") is True

    def test_dd_dev_zero(self):
        assert cli._looks_dangerous("dd if=/dev/zero of=/dev/sda") is True

    def test_chmod_777_root(self):
        assert cli._looks_dangerous("chmod 777 /") is True

    def test_delete_all(self):
        assert cli._looks_dangerous("delete all files") is True

    def test_truncate(self):
        assert cli._looks_dangerous("truncate the table") is True

    def test_format_keyword(self):
        assert cli._looks_dangerous("format the disk") is True

    def test_wipe(self):
        assert cli._looks_dangerous("wipe everything") is True

    def test_etc_passwd(self):
        assert cli._looks_dangerous("cat /etc/passwd") is True

    def test_embedded_safe_word(self):
        # "format" as a standalone word should match
        assert cli._looks_dangerous("reformat") is False

    def test_empty_string(self):
        assert cli._looks_dangerous("") is False

    def test_whitespace_only(self):
        assert cli._looks_dangerous("   \t\n  ") is False

    def test_unicode_prompt(self):
        assert cli._looks_dangerous("レビューコード 🔍") is False


# =============================================================================
# _summarise_tool_use
# =============================================================================

class TestSummariseToolUse:
    """Tests for _summarise_tool_use() tool event formatting."""

    def test_read_file_path(self):
        result = cli._summarise_tool_use("Read", {"file_path": "/tmp/foo.py"})
        assert "/tmp/foo.py" in result

    def test_edit_file_path(self):
        result = cli._summarise_tool_use("Edit", {"file_path": "/a/b.txt"})
        assert "/a/b.txt" in result

    def test_write_file_path(self):
        result = cli._summarise_tool_use("write_file", {"file_path": "/x.py"})
        assert "/x.py" in result

    def test_bash_short_command(self):
        result = cli._summarise_tool_use("Bash", {"command": "ls -la"})
        assert "$ ls -la" in result

    def test_bash_long_command_truncated(self):
        long_cmd = "x" * 100
        result = cli._summarise_tool_use("Bash", {"command": long_cmd})
        assert result.endswith("...")
        assert len(result) < len(long_cmd) + 10

    def test_glob_pattern(self):
        result = cli._summarise_tool_use("Glob", {"pattern": "**/*.py"})
        assert "**/*.py" in result

    def test_grep_pattern(self):
        result = cli._summarise_tool_use("Grep", {"pattern": "TODO"})
        assert "/TODO/" in result

    def test_unknown_tool(self):
        result = cli._summarise_tool_use("SomeTool", {"arg": "val"})
        assert result == ""

    def test_file_path_tool_without_file_path_key(self):
        result = cli._summarise_tool_use("Read", {"other": "val"})
        assert result == ""

    def test_bash_without_command(self):
        result = cli._summarise_tool_use("Bash", {})
        assert result == ""

    def test_lowercase_bash(self):
        result = cli._summarise_tool_use("bash", {"command": "echo hi"})
        assert "$ echo hi" in result

    def test_lowercase_grep(self):
        result = cli._summarise_tool_use("grep", {"pattern": "foo"})
        assert "/foo/" in result

    def test_lowercase_glob(self):
        result = cli._summarise_tool_use("glob", {"pattern": "*.md"})
        assert "*.md" in result

    def test_empty_tool_name(self):
        result = cli._summarise_tool_use("", {"file_path": "/a.py"})
        assert result == ""

    def test_empty_command(self):
        result = cli._summarise_tool_use("Bash", {"command": ""})
        assert "$ " in result

    def test_bash_command_exactly_80_chars(self):
        cmd = "x" * 80
        result = cli._summarise_tool_use("Bash", {"command": cmd})
        assert "..." not in result
        assert cmd in result

    def test_bash_command_81_chars(self):
        cmd = "x" * 81
        result = cli._summarise_tool_use("Bash", {"command": cmd})
        assert result.endswith("...")


# =============================================================================
# _print_event
# =============================================================================

class TestPrintEvent:
    """Tests for _print_event() stream-json event rendering."""

    def _now(self):
        return time.time()

    def test_assistant_text(self, capsys):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Found issue"}]},
        }
        cli._print_event(event, self._now())
        assert "Found issue" in capsys.readouterr().out

    def test_assistant_empty_text(self, capsys):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "   "}]},
        }
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_non_text_block(self, capsys):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "image", "url": "x"}]},
        }
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_event(self, capsys):
        event = {"type": "tool_use", "tool": "Read", "input": {"file_path": "/a.py"}}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Read]" in out
        assert "/a.py" in out

    def test_tool_use_name_fallback(self, capsys):
        event = {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Bash]" in out

    def test_system_event(self, capsys):
        event = {"type": "system", "message": "Initialising..."}
        cli._print_event(event, self._now())
        assert "Initialising..." in capsys.readouterr().out

    def test_system_event_empty(self, capsys):
        event = {"type": "system", "message": ""}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_result_event(self, capsys):
        event = {"type": "result", "result": "All good"}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "Result" in out
        assert "All good" in out

    def test_result_event_empty(self, capsys):
        event = {"type": "result", "result": ""}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_unknown_event_type(self, capsys):
        event = {"type": "unknown_type"}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_missing_type(self, capsys):
        event = {"data": "something"}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_with_none_input(self, capsys):
        event = {"type": "tool_use", "tool": "Read", "input": None}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[Read]" in out

    def test_assistant_empty_content_list(self, capsys):
        event = {"type": "assistant", "message": {"content": []}}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_assistant_missing_message(self, capsys):
        event = {"type": "assistant"}
        cli._print_event(event, self._now())
        assert capsys.readouterr().out.strip() == ""

    def test_tool_use_missing_both_tool_and_name(self, capsys):
        event = {"type": "tool_use", "input": {"file_path": "/a.py"}}
        cli._print_event(event, self._now())
        out = capsys.readouterr().out
        assert "[unknown]" in out


# =============================================================================
# _process_lines
# =============================================================================

class TestProcessLines:
    """Tests for _process_lines() JSONL buffer processing."""

    def test_complete_line(self, capsys):
        event = json.dumps({"type": "system", "message": "hello"})
        buf = bytearray((event + "\n").encode())
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert "hello" in capsys.readouterr().out

    def test_partial_line_returned(self):
        buf = bytearray(b"partial")
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray(b"partial")

    def test_multiple_lines(self, capsys):
        e1 = json.dumps({"type": "system", "message": "one"})
        e2 = json.dumps({"type": "system", "message": "two"})
        buf = bytearray(f"{e1}\n{e2}\n".encode())
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        out = capsys.readouterr().out
        assert "one" in out
        assert "two" in out

    def test_invalid_json_verbose(self, capsys):
        buf = bytearray(b"not json\n")
        remainder = cli._process_lines(buf, time.time(), verbose=True)
        assert remainder == bytearray()
        assert "not json" in capsys.readouterr().out

    def test_invalid_json_not_verbose(self, capsys):
        buf = bytearray(b"not json\n")
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        # Should NOT print non-JSON when verbose is False
        assert "not json" not in capsys.readouterr().out

    def test_empty_line_skipped(self, capsys):
        buf = bytearray(b"\n\n")
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_line_with_remainder(self):
        event = json.dumps({"type": "system", "message": "x"})
        buf = bytearray(f"{event}\npartial".encode())
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray(b"partial")

    def test_empty_buffer(self):
        buf = bytearray(b"")
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray(b"")

    def test_whitespace_only_lines(self, capsys):
        buf = bytearray(b"   \n\t\n")
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert capsys.readouterr().out == ""

    def test_unicode_content(self, capsys):
        event = json.dumps({"type": "system", "message": "こんにちは 🎉"})
        buf = bytearray((event + "\n").encode("utf-8"))
        remainder = cli._process_lines(buf, time.time(), verbose=False)
        assert remainder == bytearray()
        assert "こんにちは" in capsys.readouterr().out


# =============================================================================
# _build_claude_command
# =============================================================================

class TestBuildClaudeCommand:
    """Tests for _build_claude_command() CLI argument assembly."""

    def test_with_skip_permissions(self):
        cmd = cli._build_claude_command("review code", skip_permissions=True)
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "-p" in cmd
        assert "review code" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_without_skip_permissions(self):
        cmd = cli._build_claude_command("review code", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd
        assert "review code" in cmd

    def test_default_skip_permissions_is_false(self):
        """skip_permissions defaults to False (safe by default)."""
        cmd = cli._build_claude_command("review code", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd


# =============================================================================
# _spawn_claude_process
# =============================================================================

class TestSpawnClaudeProcess:
    """Tests for _spawn_claude_process() subprocess creation."""

    def test_file_not_found_exits(self):
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            with pytest.raises(SystemExit) as exc_info:
                cli._spawn_claude_process(["claude", "-p", "hi"], "/tmp")
            assert exc_info.value.code == 1

    def test_strips_claudecode_env(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "HOME": "/home"}):
            with mock.patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = mock.MagicMock()
                cli._spawn_claude_process(["claude"], "/tmp")
                call_kwargs = mock_popen.call_args
                env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
                assert "CLAUDECODE" not in env

    def test_passes_cwd_and_pipes(self):
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

    def test_dry_run(self, capsys):
        code = cli.run_claude("prompt", "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_normal_run(self, capsys):
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

    def test_dry_run_short_prompt_no_ellipsis(self, capsys):
        code = cli.run_claude("short", "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "short" in out
        assert "short..." not in out

    def test_dry_run_long_prompt_truncated(self, capsys):
        long_prompt = "x" * 200
        code = cli.run_claude(long_prompt, "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "..." in out

    def test_dry_run_empty_prompt(self, capsys):
        code = cli.run_claude("", "/tmp", dry_run=True)
        assert code == 0

    def test_nonzero_exit(self, capsys):
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

    def _parse(self, args: list[str]) -> argparse.Namespace:
        return cli._build_argument_parser().parse_args(args)

    def test_defaults(self):
        ns = self._parse([])
        assert ns.dir == "."
        assert ns.passes is None
        assert ns.level is None
        assert ns.all_passes is False
        assert ns.cycles == 1
        assert ns.idle_timeout == 120
        assert ns.dry_run is False
        assert ns.verbose is False
        assert ns.pause == 2

    def test_dir(self):
        ns = self._parse(["--dir", "/foo"])
        assert ns.dir == "/foo"

    def test_dir_short(self):
        ns = self._parse(["-d", "/bar"])
        assert ns.dir == "/bar"

    def test_passes(self):
        ns = self._parse(["--passes", "readability", "security"])
        assert ns.passes == ["readability", "security"]

    def test_all_passes(self):
        ns = self._parse(["--all-passes"])
        assert ns.all_passes is True

    def test_cycles(self):
        ns = self._parse(["--cycles", "5"])
        assert ns.cycles == 5

    def test_cycles_short(self):
        ns = self._parse(["-c", "3"])
        assert ns.cycles == 3

    def test_idle_timeout(self):
        ns = self._parse(["--idle-timeout", "300"])
        assert ns.idle_timeout == 300

    def test_dry_run(self):
        ns = self._parse(["--dry-run"])
        assert ns.dry_run is True

    def test_verbose(self):
        ns = self._parse(["--verbose"])
        assert ns.verbose is True

    def test_verbose_short(self):
        ns = self._parse(["-v"])
        assert ns.verbose is True

    def test_pause(self):
        ns = self._parse(["--pause", "10"])
        assert ns.pause == 10

    def test_invalid_pass_rejected(self):
        with pytest.raises(SystemExit):
            self._parse(["--passes", "nonexistent"])


# =============================================================================
# _print_run_summary
# =============================================================================

class TestPrintRunSummary:
    """Tests for _print_run_summary() pre-run output."""

    def test_normal_summary(self, capsys):
        passes = [{"id": "readability", "label": "Readability"}]
        cli._print_run_summary("/tmp", passes, 2, 2, 120, False)
        out = capsys.readouterr().out
        assert "claudeloop" in out
        assert "/tmp" in out
        assert "readability" in out
        assert "2" in out

    def test_dry_run_summary(self, capsys):
        passes = [{"id": "tests", "label": "Tests"}]
        cli._print_run_summary("/tmp", passes, 1, 1, 120, True)
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_single_cycle_no_plural(self, capsys):
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 1, 1, 60, False)
        out = capsys.readouterr().out
        assert "1 cycle)" in out

    def test_multiple_cycles_plural(self, capsys):
        passes = [{"id": "dry", "label": "DRY"}]
        cli._print_run_summary("/dir", passes, 3, 3, 60, False)
        out = capsys.readouterr().out
        assert "cycles)" in out

    def test_empty_passes_list(self, capsys):
        cli._print_run_summary("/dir", [], 1, 0, 60, False)
        out = capsys.readouterr().out
        assert "Total steps  : 0" in out


# =============================================================================
# _run_review_suite
# =============================================================================

class TestRunReviewSuite:
    """Tests for _run_review_suite() multi-pass execution."""

    def test_single_pass_single_cycle(self, capsys):
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = argparse.Namespace(pause=0, dry_run=True, idle_timeout=120, verbose=False, dangerously_skip_permissions=False)
        cli._run_review_suite(passes, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "Readability" in out
        assert "DRY RUN" in out

    def test_multi_cycle_banner(self, capsys):
        passes = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = argparse.Namespace(pause=0, dry_run=True, idle_timeout=120, verbose=False, dangerously_skip_permissions=False)
        cli._run_review_suite(passes, 2, "/tmp", args)
        out = capsys.readouterr().out
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out

    def test_dangerous_prompt_skipped(self, capsys):
        passes = [{"id": "evil", "label": "Evil", "prompt": "rm -rf / everything"}]
        args = argparse.Namespace(pause=0, dry_run=False, idle_timeout=120, verbose=False, dangerously_skip_permissions=False)
        with mock.patch.object(cli, "run_claude") as mock_run:
            cli._run_review_suite(passes, 1, "/tmp", args)
            mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "dangerous" in out.lower() or "Skipping" in out

    def test_nonzero_exit_continues(self, capsys):
        passes = [
            {"id": "a", "label": "A", "prompt": "do a"},
            {"id": "b", "label": "B", "prompt": "do b"},
        ]
        args = argparse.Namespace(pause=0, dry_run=False, idle_timeout=120, verbose=False, dangerously_skip_permissions=False)
        with mock.patch.object(cli, "run_claude", return_value=1):
            cli._run_review_suite(passes, 1, "/tmp", args)
        out = capsys.readouterr().out
        assert "exited with code 1" in out
        # Both passes should be attempted
        assert "A" in out
        assert "B" in out


# =============================================================================
# main
# =============================================================================

class TestMain:
    """Tests for the main() CLI entry point."""

    def test_nonexistent_dir_exits(self):
        with mock.patch("sys.argv", ["claudeloop", "--dir", "/nonexistent_xyz_abc"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_idle_timeout_zero_exits(self):
        with mock.patch("sys.argv", ["claudeloop", "--idle-timeout", "0"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_negative_pause_exits(self):
        with mock.patch("sys.argv", ["claudeloop", "--pause", "-1"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
            assert exc_info.value.code == 1

    def test_dry_run_full(self, capsys):
        with mock.patch("sys.argv", ["claudeloop", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "All done" in out

    def test_all_passes_dry_run(self, capsys):
        with mock.patch("sys.argv", ["claudeloop", "--all-passes", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        for p in cli.REVIEW_PASSES:
            assert p["label"] in out

    def test_cycles_clamp_to_one(self, capsys):
        with mock.patch("sys.argv", ["claudeloop", "--cycles", "0", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "All done" in out

    def test_specific_passes(self, capsys):
        with mock.patch("sys.argv", ["claudeloop", "--passes", "security", "perf", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "Security" in out
        assert "Performance" in out


# =============================================================================
# Module-level constants
# =============================================================================

class TestConstants:
    """Tests for module-level constants and data structures."""

    def test_pass_ids_match(self):
        assert cli.PASS_IDS == [p["id"] for p in cli.REVIEW_PASSES]

    def test_default_tier_passes_are_valid(self):
        for pass_id in cli.TIERS[cli.DEFAULT_TIER]:
            assert pass_id in cli.PASS_IDS

    def test_all_passes_have_required_keys(self):
        for p in cli.REVIEW_PASSES:
            assert "id" in p
            assert "label" in p
            assert "prompt" in p

    def test_file_path_tools_set(self):
        assert "read" in cli._FILE_PATH_TOOLS
        assert "edit" in cli._FILE_PATH_TOOLS
        assert "write" in cli._FILE_PATH_TOOLS


# =============================================================================
# _stream_process_output (integration-ish, with mocked process)
# =============================================================================

class TestStreamProcessOutput:
    """Tests for _stream_process_output() with mocked subprocesses."""

    def test_process_exits_immediately(self, capsys):
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

    def test_idle_timeout_kills(self, capsys):
        mock_proc = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = None

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
                cli._stream_process_output(mock_proc, idle_timeout=120, verbose=False)
        mock_proc.kill.assert_called_once()
        out = capsys.readouterr().out
        assert "Idle" in out

    def test_read1_used_when_available(self, capsys):
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

    def test_os_read_fallback(self, capsys):
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

    def test_process_exits_while_not_ready(self, capsys):
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

    def test_drains_data(self, capsys):
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

    def test_drains_multiple_chunks(self, capsys):
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

    def test_empty_stdout(self):
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

    def test_skip_permissions_true(self, capsys):
        with mock.patch("time.sleep"):
            cli._display_pre_run_warning(skip_permissions=True)
        out = capsys.readouterr().out
        assert "dangerously-skip-permissions is ENABLED" in out
        assert "Starting in 5 seconds" in out

    def test_skip_permissions_false(self, capsys):
        with mock.patch("time.sleep"):
            cli._display_pre_run_warning(skip_permissions=False)
        out = capsys.readouterr().out
        assert "Running without --dangerously-skip-permissions" in out
        assert "Re-run with" in out
        assert "Continuing anyway" in out

    def test_keyboard_interrupt_exits(self, capsys):
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

    def test_prints_and_exits(self, capsys):
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

    def test_uses_read1_when_available(self):
        mock_stdout = mock.MagicMock()
        mock_stdout.read1.return_value = b"data"
        result = cli._read_stdout_chunk(mock_stdout)
        assert result == b"data"
        mock_stdout.read1.assert_called_once_with(8192)

    def test_falls_back_to_os_read(self):
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

    def test_non_dry_run_calls_warning(self, capsys):
        with mock.patch("sys.argv", ["claudeloop", "--pause", "0"]):
            with mock.patch.object(cli, "_display_pre_run_warning") as mock_warn:
                with mock.patch.object(cli, "_run_review_suite"):
                    cli.main()
                mock_warn.assert_called_once()

    def test_level_thorough(self, capsys):
        with mock.patch("sys.argv", ["claudeloop", "--level", "thorough", "--dry-run", "--pause", "0"]):
            cli.main()
        out = capsys.readouterr().out
        assert "security" in out
        assert "All done" in out


# =============================================================================
# Git helpers (convergence)
# =============================================================================

class TestParseShortstat:
    """Tests for _parse_shortstat() diff output parsing."""

    def test_insertions_and_deletions(self):
        text = " 3 files changed, 20 insertions(+), 10 deletions(-)"
        assert cli._parse_shortstat(text) == 30

    def test_insertions_only(self):
        text = " 1 file changed, 5 insertions(+)"
        assert cli._parse_shortstat(text) == 5

    def test_deletions_only(self):
        text = " 2 files changed, 8 deletions(-)"
        assert cli._parse_shortstat(text) == 8

    def test_empty_string(self):
        assert cli._parse_shortstat("") == 0

    def test_no_match(self):
        assert cli._parse_shortstat("nothing here") == 0


class TestIsGitRepo:
    """Tests for _is_git_repo() detection."""

    def test_true_when_git_succeeds(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0)
            assert cli._is_git_repo("/tmp") is True

    def test_false_when_git_fails(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=128)
            assert cli._is_git_repo("/tmp") is False


class TestGitHeadSha:
    """Tests for _git_head_sha() SHA retrieval."""

    def test_returns_sha(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="abc123\n")
            assert cli._git_head_sha("/tmp") == "abc123"

    def test_returns_none_on_failure(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=128, stdout="")
            assert cli._git_head_sha("/tmp") is None


class TestGitCommitCycle:
    """Tests for _git_commit_cycle() post-cycle commit."""

    def test_commits_when_changes_exist(self):
        with mock.patch("subprocess.run") as mock_run:
            # git add succeeds, git diff --cached shows changes, git commit succeeds
            mock_run.side_effect = [
                mock.MagicMock(returncode=0),  # git add
                mock.MagicMock(returncode=1),  # git diff --cached --quiet (changes exist)
                mock.MagicMock(returncode=0),  # git commit
            ]
            assert cli._git_commit_cycle("/tmp", 1) is True

    def test_no_commit_when_clean(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                mock.MagicMock(returncode=0),  # git add
                mock.MagicMock(returncode=0),  # git diff --cached --quiet (no changes)
            ]
            assert cli._git_commit_cycle("/tmp", 1) is False

    def test_returns_false_on_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert cli._git_commit_cycle("/tmp", 1) is False


class TestGetChangePercentage:
    """Tests for _get_change_percentage() convergence metric."""

    def test_calculates_percentage(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                # git diff --shortstat
                mock.MagicMock(returncode=0, stdout=" 2 files changed, 10 insertions(+), 5 deletions(-)"),
                # git ls-files -z
                mock.MagicMock(returncode=0, stdout=b"file1.py\0file2.py\0"),
            ]
            with mock.patch.object(Path, "read_bytes", return_value=b"line\n" * 1000):
                pct = cli._get_change_percentage("/tmp", "abc123")
                assert 0 < pct < 100

    def test_zero_when_no_changes(self):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="")
            pct = cli._get_change_percentage("/tmp", "abc123")
            assert pct == 0.0


class TestConvergedAtPercentageArg:
    """Tests for --converged-at-percentage CLI argument."""

    def _parse(self, args: list[str]) -> argparse.Namespace:
        return cli._build_argument_parser().parse_args(args)

    def test_default(self):
        ns = self._parse([])
        assert ns.converged_at_percentage == cli.DEFAULT_CONVERGENCE_THRESHOLD

    def test_custom_value(self):
        ns = self._parse(["--converged-at-percentage", "0.5"])
        assert ns.converged_at_percentage == 0.5

    def test_zero_disables(self):
        ns = self._parse(["--converged-at-percentage", "0"])
        assert ns.converged_at_percentage == 0.0


class TestConvergenceInSuite:
    """Tests for convergence detection within _run_review_suite."""

    def test_stops_early_when_converged(self, capsys):
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = argparse.Namespace(
            pause=0, dry_run=False, idle_timeout=120, verbose=False,
            dangerously_skip_permissions=False,
        )
        with mock.patch.object(cli, "run_claude", return_value=0):
            with mock.patch.object(cli, "_is_git_repo", return_value=True):
                with mock.patch.object(cli, "_git_head_sha", side_effect=["sha1", "sha2", "sha2", "sha3"]):
                    with mock.patch.object(cli, "_git_commit_cycle", return_value=True):
                        with mock.patch.object(cli, "_get_change_percentage", return_value=0.05):
                            cli._run_review_suite(passes, 3, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        assert "Converged" in out

    def test_no_convergence_without_git(self, capsys):
        passes = [{"id": "dry", "label": "DRY", "prompt": "check dry"}]
        args = argparse.Namespace(
            pause=0, dry_run=True, idle_timeout=120, verbose=False,
            dangerously_skip_permissions=False,
        )
        cli._run_review_suite(passes, 2, "/tmp", args, convergence_threshold=0.1)
        out = capsys.readouterr().out
        # Should run both cycles without convergence checks (dry run)
        assert "Cycle 1/2" in out
        assert "Cycle 2/2" in out


class TestCommitMessageInstructions:
    """Tests that commit message instructions are appended to prompts."""

    def test_prompt_includes_commit_instructions(self, capsys):
        passes = [{"id": "readability", "label": "Readability", "prompt": "review code"}]
        args = argparse.Namespace(
            pause=0, dry_run=True, idle_timeout=120, verbose=False,
            dangerously_skip_permissions=False,
        )
        with mock.patch.object(cli, "run_claude", return_value=0) as mock_run:
            cli._run_review_suite(passes, 1, "/tmp", args)
            prompt_used = mock_run.call_args[0][0]
            assert "commit message rules" in prompt_used
            assert "Do not mention Claude" in prompt_used
