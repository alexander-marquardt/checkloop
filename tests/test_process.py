"""Tests for checkloop.process — command building, spawning, run_claude, and process cleanup."""

from __future__ import annotations

import io
import signal
import subprocess
import time
from unittest import mock

import pytest

from checkloop import process
from checkloop.streaming import process_jsonl_buffer


class TestBuildClaudeCommand:
    """Tests for _build_claude_command() CLI argument assembly."""

    def test_with_skip_permissions(self) -> None:
        cmd = process._build_claude_command("review code", skip_permissions=True)
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "-p" in cmd
        assert "review code" in cmd
        assert "--output-format" in cmd
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    def test_without_skip_permissions(self) -> None:
        cmd = process._build_claude_command("review code", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd
        assert "review code" in cmd

    def test_default_skip_permissions_is_false(self) -> None:
        cmd = process._build_claude_command("review code", skip_permissions=False)
        assert "--dangerously-skip-permissions" not in cmd

    def test_empty_prompt(self) -> None:
        cmd = process._build_claude_command("", skip_permissions=False)
        assert "-p" in cmd
        assert "" in cmd

    def test_prompt_with_special_characters(self) -> None:
        cmd = process._build_claude_command("review 'code' with \"quotes\" & $vars", skip_permissions=False)
        assert "review 'code' with \"quotes\" & $vars" in cmd


class TestSanitizedEnv:
    """Tests for SANITIZED_ENV — environment stripping for subprocesses."""

    def test_claudecode_key_stripped(self) -> None:
        """CLAUDECODE env var must be removed to prevent nested-invocation refusal."""
        assert "CLAUDECODE" not in process.SANITIZED_ENV

    def test_path_preserved(self) -> None:
        """PATH must be preserved so subprocess can find the claude binary."""
        import os
        assert process.SANITIZED_ENV.get("PATH") == os.environ.get("PATH")

    def test_other_env_vars_preserved(self) -> None:
        """Non-CLAUDECODE env vars should pass through unchanged."""
        import os
        for key in ("HOME", "USER", "LANG"):
            if key in os.environ:
                assert process.SANITIZED_ENV.get(key) == os.environ[key]


class TestCheckIdleTimeout:
    """Tests for _check_idle_timeout() boundary conditions."""

    def test_not_expired(self) -> None:
        mock_proc = mock.MagicMock()
        with mock.patch("time.time", return_value=100.0):
            result = process._check_idle_timeout(
                last_output_time=90.0, idle_timeout=120,
                check_start_time=80.0, process=mock_proc,
            )
        assert result is False

    def test_exactly_at_threshold_does_not_trigger(self) -> None:
        """Idle timeout uses strict > comparison, so exactly at threshold doesn't trigger."""
        mock_proc = mock.MagicMock()
        with mock.patch("time.time", return_value=220.0):
            result = process._check_idle_timeout(
                last_output_time=100.0, idle_timeout=120,
                check_start_time=80.0, process=mock_proc,
            )
        assert result is False

    def test_one_second_over_threshold(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        with mock.patch("time.time", return_value=221.0), \
             mock.patch.object(process, "_kill_process_group"), \
             mock.patch.object(process, "find_all_descendant_pids", return_value=[]):
            result = process._check_idle_timeout(
                last_output_time=100.0, idle_timeout=120,
                check_start_time=80.0, process=mock_proc,
            )
        assert result is True

    def test_descendants_running_prevents_kill(self) -> None:
        """When idle timeout triggers but descendants are running, don't kill."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        with mock.patch("time.time", return_value=221.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill, \
             mock.patch.object(process, "find_all_descendant_pids", return_value=[200, 201]):
            result = process._check_idle_timeout(
                last_output_time=100.0, idle_timeout=120,
                check_start_time=80.0, process=mock_proc,
            )
        assert result is False  # Should NOT kill
        mock_kill.assert_not_called()

    def test_no_descendants_allows_kill(self) -> None:
        """When idle timeout triggers and no descendants, proceed with kill."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        with mock.patch("time.time", return_value=221.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill, \
             mock.patch.object(process, "find_all_descendant_pids", return_value=[]):
            result = process._check_idle_timeout(
                last_output_time=100.0, idle_timeout=120,
                check_start_time=80.0, process=mock_proc,
            )
        assert result is True  # Should kill
        mock_kill.assert_called_once()


class TestSpawnClaudeProcess:
    """Tests for _spawn_claude_process() subprocess creation."""

    def test_file_not_found_exits(self) -> None:
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            with pytest.raises(SystemExit) as exc_info:
                process._spawn_claude_process(["claude", "-p", "hi"], "/tmp")
            assert exc_info.value.code == 1

    def test_strips_claudecode_env(self) -> None:
        import os
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "HOME": "/home"}):
            with mock.patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = mock.MagicMock()
                process._spawn_claude_process(["claude"], "/tmp")
                call_kwargs = mock_popen.call_args
                env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
                assert "CLAUDECODE" not in env

    def test_passes_cwd_and_pipes(self) -> None:
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock.MagicMock()
            process._spawn_claude_process(["claude"], "/mydir")
            call_kwargs = mock_popen.call_args
            assert call_kwargs.kwargs["cwd"] == "/mydir"
            assert call_kwargs.kwargs["stdout"] == subprocess.PIPE
            assert call_kwargs.kwargs["stderr"] == subprocess.DEVNULL
            assert call_kwargs.kwargs["stdin"] == subprocess.PIPE  # stdin piped for nudges

    def test_start_new_session(self) -> None:
        with mock.patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = mock.MagicMock()
            process._spawn_claude_process(["claude"], "/tmp")
            call_kwargs = mock_popen.call_args.kwargs
            assert call_kwargs["start_new_session"] is True

    def test_oserror_exits(self) -> None:
        with mock.patch("subprocess.Popen", side_effect=PermissionError("not executable")):
            with pytest.raises(SystemExit) as exc_info:
                process._spawn_claude_process(["claude"], "/tmp")
            assert exc_info.value.code == 1

    def test_unexpected_exception_exits(self) -> None:
        """A generic Exception from Popen should be caught and cause SystemExit."""
        with mock.patch("subprocess.Popen", side_effect=RuntimeError("something unexpected")):
            with pytest.raises(SystemExit) as exc_info:
                process._spawn_claude_process(["claude"], "/tmp")
            assert exc_info.value.code == 1


class TestRunClaude:
    """Tests for the public run_claude() function."""

    def test_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = process.run_claude("prompt", "/tmp", dry_run=True)
        assert result.exit_code == 0
        assert result.kill_reason is None
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_normal_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.stdout = io.BytesIO(b'')
        mock_proc.stderr = io.BytesIO(b'')
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = None
        mock_proc.returncode = 0

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc), \
             mock.patch.object(process, "_stream_process_output", return_value=(time.time(), None)), \
             mock.patch.object(process, "_kill_process_group"):
            result = process.run_claude("prompt", "/tmp", dry_run=False)
        assert result.exit_code == 0
        assert result.kill_reason is None

    def test_dry_run_short_prompt_no_ellipsis(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = process.run_claude("short", "/tmp", dry_run=True)
        assert result.exit_code == 0
        out = capsys.readouterr().out
        assert "short" in out
        assert "short..." not in out

    def test_dry_run_long_prompt_truncated(self, capsys: pytest.CaptureFixture[str]) -> None:
        long_prompt = "x" * 200
        result = process.run_claude(long_prompt, "/tmp", dry_run=True)
        assert result.exit_code == 0
        out = capsys.readouterr().out
        assert "..." in out

    def test_dry_run_empty_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = process.run_claude("", "/tmp", dry_run=True)
        assert result.exit_code == 0

    def test_nonzero_exit(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 1
        mock_proc.wait.return_value = None

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc), \
             mock.patch.object(process, "_stream_process_output", return_value=(time.time(), None)), \
             mock.patch.object(process, "_kill_process_group"):
            result = process.run_claude("prompt", "/tmp", dry_run=False)
        assert result.exit_code == 1
        out = capsys.readouterr().out
        assert "exited with code 1" in out

    def test_whitespace_only_prompt_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = process.run_claude("   \t\n  ", "/tmp", dry_run=True)
        assert result.exit_code == 0

    def test_unicode_prompt_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = process.run_claude("レビューコード 🔍 review", "/tmp", dry_run=True)
        assert result.exit_code == 0
        out = capsys.readouterr().out
        assert "レビューコード" in out

    def test_memory_kill_returns_kill_reason(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = -9
        mock_proc.wait.return_value = None

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc), \
             mock.patch.object(process, "_stream_process_output",
                               return_value=(time.time(), process.KILL_REASON_MEMORY)), \
             mock.patch.object(process, "_kill_process_group"):
            result = process.run_claude("prompt", "/tmp")
        assert result.kill_reason == process.KILL_REASON_MEMORY


class TestRunClaudeReturnCodeNone:
    """Test run_claude when process.returncode is None."""

    def test_returncode_none_returns_negative_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        mock_proc.returncode = None
        mock_proc.wait.return_value = None

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=(time.time(), None)):
                with mock.patch.object(process, "_kill_process_group"):
                    with mock.patch.object(process, "log_memory_usage"):
                        result = process.run_claude("test", "/tmp")
        assert result.exit_code == -1
        out = capsys.readouterr().out
        assert "exited with code -1" in out


class TestRunClaudeWaitTimeout:
    """Tests for run_claude() when process.wait() times out."""

    def test_wait_timeout_triggers_kill(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 120)
        mock_proc.returncode = -9

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=(time.time(), None)):
                with mock.patch.object(process, "_kill_process_group") as mock_kill:
                    process.run_claude("test prompt", "/tmp", idle_timeout=1)
                    assert mock_kill.call_count == 1


class TestRunClaudeWaitOSError:
    """Tests for run_claude() when process.wait() raises OSError."""

    def test_wait_oserror_handled_gracefully(self) -> None:
        """An OSError from process.wait() should be caught without crashing."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 7777
        mock_proc.wait.side_effect = OSError("child process gone")
        mock_proc.returncode = 0

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=(time.time(), None)):
                with mock.patch.object(process, "_kill_process_group"):
                    result = process.run_claude("test prompt", "/tmp", idle_timeout=1)
                    assert result.exit_code == 0


class TestExecuteClaudeProcessCleanup:
    """Test that _execute_claude_process kills the process on streaming errors."""

    def test_stream_error_kills_process(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", side_effect=RuntimeError("boom")):
                with mock.patch.object(process, "_kill_process_group") as kill_mock:
                    with pytest.raises(RuntimeError, match="boom"):
                        process._execute_claude_process(["claude"], "/tmp", idle_timeout=120, debug=False)
                    kill_mock.assert_called_with(mock_proc, extra_pids=set())


class TestKillProcessGroup:
    """Tests for _kill_process_group() cleanup."""

    def test_kills_process_group(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        with mock.patch("os.getpgid", return_value=12345) as mock_getpgid:
            with mock.patch("os.killpg") as mock_killpg:
                process._kill_process_group(mock_proc)
                mock_getpgid.assert_called_once_with(12345)
                mock_killpg.assert_called_with(12345, signal.SIGTERM)

    def test_sigkill_on_timeout(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 99
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)
        with mock.patch("os.getpgid", return_value=99):
            with mock.patch("os.killpg") as mock_killpg:
                process._kill_process_group(mock_proc)
                calls = [c for c in mock_killpg.call_args_list]
                signals_sent = [c[0][1] for c in calls]
                assert signal.SIGTERM in signals_sent
                assert signal.SIGKILL in signals_sent

    def test_handles_already_dead_process(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 1
        with mock.patch("os.getpgid", side_effect=OSError("No such process")):
            process._kill_process_group(mock_proc)

    def test_sigkill_oserror_handled(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)

        with mock.patch("os.getpgid", return_value=12345):
            with mock.patch("os.killpg") as mock_killpg:
                def killpg_side_effect(pgid: int, sig: signal.Signals) -> None:
                    if sig == signal.SIGKILL:
                        raise OSError("no such process")
                mock_killpg.side_effect = killpg_side_effect
                process._kill_process_group(mock_proc)


class TestCheckHardTimeout:
    """Tests for _check_hard_timeout() wall-clock limit."""

    def test_disabled_when_zero(self) -> None:
        mock_proc = mock.MagicMock()
        result = process._check_hard_timeout(
            check_start_time=time.time() - 9999, check_timeout=0, process=mock_proc,
        )
        assert result is False

    def test_not_exceeded(self) -> None:
        mock_proc = mock.MagicMock()
        with mock.patch("time.time", return_value=100.0):
            result = process._check_hard_timeout(
                check_start_time=90.0, check_timeout=60, process=mock_proc,
            )
        assert result is False

    def test_exceeded_kills_process(self) -> None:
        mock_proc = mock.MagicMock()
        with mock.patch("time.time", return_value=200.0):
            with mock.patch.object(process, "_kill_process_group") as mock_kill:
                result = process._check_hard_timeout(
                    check_start_time=100.0, check_timeout=60, process=mock_proc,
                )
        assert result is True
        mock_kill.assert_called_once_with(mock_proc)


class TestCheckMemoryLimit:
    """Tests for _check_memory_limit() child tree RSS monitoring."""

    def test_disabled_when_zero(self) -> None:
        exceeded, _, _ = process._check_memory_limit(
            session_id=123, max_memory_mb=0,
            check_start_time=time.time(), process=mock.MagicMock(),
            last_memory_check=time.time(),
        )
        assert exceeded is False

    def test_skipped_before_interval(self) -> None:
        now = time.time()
        exceeded, last, _ = process._check_memory_limit(
            session_id=123, max_memory_mb=4096,
            check_start_time=now, process=mock.MagicMock(),
            last_memory_check=now,  # just checked
        )
        assert exceeded is False
        assert last == now  # unchanged

    def test_under_limit(self) -> None:
        now = time.time()
        old_check = now - 20  # well past interval
        with mock.patch.object(process, "measure_session_rss_mb", return_value=500.0):
            exceeded, last, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock.MagicMock(),
                last_memory_check=old_check,
            )
        assert exceeded is False
        assert last > old_check  # updated

    def test_over_limit_kills_process(self) -> None:
        now = time.time()
        mock_proc = mock.MagicMock()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=5000.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill, \
             mock.patch.object(process, "_log_per_pid_breakdown"):
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20,
            )
        assert exceeded is True
        mock_kill.assert_called_once_with(mock_proc)


class TestStreamProcessOutputKillReasons:
    """Tests for _stream_process_output returning different kill reasons."""

    def test_hard_timeout_returns_timeout_reason(self) -> None:
        """When _check_hard_timeout triggers, kill_reason is KILL_REASON_TIMEOUT."""
        mock_proc = mock.MagicMock()
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.pid = 123

        with mock.patch.object(process, "_check_idle_timeout", return_value=False), \
             mock.patch.object(process, "_check_hard_timeout", return_value=True), \
             mock.patch.object(process, "_check_memory_limit", return_value=(False, time.time(), 0.0)):
            _, kill_reason = process._stream_process_output(
                mock_proc, idle_timeout=120, debug=False,
                check_timeout=60, max_memory_mb=0,
            )
        assert kill_reason == process.KILL_REASON_TIMEOUT

    def test_memory_limit_returns_memory_reason(self) -> None:
        """When _check_memory_limit triggers, kill_reason is KILL_REASON_MEMORY."""
        mock_proc = mock.MagicMock()
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.pid = 123

        with mock.patch.object(process, "_check_idle_timeout", return_value=False), \
             mock.patch.object(process, "_check_hard_timeout", return_value=False), \
             mock.patch.object(process, "_check_memory_limit", return_value=(True, time.time(), 5000.0)):
            _, kill_reason = process._stream_process_output(
                mock_proc, idle_timeout=120, debug=False,
                check_timeout=60, max_memory_mb=4096,
            )
        assert kill_reason == process.KILL_REASON_MEMORY


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


class TestCheckMemoryLimitBoundary:
    """Boundary tests for _check_memory_limit()."""

    def test_exactly_at_limit_does_not_kill(self) -> None:
        """rss_mb == max_memory_mb uses > comparison, so exact match doesn't kill."""
        now = time.time()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=4096.0):
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock.MagicMock(),
                last_memory_check=now - 20,
            )
        assert exceeded is False

    def test_one_mb_over_limit_kills(self) -> None:
        """One MB over the limit should trigger kill."""
        now = time.time()
        mock_proc = mock.MagicMock()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=4097.0), \
             mock.patch.object(process, "_kill_process_group") as mock_kill, \
             mock.patch.object(process, "_log_per_pid_breakdown"):
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20,
            )
        assert exceeded is True
        mock_kill.assert_called_once_with(mock_proc)

    def test_negative_max_memory_disabled(self) -> None:
        """Negative max_memory_mb should be treated as disabled."""
        exceeded, _, _ = process._check_memory_limit(
            session_id=123, max_memory_mb=-100,
            check_start_time=time.time(), process=mock.MagicMock(),
            last_memory_check=0.0,
        )
        assert exceeded is False


class TestCheckBufferOverflow:
    """Edge cases for max_buffer_size enforcement in process_jsonl_buffer()."""

    def test_buffer_at_exact_limit(self) -> None:
        """Buffer at exactly max_buffer_size should NOT be truncated."""
        limit = 1024
        buf = bytearray(b"x" * limit)
        result = process_jsonl_buffer(buf, time.time(), False, max_buffer_size=limit)
        assert len(result) == limit

    def test_buffer_one_over_limit(self) -> None:
        """Buffer one byte over max_buffer_size should be cleared."""
        limit = 1024
        buf = bytearray(b"x" * (limit + 1))
        result = process_jsonl_buffer(buf, time.time(), False, max_buffer_size=limit)
        assert len(result) == 0

    def test_empty_buffer(self) -> None:
        """Empty buffer should be returned unchanged."""
        buf = bytearray()
        result = process_jsonl_buffer(buf, time.time(), False, max_buffer_size=1024)
        assert len(result) == 0


# =============================================================================
# _check_resource_limits — combined behavior
# =============================================================================

class TestCheckResourceLimitsCombined:
    """Tests for _check_resource_limits() which combines idle, hard timeout, memory checks, and nudges."""

    def _make_mock_process(self) -> mock.MagicMock:
        proc = mock.MagicMock()
        proc.pid = 12345
        proc.stdin = mock.MagicMock()
        return proc

    def test_no_limits_exceeded(self) -> None:
        """When all limits are within bounds, returns None kill_reason."""
        proc = self._make_mock_process()
        now = time.time()
        kill_reason, last_mem, last_nudge, _ = process._check_resource_limits(
            proc, check_start_time=now, last_output_time=now,
            idle_timeout=300, check_timeout=0, max_memory_mb=0,
            last_memory_check=now, last_nudge_time=0.0,
        )
        assert kill_reason is None

    def test_idle_timeout_detected_first(self) -> None:
        """Idle timeout should be detected before hard timeout or memory."""
        proc = self._make_mock_process()
        now = time.time()
        kill_reason, _, _, _ = process._check_resource_limits(
            proc, check_start_time=now - 10, last_output_time=now - 400,
            idle_timeout=300, check_timeout=600, max_memory_mb=8192,
            last_memory_check=now, last_nudge_time=0.0,
        )
        assert kill_reason == process.KILL_REASON_IDLE

    def test_hard_timeout_when_not_idle(self) -> None:
        """Hard timeout should be detected when idle timeout hasn't triggered."""
        proc = self._make_mock_process()
        now = time.time()
        kill_reason, _, _, _ = process._check_resource_limits(
            proc, check_start_time=now - 700, last_output_time=now,
            idle_timeout=300, check_timeout=600, max_memory_mb=0,
            last_memory_check=now, last_nudge_time=0.0,
        )
        assert kill_reason == process.KILL_REASON_TIMEOUT

    def test_memory_limit_when_no_timeout(self) -> None:
        """Memory limit should be detected when timeouts haven't triggered."""
        proc = self._make_mock_process()
        now = time.time()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=10000.0), \
             mock.patch.object(process, "_log_per_pid_breakdown"):
            kill_reason, _, _, _ = process._check_resource_limits(
                proc, check_start_time=now, last_output_time=now,
                idle_timeout=300, check_timeout=0, max_memory_mb=8192,
                last_memory_check=now - 20, last_nudge_time=0.0,
            )
        assert kill_reason == process.KILL_REASON_MEMORY

    def test_nudge_sent_before_idle_timeout(self) -> None:
        """Nudge should be sent when approaching idle timeout."""
        proc = self._make_mock_process()
        now = time.time()
        # idle_seconds = 250, threshold = 300 - 60 = 240, so nudge should fire
        kill_reason, _, last_nudge, _ = process._check_resource_limits(
            proc, check_start_time=now - 250, last_output_time=now - 250,
            idle_timeout=300, check_timeout=0, max_memory_mb=0,
            last_memory_check=now, last_nudge_time=0.0,
        )
        assert kill_reason is None  # not yet timed out
        assert last_nudge > 0  # nudge was sent
        proc.stdin.write.assert_called_once()

    def test_nudge_not_sent_when_already_nudged(self) -> None:
        """Nudge should not be sent again if we already nudged this idle period."""
        proc = self._make_mock_process()
        now = time.time()
        last_output = now - 250
        # last_nudge_time > last_output_time means we already nudged
        kill_reason, _, last_nudge, _ = process._check_resource_limits(
            proc, check_start_time=now - 250, last_output_time=last_output,
            idle_timeout=300, check_timeout=0, max_memory_mb=0,
            last_memory_check=now, last_nudge_time=now - 10,  # nudged recently
        )
        assert kill_reason is None
        proc.stdin.write.assert_not_called()  # no duplicate nudge


# =============================================================================
# _kill_remaining_descendants — descendant cleanup after group/session kill
# =============================================================================


class TestKillRemainingDescendants:
    """Tests for _kill_remaining_descendants() post-teardown cleanup."""

    def test_kills_descendants_and_tracks_them(self) -> None:
        """Known descendant PIDs are killed and added to previous_descendant_pids."""
        from checkloop.monitoring import previous_descendant_pids
        known = {200, 300, 400}
        with mock.patch.object(process, "kill_pids", return_value=3) as mock_kill:
            process._kill_remaining_descendants(known, root_pid=100)
        # All PIDs except self and root should be targeted
        called_pids = set(mock_kill.call_args[0][0])
        assert called_pids == {200, 300, 400}
        # Should be tracked for follow-up sweeps
        assert {200, 300, 400} <= previous_descendant_pids

    def test_excludes_self_and_root(self) -> None:
        """The current process and the root_pid must be excluded."""
        import os
        my_pid = os.getpid()
        known = {my_pid, 100, 200}
        with mock.patch.object(process, "kill_pids", return_value=1) as mock_kill:
            process._kill_remaining_descendants(known, root_pid=100)
        called_pids = set(mock_kill.call_args[0][0])
        assert my_pid not in called_pids
        assert 100 not in called_pids
        assert called_pids == {200}

    def test_empty_known_set_is_noop(self) -> None:
        """An empty known set should not call kill_pids."""
        with mock.patch.object(process, "kill_pids") as mock_kill:
            process._kill_remaining_descendants(set(), root_pid=100)
        mock_kill.assert_not_called()

    def test_only_self_and_root_is_noop(self) -> None:
        """If known only contains self and root, nothing to kill."""
        import os
        my_pid = os.getpid()
        with mock.patch.object(process, "kill_pids") as mock_kill:
            process._kill_remaining_descendants({my_pid, 100}, root_pid=100)
        mock_kill.assert_not_called()


# =============================================================================
# _kill_process_group — descendant handling via extra_pids
# =============================================================================


class TestKillProcessGroupDescendants:
    """Tests for _kill_process_group() descendant cleanup with extra_pids."""

    def test_merges_extra_pids_with_teardown_snapshot(self) -> None:
        """extra_pids from streaming are merged with the teardown tree snapshot."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 100
        extra = {200, 300}  # collected during streaming
        teardown_snapshot = [400, 500]  # fresh snapshot at teardown

        with mock.patch("os.getpgid", return_value=100), \
             mock.patch("os.killpg"), \
             mock.patch.object(process, "find_all_descendant_pids", return_value=teardown_snapshot), \
             mock.patch.object(process, "kill_session_stragglers"), \
             mock.patch.object(process, "_kill_remaining_descendants") as mock_desc:
            process._kill_process_group(mock_proc, extra_pids=extra)

        # Should be called with the union of teardown snapshot and extra_pids
        all_known = mock_desc.call_args[0][0]
        assert all_known == {200, 300, 400, 500}
        assert mock_desc.call_args[0][1] == 100  # root_pid

    def test_no_extra_pids_still_snapshots(self) -> None:
        """Without extra_pids, only the teardown snapshot is used."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 100
        with mock.patch("os.getpgid", return_value=100), \
             mock.patch("os.killpg"), \
             mock.patch.object(process, "find_all_descendant_pids", return_value=[400]), \
             mock.patch.object(process, "kill_session_stragglers"), \
             mock.patch.object(process, "_kill_remaining_descendants") as mock_desc:
            process._kill_process_group(mock_proc)

        all_known = mock_desc.call_args[0][0]
        assert all_known == {400}

    def test_process_already_dead_still_cleans_session(self) -> None:
        """When os.getpgid raises OSError, session stragglers are still cleaned."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 100
        with mock.patch("os.getpgid", side_effect=OSError("No such process")), \
             mock.patch.object(process, "kill_session_stragglers") as mock_sess:
            process._kill_process_group(mock_proc, extra_pids={200, 300})
        mock_sess.assert_called_once_with(100)


# =============================================================================
# _stream_process_output — descendant accumulation during streaming
# =============================================================================


class TestStreamDescendantAccumulation:
    """Tests for periodic descendant scanning in _stream_process_output."""

    def test_accumulates_descendants_during_streaming(self) -> None:
        """Descendants found during periodic scans are added to accumulated set."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 100
        mock_proc.stdout = io.BytesIO(b"")  # empty → EOF immediately
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        accumulated: set[int] = set()

        # Set interval to 0 so scan triggers on every loop iteration.
        with mock.patch.object(process, "_MEMORY_CHECK_INTERVAL", 0), \
             mock.patch.object(process, "_check_resource_limits", return_value=(None, 0.0, 0.0, 0.0)), \
             mock.patch.object(process, "find_all_descendant_pids", return_value=[200, 300]), \
             mock.patch("select.select", return_value=([], [], [])):
            process._stream_process_output(
                mock_proc, idle_timeout=120, debug=False,
                accumulated_descendant_pids=accumulated,
            )

        assert {200, 300} <= accumulated

    def test_no_accumulation_without_parameter(self) -> None:
        """When accumulated_descendant_pids is None, no tree scans happen."""
        mock_proc = mock.MagicMock()
        mock_proc.pid = 100
        mock_proc.stdout = io.BytesIO(b"")
        mock_proc.poll.return_value = 0
        mock_proc.returncode = 0

        with mock.patch.object(process, "_check_resource_limits", return_value=(None, 0.0, 0.0, 0.0)), \
             mock.patch.object(process, "find_all_descendant_pids") as mock_find, \
             mock.patch("select.select", return_value=([], [], [])):
            process._stream_process_output(
                mock_proc, idle_timeout=120, debug=False,
                # accumulated_descendant_pids not provided — defaults to None
            )

        mock_find.assert_not_called()


# =============================================================================
# _log_per_pid_breakdown — forensic per-PID RSS logging
# =============================================================================


class TestLogPerPidBreakdown:
    """Tests for _log_per_pid_breakdown() forensic logging."""

    def test_logs_session_and_descendant_pids(self) -> None:
        """Combines session PIDs and known descendants into a single snapshot."""
        with mock.patch.object(process, "find_session_pids", return_value=[200, 300]) as mock_find, \
             mock.patch.object(process, "snapshot_process_rss",
                               return_value=[(200, 100.0, "node"), (300, 50.0, "python"), (400, 25.0, "sh")]) as mock_snap:
            process._log_per_pid_breakdown(session_id=100, known_descendants={300, 400})
        mock_find.assert_called_once_with(100)
        # All unique PIDs minus our own should be queried
        queried_pids = mock_snap.call_args[0][0]
        assert {200, 300, 400} <= queried_pids

    def test_excludes_own_pid(self) -> None:
        """The current process is excluded from the snapshot."""
        import os
        my_pid = os.getpid()
        with mock.patch.object(process, "find_session_pids", return_value=[my_pid, 200]), \
             mock.patch.object(process, "snapshot_process_rss", return_value=[(200, 50.0, "node")]) as mock_snap:
            process._log_per_pid_breakdown(session_id=100, known_descendants=None)
        queried_pids = mock_snap.call_args[0][0]
        assert my_pid not in queried_pids

    def test_no_pids_skips_snapshot(self) -> None:
        """When session has no PIDs and no known descendants, snapshot is not called."""
        import os
        with mock.patch.object(process, "find_session_pids", return_value=[os.getpid()]), \
             mock.patch.object(process, "snapshot_process_rss") as mock_snap:
            process._log_per_pid_breakdown(session_id=100, known_descendants=None)
        mock_snap.assert_not_called()

    def test_empty_snapshot_result_is_handled(self) -> None:
        """When snapshot returns empty (all processes dead), no crash."""
        with mock.patch.object(process, "find_session_pids", return_value=[200]), \
             mock.patch.object(process, "snapshot_process_rss", return_value=[]):
            # Should not raise
            process._log_per_pid_breakdown(session_id=100, known_descendants={300})

    def test_none_descendants_still_queries_session(self) -> None:
        """When known_descendants is None, only session PIDs are queried."""
        with mock.patch.object(process, "find_session_pids", return_value=[200]) as mock_find, \
             mock.patch.object(process, "snapshot_process_rss",
                               return_value=[(200, 50.0, "node")]) as mock_snap:
            process._log_per_pid_breakdown(session_id=100, known_descendants=None)
        mock_find.assert_called_once_with(100)
        queried_pids = mock_snap.call_args[0][0]
        assert 200 in queried_pids


# =============================================================================
# _check_memory_limit — memory growth trend warnings
# =============================================================================


class TestMemoryGrowthTrendWarnings:
    """Tests for memory growth trend warnings at 50%/75% thresholds and doubling."""

    def test_50_percent_threshold_triggers_breakdown(self) -> None:
        """Crossing the 50% threshold triggers _log_per_pid_breakdown."""
        now = time.time()
        mock_proc = mock.MagicMock()
        # prev_rss_mb=1800 (44% of 4096), current=2100 (51% of 4096) — no doubling
        with mock.patch.object(process, "measure_session_rss_mb", return_value=2100.0), \
             mock.patch.object(process, "_log_per_pid_breakdown") as mock_breakdown:
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20, prev_rss_mb=1800.0,
            )
        assert exceeded is False
        mock_breakdown.assert_called_once()

    def test_75_percent_threshold_triggers_breakdown(self) -> None:
        """Crossing the 75% threshold triggers _log_per_pid_breakdown."""
        now = time.time()
        mock_proc = mock.MagicMock()
        # prev_rss_mb=2000 (49% of 4096), current=3100 (76% of 4096)
        with mock.patch.object(process, "measure_session_rss_mb", return_value=3100.0), \
             mock.patch.object(process, "_log_per_pid_breakdown") as mock_breakdown:
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20, prev_rss_mb=2000.0,
            )
        assert exceeded is False
        # Should be called twice: once for 50% and once for 75% crossing
        assert mock_breakdown.call_count == 2

    def test_doubling_triggers_breakdown(self) -> None:
        """RSS doubling between measurements triggers _log_per_pid_breakdown."""
        now = time.time()
        mock_proc = mock.MagicMock()
        # prev=500, current=1000 — exactly doubled (under both 50% and 75% of 4096)
        with mock.patch.object(process, "measure_session_rss_mb", return_value=1000.0), \
             mock.patch.object(process, "_log_per_pid_breakdown") as mock_breakdown:
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20, prev_rss_mb=500.0,
            )
        assert exceeded is False
        mock_breakdown.assert_called_once()  # doubling

    def test_no_warning_below_thresholds(self) -> None:
        """No breakdown logged when staying below 50% with no doubling."""
        now = time.time()
        mock_proc = mock.MagicMock()
        # prev=400 (10%), current=600 (15%) — below 50%, no doubling
        with mock.patch.object(process, "measure_session_rss_mb", return_value=600.0), \
             mock.patch.object(process, "_log_per_pid_breakdown") as mock_breakdown:
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20, prev_rss_mb=400.0,
            )
        assert exceeded is False
        mock_breakdown.assert_not_called()

    def test_prev_rss_zero_skips_doubling_check(self) -> None:
        """When prev_rss_mb is 0 (first measurement), no doubling warning."""
        now = time.time()
        mock_proc = mock.MagicMock()
        # prev=0, current=500 (12%) — below 50%, prev is 0 so no doubling
        with mock.patch.object(process, "measure_session_rss_mb", return_value=500.0), \
             mock.patch.object(process, "_log_per_pid_breakdown") as mock_breakdown:
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20, prev_rss_mb=0.0,
            )
        assert exceeded is False
        mock_breakdown.assert_not_called()

    def test_returns_current_rss_for_tracking(self) -> None:
        """The third return value is the current RSS for trend tracking."""
        now = time.time()
        mock_proc = mock.MagicMock()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=1234.0):
            _, _, current_rss = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20, prev_rss_mb=1000.0,
            )
        assert abs(current_rss - 1234.0) < 0.01

    def test_over_limit_also_triggers_breakdown(self) -> None:
        """When exceeding the memory limit, breakdown is logged before killing."""
        now = time.time()
        mock_proc = mock.MagicMock()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=5000.0), \
             mock.patch.object(process, "_kill_process_group"), \
             mock.patch.object(process, "_log_per_pid_breakdown") as mock_breakdown:
            exceeded, _, _ = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20, prev_rss_mb=3000.0,
            )
        assert exceeded is True
        # 50%, 75%, and limit-exceeded all trigger breakdown
        assert mock_breakdown.call_count >= 1


# =============================================================================
# _check_memory_limit — escaped descendant RSS measurement
# =============================================================================


class TestCheckMemoryLimitEscapedDescendants:
    """Tests for _check_memory_limit() including escaped-session descendant RSS."""

    def test_includes_escaped_descendant_rss(self) -> None:
        """Descendants that escaped the session are included in total RSS."""
        now = time.time()
        mock_proc = mock.MagicMock()
        mock_proc.pid = 100
        # Session RSS = 2000, escaped descendants contribute 1000 more
        with mock.patch.object(process, "measure_session_rss_mb", return_value=2000.0), \
             mock.patch.object(process, "find_all_descendant_pids", return_value=[200, 300]), \
             mock.patch.object(process, "measure_pid_rss_mb", return_value=1000.0):
            exceeded, _, current_rss = process._check_memory_limit(
                session_id=100, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20,
                known_descendants={200, 300, 400},
            )
        # Total should be session (2000) + escaped (1000) = 3000
        assert abs(current_rss - 3000.0) < 0.01
        assert exceeded is False

    def test_no_descendants_only_session_rss(self) -> None:
        """Without known_descendants, only session RSS is measured."""
        now = time.time()
        mock_proc = mock.MagicMock()
        mock_proc.pid = 100
        with mock.patch.object(process, "measure_session_rss_mb", return_value=2000.0), \
             mock.patch.object(process, "measure_pid_rss_mb") as mock_pid_rss:
            exceeded, _, current_rss = process._check_memory_limit(
                session_id=100, max_memory_mb=4096,
                check_start_time=now - 60, process=mock_proc,
                last_memory_check=now - 20,
                known_descendants=None,
            )
        mock_pid_rss.assert_not_called()
        assert abs(current_rss - 2000.0) < 0.01


# =============================================================================
# _kill_remaining_descendants — verify_pids_dead integration
# =============================================================================


class TestKillRemainingDescendantsVerification:
    """Tests for _kill_remaining_descendants() calling verify_pids_dead."""

    def test_verify_called_after_killing(self) -> None:
        """After killing descendants, verify_pids_dead is called."""
        from checkloop.monitoring import previous_descendant_pids
        known = {200, 300}
        with mock.patch.object(process, "kill_pids", return_value=2), \
             mock.patch.object(process, "verify_pids_dead", return_value=[]) as mock_verify:
            process._kill_remaining_descendants(known, root_pid=100)
        mock_verify.assert_called_once()
        verified_pids = mock_verify.call_args[0][0]
        assert set(verified_pids) == {200, 300}
        previous_descendant_pids.discard(200)
        previous_descendant_pids.discard(300)

    def test_verify_not_called_when_none_killed(self) -> None:
        """When kill_pids returns 0, verify is not called."""
        known = {200, 300}
        with mock.patch.object(process, "kill_pids", return_value=0), \
             mock.patch.object(process, "verify_pids_dead") as mock_verify:
            process._kill_remaining_descendants(known, root_pid=100)
        mock_verify.assert_not_called()


class TestDescribeActiveWork:
    """Tests for _describe_active_work returning rich ActiveWorkSnapshot data."""

    def test_empty_tree_returns_working(self) -> None:
        with mock.patch.object(process, "find_all_descendant_pids", return_value=[]):
            snap = process._describe_active_work(root_pid=1234)
        assert snap.description == "working"
        assert snap.descendant_count == 0
        assert snap.total_rss_mb == 0.0

    def test_snapshot_empty_falls_back_to_count(self) -> None:
        """If ps returns nothing, we still report the descendant count."""
        with mock.patch.object(process, "find_all_descendant_pids", return_value=[1, 2, 3]), \
             mock.patch.object(process, "snapshot_process_rss", return_value=[]):
            snap = process._describe_active_work(root_pid=1234)
        assert snap.description == "3 subprocess(es) active"
        assert snap.descendant_count == 3
        assert snap.total_rss_mb == 0.0

    def test_populates_rss_totals_and_top_process(self) -> None:
        """Total RSS sums all children; top is the single largest process."""
        pids = [100, 101, 102]
        snapshot = [
            (100, 200.0, "/usr/bin/python3 -m pytest"),
            (101, 50.0, "/usr/local/bin/node"),
            (102, 800.0, "/usr/bin/python3 -m pytest test_heavy.py"),
        ]
        with mock.patch.object(process, "find_all_descendant_pids", return_value=pids), \
             mock.patch.object(process, "snapshot_process_rss", return_value=snapshot):
            snap = process._describe_active_work(root_pid=1234)
        assert snap.descendant_count == 3
        assert snap.total_rss_mb == 1050.0
        assert snap.top_name == "python3"
        assert snap.top_rss_mb == 800.0
        # 'node' is in the IGNORE set, so the description picks up python3 only.
        assert "python3" in snap.description

    def test_ignores_shells_for_description_but_counts_rss(self) -> None:
        """Shells don't show in the description, but their RSS is still counted."""
        pids = [100]
        snapshot = [(100, 5.0, "/bin/zsh")]
        with mock.patch.object(process, "find_all_descendant_pids", return_value=pids), \
             mock.patch.object(process, "snapshot_process_rss", return_value=snapshot):
            snap = process._describe_active_work(root_pid=1234)
        # Description falls back to generic count when only ignored tools present.
        assert snap.description == "1 subprocess(es) active"
        assert snap.total_rss_mb == 5.0
        assert snap.top_name == "zsh"


class TestFormatQuietStatus:
    """Tests for the quiet-period inline status formatter."""

    def test_includes_tree_rss_and_free_memory(self) -> None:
        work = process.ActiveWorkSnapshot(
            description="running pytest",
            descendant_count=3, total_rss_mb=1200.0,
            top_name="python", top_rss_mb=800.0,
        )
        line = process._format_quiet_status("10m29s", work, quiet_seconds=199, free_mb=4123.0)
        assert "[10m29s]" in line
        assert "running pytest" in line
        assert "199s silent" in line
        assert "tree 1200MB" in line
        assert "top python:800MB" in line
        assert "host free 4123MB" in line

    def test_omits_top_when_single_process(self) -> None:
        """A single child makes 'top X:YMB' redundant (it equals the tree total)."""
        work = process.ActiveWorkSnapshot(
            description="running pytest",
            descendant_count=1, total_rss_mb=800.0,
            top_name="python", top_rss_mb=800.0,
        )
        line = process._format_quiet_status("1m00s", work, quiet_seconds=30, free_mb=4000.0)
        assert "tree 800MB" in line
        assert "top python" not in line

    def test_omits_free_memory_when_unavailable(self) -> None:
        """When vm_stat/meminfo fails, we render the line without host memory."""
        work = process.ActiveWorkSnapshot(
            description="running pytest",
            descendant_count=2, total_rss_mb=500.0,
            top_name="python", top_rss_mb=300.0,
        )
        line = process._format_quiet_status("1m00s", work, quiet_seconds=30, free_mb=None)
        assert "host free" not in line
        assert "tree 500MB" in line

    def test_omits_tree_when_empty(self) -> None:
        """When there are no measured children, don't show a zero-MB tree."""
        work = process.ActiveWorkSnapshot(
            description="working", descendant_count=0,
            total_rss_mb=0.0, top_name="", top_rss_mb=0.0,
        )
        line = process._format_quiet_status("0m30s", work, quiet_seconds=20, free_mb=5000.0)
        assert "tree" not in line
        assert "host free 5000MB" in line
