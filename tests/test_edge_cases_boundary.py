"""Edge case and boundary condition tests across all modules.

Covers: off-by-one errors, empty/null inputs, boundary values,
negative numbers, Unicode/encoding edge cases, and empty collections
that aren't tested elsewhere.
"""

from __future__ import annotations

import io
import json
import math
import time
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest

from checkloop import checks, checkpoint, git, monitoring, process, streaming, suite, terminal
from checkloop.checkpoint import (
    CheckpointData,
    _has_valid_field_types,
    _is_strict_int,
    _is_strict_number,
    _is_string_list,
)
from checkloop.checks import CheckDef
from checkloop.process import CheckResult
from helpers import make_check, make_checkpoint_data, make_git_result, make_suite_args


# =============================================================================
# terminal.py — compute_summary_stats edge cases
# =============================================================================


class TestComputeSummaryStatsEdgeCases:
    """Edge cases for compute_summary_stats()."""

    def test_empty_results_returns_all_zeros(self) -> None:
        stats = terminal.compute_summary_stats([])
        assert stats.succeeded == 0
        assert stats.failed == 0
        assert stats.killed == 0
        assert stats.total_lines == 0
        assert stats.with_changes == 0

    def test_all_none_lines_changed(self) -> None:
        """lines_changed=None should be treated as 0 in sum."""
        row = terminal.SummaryRow(
            check_id="a", label="A", cycle=1, exit_code=0,
            kill_reason=None, made_changes=True,
            lines_changed=None, change_pct=None, duration="0m01s",
        )
        stats = terminal.compute_summary_stats([row])
        assert stats.total_lines == 0
        assert stats.with_changes == 1

    def test_mix_of_none_and_int_lines_changed(self) -> None:
        rows = [
            terminal.SummaryRow(
                check_id="a", label="A", cycle=1, exit_code=0,
                kill_reason=None, made_changes=True,
                lines_changed=None, change_pct=None, duration="0m01s",
            ),
            terminal.SummaryRow(
                check_id="b", label="B", cycle=1, exit_code=0,
                kill_reason=None, made_changes=True,
                lines_changed=42, change_pct=1.0, duration="0m02s",
            ),
        ]
        stats = terminal.compute_summary_stats(rows)
        assert stats.total_lines == 42


# =============================================================================
# process.py — _check_hard_timeout boundary
# =============================================================================


class TestCheckHardTimeoutBoundary:
    """Boundary tests for _check_hard_timeout()."""

    def test_exactly_at_threshold_does_not_trigger(self) -> None:
        """elapsed == check_timeout uses > comparison, so exact match doesn't kill."""
        mock_proc = mock.MagicMock()
        with mock.patch("time.time", return_value=160.0):
            result = process._check_hard_timeout(
                check_start_time=100.0, check_timeout=60, process=mock_proc,
            )
        assert result is False

    def test_one_nanosecond_over_threshold_triggers(self) -> None:
        """Just barely over the threshold should trigger."""
        mock_proc = mock.MagicMock()
        with mock.patch("time.time", return_value=160.0000001):
            with mock.patch.object(process, "_kill_process_group"):
                result = process._check_hard_timeout(
                    check_start_time=100.0, check_timeout=60, process=mock_proc,
                )
        assert result is True

    def test_negative_check_timeout_disabled(self) -> None:
        """Negative timeout should be treated as disabled (same as 0)."""
        mock_proc = mock.MagicMock()
        result = process._check_hard_timeout(
            check_start_time=0.0, check_timeout=-5, process=mock_proc,
        )
        assert result is False


# =============================================================================
# process.py — _check_memory_limit boundary
# =============================================================================


class TestCheckMemoryLimitBoundary:
    """Boundary tests for _check_memory_limit()."""

    def test_exactly_at_limit_does_not_kill(self) -> None:
        """rss_mb == max_memory_mb uses > comparison, so exact match doesn't kill."""
        now = time.time()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=4096.0):
            exceeded, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock.MagicMock(),
                last_memory_check=now - 20,
            )
        assert exceeded is False

    def test_one_mb_over_limit_kills(self) -> None:
        """One MB over the limit should trigger kill."""
        now = time.time()
        mock_proc = mock.MagicMock()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=4097.0):
            with mock.patch.object(process, "_kill_process_group") as mock_kill:
                exceeded, _ = process._check_memory_limit(
                    session_id=123, max_memory_mb=4096,
                    check_start_time=now - 60, process=mock_proc,
                    last_memory_check=now - 20,
                )
        assert exceeded is True
        mock_kill.assert_called_once_with(mock_proc)

    def test_negative_max_memory_disabled(self) -> None:
        """Negative max_memory_mb should be treated as disabled."""
        exceeded, _ = process._check_memory_limit(
            session_id=123, max_memory_mb=-100,
            check_start_time=time.time(), process=mock.MagicMock(),
            last_memory_check=0.0,
        )
        assert exceeded is False


# =============================================================================
# streaming.py — process_jsonl_buffer with non-dict JSON
# =============================================================================


class TestProcessJsonlBufferNonDictJson:
    """Edge cases for process_jsonl_buffer with valid JSON that isn't a dict."""

    def test_json_array_does_not_crash(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A JSON array is valid JSON but not a stream event dict."""
        buf = bytearray(b'[1, 2, 3]\n')
        remainder = streaming.process_jsonl_buffer(buf, time.time(), debug=False)
        assert remainder == bytearray()

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
        assert remainder == bytearray()


# =============================================================================
# git.py — _count_file_lines with edge case line endings
# =============================================================================


class TestCountFileLinesLineEndings:
    """Edge cases for _count_file_lines() with different line endings."""

    def test_cr_only_line_endings_counted_as_zero(self, tmp_path: Path) -> None:
        """Old Mac line endings (\\r without \\n) are counted as 0 lines."""
        f = tmp_path / "cr_only.txt"
        f.write_bytes(b"line1\rline2\rline3\r")
        assert git._count_file_lines(f) == 0

    def test_mixed_cr_and_crlf(self, tmp_path: Path) -> None:
        """Mix of \\r and \\r\\n: only \\n characters are counted."""
        f = tmp_path / "mixed.txt"
        f.write_bytes(b"line1\r\nline2\rline3\r\nline4\r")
        assert git._count_file_lines(f) == 2

    def test_file_with_only_newlines(self, tmp_path: Path) -> None:
        """A file with only newlines should count them all."""
        f = tmp_path / "newlines.txt"
        f.write_bytes(b"\n\n\n\n\n")
        assert git._count_file_lines(f) == 5

    def test_single_byte_file(self, tmp_path: Path) -> None:
        """A single non-newline byte."""
        f = tmp_path / "single.txt"
        f.write_bytes(b"x")
        assert git._count_file_lines(f) == 0

    def test_single_newline_byte(self, tmp_path: Path) -> None:
        """A single newline byte."""
        f = tmp_path / "nl.txt"
        f.write_bytes(b"\n")
        assert git._count_file_lines(f) == 1

    def test_null_byte_at_position_zero(self, tmp_path: Path) -> None:
        """A file starting with a null byte is treated as binary."""
        f = tmp_path / "null_start.bin"
        f.write_bytes(b"\0line1\nline2\n")
        assert git._count_file_lines(f) == 0


# =============================================================================
# checkpoint.py — load_checkpoint with missing convergence_threshold
# =============================================================================


class TestLoadCheckpointMissingOptionalFields:
    """Edge cases for load_checkpoint when fields are missing."""

    def test_missing_convergence_threshold_rejected(self, tmp_path: Path) -> None:
        """A checkpoint missing convergence_threshold should be rejected."""
        raw: dict[str, Any] = dict(make_checkpoint_data(workdir=str(tmp_path)))
        del raw["convergence_threshold"]
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps(raw))
        assert checkpoint.load_checkpoint(str(tmp_path)) is None

    def test_missing_prev_change_pct_still_valid(self, tmp_path: Path) -> None:
        """prev_change_pct missing is treated as None (not in required_keys)."""
        raw: dict[str, Any] = dict(make_checkpoint_data(workdir=str(tmp_path)))
        # prev_change_pct defaults to None in make_checkpoint_data
        # Explicitly remove it to test that it's treated as None
        if "prev_change_pct" in raw:
            del raw["prev_change_pct"]
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps(raw))
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        # Should be accepted since prev_change_pct defaults to None via .get()
        assert loaded is not None

    def test_missing_previously_changed_ids_still_valid(self, tmp_path: Path) -> None:
        """previously_changed_ids missing is treated as None."""
        raw: dict[str, Any] = dict(make_checkpoint_data(workdir=str(tmp_path)))
        if "previously_changed_ids" in raw:
            del raw["previously_changed_ids"]
        path = tmp_path / checkpoint._CHECKPOINT_FILENAME
        path.write_text(json.dumps(raw))
        loaded = checkpoint.load_checkpoint(str(tmp_path))
        assert loaded is not None


# =============================================================================
# checks.py — looks_dangerous edge cases
# =============================================================================


class TestLooksDangerousAdditional:
    """Additional edge cases for looks_dangerous()."""

    def test_keyword_at_start_of_string(self) -> None:
        assert checks.looks_dangerous("rm -rf / now") is True

    def test_keyword_at_end_of_string(self) -> None:
        assert checks.looks_dangerous("please rm -rf /") is True

    def test_multiple_dangerous_keywords(self) -> None:
        """String with multiple dangerous keywords should still be detected."""
        assert checks.looks_dangerous("rm -rf / and drop database") is True

    def test_partial_keyword_not_detected(self) -> None:
        """'format' alone should not match if not followed by matching pattern."""
        assert checks.looks_dangerous("reformat the code") is False

    def test_keyword_surrounded_by_punctuation(self) -> None:
        assert checks.looks_dangerous("(rm -rf /)") is True

    def test_very_long_string_with_keyword_at_end(self) -> None:
        """Keyword buried at the end of a long string should still be found."""
        padding = "safe text " * 10000
        assert checks.looks_dangerous(padding + "rm -rf /") is True


# =============================================================================
# git.py — _parse_shortstat edge cases
# =============================================================================


class TestParseShortstatAdditional:
    """Additional edge cases for _parse_shortstat()."""

    def test_negative_number_before_insertion(self) -> None:
        """\\d+ matches digits after the minus sign (git never outputs negatives)."""
        # Regex matches "5" from "-5" since \d+ ignores the leading minus
        assert git._parse_shortstat(" 1 file changed, -5 insertions(+)") == 5

    def test_decimal_number_partial_match(self) -> None:
        """'1.5 insertions' — regex matches '5' as the digits before ' insertion'."""
        result = git._parse_shortstat(" 1 file changed, 1.5 insertions(+)")
        # \d+ matches "5" from "1.5" because "5 insertion" is the first \d+ match
        assert result == 5

    def test_no_space_before_insertion(self) -> None:
        """Without a space before 'insertion', the regex might not match."""
        result = git._parse_shortstat(" 1 file changed,5 insertions(+)")
        # \d+ matches "5" because regex doesn't require leading space
        assert result == 5


# =============================================================================
# git.py — build_changed_files_prefix edge cases
# =============================================================================


class TestBuildChangedFilesPrefixEdgeCases:
    """Edge cases for build_changed_files_prefix()."""

    def test_single_empty_string_filename(self) -> None:
        """An empty-string filename is unusual but should not crash."""
        result = git.build_changed_files_prefix([""])
        assert "1 file(s)" in result

    def test_very_long_filename(self) -> None:
        """A filename with 1000 characters should work."""
        long_name = "a" * 1000 + ".py"
        result = git.build_changed_files_prefix([long_name])
        assert long_name in result


# =============================================================================
# checkpoint.py — prompt_resume edge cases
# =============================================================================


class TestPromptResumeEdgeCases:
    """Edge cases for prompt_resume()."""

    def test_user_says_YES_uppercase(self, tmp_path: Path) -> None:
        """'YES' (uppercase) should be accepted as resume."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "YES\n"
            with mock.patch("select.select", return_value=([mock_stdin], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is True

    def test_user_says_yes_with_whitespace(self, tmp_path: Path) -> None:
        """'  yes  ' with surrounding whitespace should be accepted."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "  yes  \n"
            with mock.patch("select.select", return_value=([mock_stdin], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is True

    def test_user_says_ye_not_accepted(self, tmp_path: Path) -> None:
        """'ye' is not 'y' or 'yes', so it should not resume."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.return_value = "ye\n"
            with mock.patch("select.select", return_value=([mock_stdin], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is False

    def test_readline_oserror_returns_false(self, tmp_path: Path) -> None:
        """When readline() raises OSError, should return False."""
        data = make_checkpoint_data(workdir=str(tmp_path))
        checkpoint.save_checkpoint(str(tmp_path), data)
        with mock.patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_stdin.readline.side_effect = OSError("broken pipe")
            with mock.patch("select.select", return_value=([mock_stdin], [], [])):
                result = checkpoint.prompt_resume(str(tmp_path))
        assert result is False


# =============================================================================
# terminal.py — format_duration additional edge cases
# =============================================================================


class TestFormatDurationAdditional:
    """Additional edge cases for format_duration()."""

    def test_negative_zero(self) -> None:
        assert terminal.format_duration(-0.0) == "0m00s"

    def test_one_second(self) -> None:
        assert terminal.format_duration(1) == "0m01s"

    def test_59_seconds(self) -> None:
        assert terminal.format_duration(59) == "0m59s"

    def test_61_seconds(self) -> None:
        assert terminal.format_duration(61) == "1m01s"


# =============================================================================
# monitoring.py — edge cases
# =============================================================================


class TestMonitoringEdgeCases:
    """Edge cases for monitoring module."""

    def test_sum_rss_from_ps_empty_stdout(self) -> None:
        """ps returning empty stdout should return 0.0."""
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="")):
            assert monitoring._sum_rss_from_ps("-p", "1") == 0.0

    def test_sum_rss_from_ps_whitespace_only_stdout(self) -> None:
        """ps returning whitespace-only stdout should return 0.0."""
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="   \n  \n")):
            # After strip(), the string is empty, so returns 0.0
            result = monitoring._sum_rss_from_ps("-p", "1")
            assert result == 0.0

    def test_sum_rss_from_ps_all_non_numeric(self) -> None:
        """ps returning only non-numeric lines should return 0.0."""
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="abc\ndef\n")):
            result = monitoring._sum_rss_from_ps("-p", "1")
            assert result == 0.0

    def test_kill_pids_single_pid(self) -> None:
        """Killing a single PID should return 1 if successful."""
        with mock.patch("os.kill"):
            assert monitoring.kill_pids([42]) == 1

    def test_kill_pids_all_dead(self) -> None:
        """If all PIDs are already dead, killed count should be 0."""
        with mock.patch("os.kill", side_effect=OSError("No such process")):
            assert monitoring.kill_pids([1, 2, 3]) == 0


# =============================================================================
# process.py — _check_buffer_overflow edge cases
# =============================================================================


class TestCheckBufferOverflow:
    """Edge cases for _check_buffer_overflow()."""

    def test_buffer_at_exact_limit(self) -> None:
        """Buffer at exactly _MAX_BUFFER_SIZE should NOT be truncated."""
        buf = bytearray(b"x" * process._MAX_BUFFER_SIZE)
        result = process._check_buffer_overflow(buf)
        assert len(result) == process._MAX_BUFFER_SIZE

    def test_buffer_one_over_limit(self) -> None:
        """Buffer one byte over _MAX_BUFFER_SIZE should be cleared."""
        buf = bytearray(b"x" * (process._MAX_BUFFER_SIZE + 1))
        result = process._check_buffer_overflow(buf)
        assert len(result) == 0

    def test_empty_buffer(self) -> None:
        """Empty buffer should be returned unchanged."""
        buf = bytearray()
        result = process._check_buffer_overflow(buf)
        assert len(result) == 0


# =============================================================================
# suite.py — _build_suite_state edge cases
# =============================================================================


class TestBuildSuiteStateEdgeCases:
    """Edge cases for _build_suite_state()."""

    def test_none_resume_creates_fresh_state(self) -> None:
        state = suite._build_suite_state(None)
        assert state.start_cycle == 1
        assert state.start_check_index == 0
        assert state.resume_active_check_ids is None
        assert state.resume_changed == set()
        assert state.prev_change_pct is None
        assert state.previously_changed_ids is None
        assert state.started_at != ""

    def test_resume_with_previously_changed_ids(self) -> None:
        data = make_checkpoint_data(
            previously_changed_ids=["a", "b"],
            prev_change_pct=2.5,
        )
        state = suite._build_suite_state(data)
        assert state.previously_changed_ids == {"a", "b"}
        assert state.prev_change_pct == 2.5


# =============================================================================
# suite.py — _resolve_cycle_checks edge cases
# =============================================================================


class TestResolveCycleChecksEdgeCases:
    """Edge cases for _resolve_cycle_checks()."""

    def test_resume_check_not_in_selected(self) -> None:
        """When resume active_check_ids includes IDs not in selected_checks, they're filtered."""
        state = suite._SuiteState()
        state.resume_active_check_ids = ["a", "b", "c"]
        state.start_check_index = 0
        state.resume_changed = set()
        selected = [make_check("a"), make_check("c")]
        active, start_idx, changed = suite._resolve_cycle_checks(selected, state)
        ids = [c["id"] for c in active]
        assert "b" not in ids
        assert "a" in ids
        assert "c" in ids

    def test_non_resume_returns_all_selected(self) -> None:
        state = suite._SuiteState()
        selected = [make_check("x"), make_check("y")]
        active, start_idx, changed = suite._resolve_cycle_checks(selected, state)
        assert len(active) == 2
        assert start_idx == 0
        assert changed is None


# =============================================================================
# git.py — _cached_total_tracked_lines edge cases
# =============================================================================


class TestCachedTotalTrackedLinesEdgeCases:
    """Edge cases for _cached_total_tracked_lines()."""

    def test_oserror_on_resolve_returns_1(self) -> None:
        """When Path.resolve() raises OSError, should return 1."""
        with mock.patch.object(Path, "resolve", side_effect=OSError("bad path")):
            result = git._cached_total_tracked_lines("/nonexistent\x00path")
        assert result == 1


# =============================================================================
# streaming.py — _print_event with edge case dict values
# =============================================================================


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


# =============================================================================
# checkpoint.py — _is_strict_int edge cases
# =============================================================================


class TestIsStrictIntEdgeCases:
    """Additional boundary tests for _is_strict_int()."""

    def test_none_is_rejected(self) -> None:
        assert _is_strict_int(None) is False

    def test_string_number_is_rejected(self) -> None:
        assert _is_strict_int("5") is False

    def test_negative_one_with_min_zero(self) -> None:
        assert _is_strict_int(-1, min_value=0) is False

    def test_zero_with_min_zero(self) -> None:
        assert _is_strict_int(0, min_value=0) is True

    def test_max_python_int(self) -> None:
        """Very large ints should still be accepted."""
        assert _is_strict_int(2**63) is True

    def test_list_is_rejected(self) -> None:
        assert _is_strict_int([1]) is False

    def test_dict_is_rejected(self) -> None:
        assert _is_strict_int({"a": 1}) is False


# =============================================================================
# checkpoint.py — _is_string_list edge cases
# =============================================================================


class TestIsStringListEdgeCases:
    """Additional edge cases for _is_string_list()."""

    def test_tuple_is_rejected(self) -> None:
        """Tuples are not lists."""
        assert _is_string_list(("a", "b")) is False

    def test_single_string_item(self) -> None:
        assert _is_string_list(["hello"]) is True

    def test_unicode_strings(self) -> None:
        assert _is_string_list(["こんにちは", "🎉"]) is True

    def test_empty_strings(self) -> None:
        assert _is_string_list(["", "", ""]) is True

    def test_int_is_rejected(self) -> None:
        assert _is_string_list(42) is False

    def test_none_is_rejected(self) -> None:
        assert _is_string_list(None) is False


# =============================================================================
# checks.py — tier configuration consistency
# =============================================================================


class TestTierConsistency:
    """Tests for tier configuration consistency."""

    def test_all_tier_ids_are_valid(self) -> None:
        """Every check ID in every tier must exist in CHECK_IDS."""
        for tier_name, tier_ids in checks.TIERS.items():
            for check_id in tier_ids:
                assert check_id in checks.CHECK_IDS, f"{check_id} from tier {tier_name} not in CHECK_IDS"

    def test_exhaustive_tier_includes_all_checks(self) -> None:
        """The exhaustive tier should include every defined check."""
        assert set(checks.TIER_EXHAUSTIVE) == set(checks.CHECK_IDS)

    def test_basic_is_subset_of_thorough(self) -> None:
        assert set(checks.TIER_BASIC).issubset(set(checks.TIER_THOROUGH))

    def test_thorough_is_subset_of_exhaustive(self) -> None:
        assert set(checks.TIER_THOROUGH).issubset(set(checks.TIER_EXHAUSTIVE))

    def test_bookend_checks_in_all_tiers(self) -> None:
        """test-fix and test-validate should appear in all tiers."""
        for tier_name, tier_ids in checks.TIERS.items():
            assert "test-fix" in tier_ids, f"test-fix missing from {tier_name}"
            assert "test-validate" in tier_ids, f"test-validate missing from {tier_name}"

    def test_check_ids_have_unique_values(self) -> None:
        """No duplicate check IDs."""
        assert len(checks.CHECK_IDS) == len(set(checks.CHECK_IDS))

    def test_all_checks_have_nonempty_prompt(self) -> None:
        for check in checks.CHECKS:
            assert check["prompt"].strip(), f"Check {check['id']} has empty prompt"

    def test_all_checks_have_nonempty_label(self) -> None:
        for check in checks.CHECKS:
            assert check["label"].strip(), f"Check {check['id']} has empty label"


# =============================================================================
# suite.py — CheckOutcome edge cases
# =============================================================================


class TestCheckOutcomeToSummaryDict:
    """Edge cases for CheckOutcome.to_summary_dict()."""

    def test_all_none_optional_fields(self) -> None:
        outcome = suite.CheckOutcome(
            check_id="test", label="Test", cycle=1,
            exit_code=0, kill_reason=None,
            made_changes=False, lines_changed=None,
            change_pct=None, duration_seconds=0.0,
        )
        row = outcome.to_summary_dict()
        assert row["lines_changed"] is None
        assert row["change_pct"] is None
        assert row["kill_reason"] is None
        assert row["duration"] == "0m00s"

    def test_zero_duration(self) -> None:
        outcome = suite.CheckOutcome(
            check_id="t", label="T", cycle=1,
            exit_code=0, kill_reason=None,
            made_changes=False, lines_changed=0,
            change_pct=0.0, duration_seconds=0.0,
        )
        row = outcome.to_summary_dict()
        assert row["duration"] == "0m00s"

    def test_negative_duration(self) -> None:
        """Negative duration (clock skew) should be handled gracefully."""
        outcome = suite.CheckOutcome(
            check_id="t", label="T", cycle=1,
            exit_code=0, kill_reason=None,
            made_changes=False, lines_changed=0,
            change_pct=0.0, duration_seconds=-5.0,
        )
        row = outcome.to_summary_dict()
        assert row["duration"] == "0m00s"  # format_duration clamps negative to 0
