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
        with mock.patch("time.time", return_value=221.0):
            result = process._check_idle_timeout(
                last_output_time=100.0, idle_timeout=120,
                check_start_time=80.0, process=mock_proc,
            )
        assert result is True


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
            assert call_kwargs.kwargs["stdin"] == subprocess.DEVNULL

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

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=(time.time(), None)):
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

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=(time.time(), None)):
                mock_proc.wait.return_value = None
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

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output",
                                   return_value=(time.time(), process.KILL_REASON_MEMORY)):
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
                    kill_mock.assert_called_with(mock_proc)


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
        exceeded, _ = process._check_memory_limit(
            session_id=123, max_memory_mb=0,
            check_start_time=time.time(), process=mock.MagicMock(),
            last_memory_check=time.time(),
        )
        assert exceeded is False

    def test_skipped_before_interval(self) -> None:
        now = time.time()
        exceeded, last = process._check_memory_limit(
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
            exceeded, last = process._check_memory_limit(
                session_id=123, max_memory_mb=4096,
                check_start_time=now - 60, process=mock.MagicMock(),
                last_memory_check=old_check,
            )
        assert exceeded is False
        assert last > old_check  # updated

    def test_over_limit_kills_process(self) -> None:
        now = time.time()
        mock_proc = mock.MagicMock()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=5000.0):
            with mock.patch.object(process, "_kill_process_group") as mock_kill:
                exceeded, _ = process._check_memory_limit(
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
             mock.patch.object(process, "_check_memory_limit", return_value=(False, time.time())):
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
             mock.patch.object(process, "_check_memory_limit", return_value=(True, time.time())):
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
    """Tests for _check_resource_limits() which combines idle, hard timeout, and memory checks."""

    def _make_mock_process(self) -> mock.MagicMock:
        proc = mock.MagicMock()
        proc.pid = 12345
        return proc

    def test_no_limits_exceeded(self) -> None:
        """When all limits are within bounds, returns None kill_reason."""
        proc = self._make_mock_process()
        now = time.time()
        kill_reason, last_mem = process._check_resource_limits(
            proc, check_start_time=now, last_output_time=now,
            idle_timeout=300, check_timeout=0, max_memory_mb=0,
            last_memory_check=now,
        )
        assert kill_reason is None

    def test_idle_timeout_detected_first(self) -> None:
        """Idle timeout should be detected before hard timeout or memory."""
        proc = self._make_mock_process()
        now = time.time()
        kill_reason, _ = process._check_resource_limits(
            proc, check_start_time=now - 10, last_output_time=now - 400,
            idle_timeout=300, check_timeout=600, max_memory_mb=8192,
            last_memory_check=now,
        )
        assert kill_reason == process.KILL_REASON_IDLE

    def test_hard_timeout_when_not_idle(self) -> None:
        """Hard timeout should be detected when idle timeout hasn't triggered."""
        proc = self._make_mock_process()
        now = time.time()
        kill_reason, _ = process._check_resource_limits(
            proc, check_start_time=now - 700, last_output_time=now,
            idle_timeout=300, check_timeout=600, max_memory_mb=0,
            last_memory_check=now,
        )
        assert kill_reason == process.KILL_REASON_TIMEOUT

    def test_memory_limit_when_no_timeout(self) -> None:
        """Memory limit should be detected when timeouts haven't triggered."""
        proc = self._make_mock_process()
        now = time.time()
        with mock.patch.object(process, "measure_session_rss_mb", return_value=10000.0):
            kill_reason, _ = process._check_resource_limits(
                proc, check_start_time=now, last_output_time=now,
                idle_timeout=300, check_timeout=0, max_memory_mb=8192,
                last_memory_check=now - 20,  # past the check interval
            )
        assert kill_reason == process.KILL_REASON_MEMORY
