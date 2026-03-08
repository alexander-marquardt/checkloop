"""Tests for checkloop.process — subprocess management and run_claude."""

from __future__ import annotations

import io
import json
import signal
import subprocess
import time
from unittest import mock

import pytest

from checkloop import process


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


class TestRunClaude:
    """Tests for the public run_claude() function."""

    def test_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = process.run_claude("prompt", "/tmp", dry_run=True)
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

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=time.time()):
                code = process.run_claude("prompt", "/tmp", dry_run=False)
        assert code == 0

    def test_dry_run_short_prompt_no_ellipsis(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = process.run_claude("short", "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "short" in out
        assert "short..." not in out

    def test_dry_run_long_prompt_truncated(self, capsys: pytest.CaptureFixture[str]) -> None:
        long_prompt = "x" * 200
        code = process.run_claude(long_prompt, "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "..." in out

    def test_dry_run_empty_prompt(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = process.run_claude("", "/tmp", dry_run=True)
        assert code == 0

    def test_nonzero_exit(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 1

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=time.time()):
                mock_proc.wait.return_value = None
                code = process.run_claude("prompt", "/tmp", dry_run=False)
        assert code == 1
        out = capsys.readouterr().out
        assert "exited with code 1" in out

    def test_whitespace_only_prompt_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = process.run_claude("   \t\n  ", "/tmp", dry_run=True)
        assert code == 0

    def test_unicode_prompt_dry_run(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = process.run_claude("レビューコード 🔍 review", "/tmp", dry_run=True)
        assert code == 0
        out = capsys.readouterr().out
        assert "レビューコード" in out


class TestRunClaudeReturnCodeNone:
    """Test run_claude when process.returncode is None."""

    def test_returncode_none_returns_negative_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 9999
        mock_proc.returncode = None
        mock_proc.wait.return_value = None

        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", return_value=time.time()):
                with mock.patch.object(process, "_kill_process_group"):
                    with mock.patch.object(process, "_log_memory_usage"):
                        code = process.run_claude("test", "/tmp")
        assert code == -1
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
            with mock.patch.object(process, "_stream_process_output", return_value=time.time()):
                with mock.patch.object(process, "_kill_process_group") as mock_kill:
                    process.run_claude("test prompt", "/tmp", idle_timeout=1)
                    assert mock_kill.call_count == 1


class TestExecuteClaudeProcessCleanup:
    """Test that _execute_claude_process kills the process on streaming errors."""

    def test_stream_error_kills_process(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.pid = 12345
        with mock.patch.object(process, "_spawn_claude_process", return_value=mock_proc):
            with mock.patch.object(process, "_stream_process_output", side_effect=RuntimeError("boom")):
                with mock.patch.object(process, "_kill_process_group") as kill_mock:
                    with pytest.raises(RuntimeError, match="boom"):
                        process._execute_claude_process(["claude"], "/tmp", 120, False)
                    kill_mock.assert_called_with(mock_proc)


class TestStreamProcessOutput:
    """Tests for _stream_process_output() with mocked subprocesses."""

    def test_process_exits_immediately(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "done"})
        mock_proc.stdout = io.BytesIO(f"{event}\n".encode())
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = 0
        with mock.patch("select.select", return_value=([mock_proc.stdout], [], [])):
            start = process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
        assert isinstance(start, float)
        out = capsys.readouterr().out
        assert "done" in out

    def test_idle_timeout_kills(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.stdout = mock.MagicMock()
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        with mock.patch("select.select", return_value=([], [], [])):
            with mock.patch("time.time") as mock_time:
                mock_time.side_effect = [
                    100.0,
                    100.0,
                    250.0,
                ] + [250.0] * 10
                mock_proc.stdout.read.return_value = b""
                with mock.patch("os.getpgid", return_value=12345):
                    with mock.patch("os.killpg"):
                        process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
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
            process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
        mock_stdout.read1.assert_called()

    def test_os_read_fallback(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "yo"})
        data = f"{event}\n".encode()

        mock_stdout = mock.MagicMock(spec=["fileno", "read", "close"])
        mock_stdout.fileno.return_value = 99
        mock_stdout.read.return_value = b""
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.side_effect = [None, 0]

        with mock.patch("select.select", return_value=([mock_stdout], [], [])):
            with mock.patch("os.read", side_effect=[data, b""]):
                process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
        out = capsys.readouterr().out
        assert "yo" in out

    def test_process_exits_while_not_ready(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "final"})
        data = f"{event}\n".encode()

        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99
        mock_stdout.read.return_value = b""
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.side_effect = [None, 0]

        with mock.patch("select.select", return_value=([], [], [])):
            with mock.patch("os.read", side_effect=[data, b""]):
                process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
        out = capsys.readouterr().out
        assert "final" in out

    def test_stdout_none_returns_time(self) -> None:
        mock_proc = mock.MagicMock()
        mock_proc.stdout = None
        mock_proc.pid = 12345
        result = process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
        assert isinstance(result, float)


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
                process._stream_process_output(mock_proc, idle_timeout=120, debug=False)

    def test_stdout_close_oserror_handled(self) -> None:
        mock_proc = mock.MagicMock()
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 5
        mock_stdout.read1 = None
        mock_proc.stdout = mock_stdout
        mock_proc.pid = 9999
        mock_proc.poll.return_value = 0

        with mock.patch("select.select", return_value=([], [], [])):
            with mock.patch("os.read", return_value=b""):
                mock_stdout.close.side_effect = OSError("close failed")
                process._stream_process_output(mock_proc, idle_timeout=120, debug=False)


class TestDrainRemainingStdout:
    """Tests for _drain_remaining_stdout() post-exit data collection."""

    def test_drains_data(self, capsys: pytest.CaptureFixture[str]) -> None:
        event = json.dumps({"type": "system", "message": "leftover"})
        data = f"{event}\n".encode()

        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99

        with mock.patch("os.read", side_effect=[data, b""]):
            result = process._drain_remaining_stdout(
                mock_stdout, bytearray(), time.time(), debug=False,
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
            process._drain_remaining_stdout(
                mock_stdout, bytearray(), time.time(), debug=False,
            )
        out = capsys.readouterr().out
        assert "chunk1" in out
        assert "chunk2" in out

    def test_empty_stdout(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99

        with mock.patch("os.read", return_value=b""):
            result = process._drain_remaining_stdout(
                mock_stdout, bytearray(), time.time(), debug=False,
            )
        assert result == bytearray()

    def test_oserror_returns_buffer_unchanged(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.side_effect = OSError("bad fd")
        buf = bytearray(b"existing")
        result = process._drain_remaining_stdout(mock_stdout, buf, time.time(), False)
        assert result == bytearray(b"existing")


class TestReadStdoutChunk:
    """Tests for _read_stdout_chunk() read strategy selection."""

    def test_uses_read1_when_available(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.read1.return_value = b"data"
        result = process._read_stdout_chunk(mock_stdout)
        assert result == b"data"
        mock_stdout.read1.assert_called_once_with(process._READ_CHUNK_SIZE)

    def test_falls_back_to_os_read(self) -> None:
        mock_stdout = mock.MagicMock(spec=["fileno"])
        mock_stdout.fileno.return_value = 42
        with mock.patch("os.read", return_value=b"fallback"):
            result = process._read_stdout_chunk(mock_stdout)
        assert result == b"fallback"

    def test_oserror_returns_empty_bytes(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.read1 = mock.MagicMock(side_effect=OSError("broken pipe"))
        result = process._read_stdout_chunk(mock_stdout)
        assert result == b""


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


class TestMeasureCurrentRssMb:
    """Tests for _measure_current_rss_mb() memory measurement."""

    def test_returns_positive_value(self) -> None:
        rss = process._measure_current_rss_mb()
        assert rss > 0

    def test_fallback_on_ps_failure(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            rss = process._measure_current_rss_mb()
            assert rss > 0

    def test_oserror_falls_back_to_resource(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            rss = process._measure_current_rss_mb()
            assert rss > 0

    def test_valueerror_falls_back_to_resource(self) -> None:
        with mock.patch("subprocess.run", side_effect=ValueError("bad")):
            rss = process._measure_current_rss_mb()
            assert rss > 0

    def test_getrusage_oserror_returns_zero(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            with mock.patch("resource.getrusage", side_effect=OSError("no resource")):
                rss = process._measure_current_rss_mb()
                assert rss == 0.0

    def test_ps_returns_non_numeric(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="not_a_number\n")
        with mock.patch("subprocess.run", return_value=mock_result):
            result = process._measure_current_rss_mb()
            assert isinstance(result, float)

    def test_ps_fails(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no ps")):
            result = process._measure_current_rss_mb()
            assert isinstance(result, float)

    def test_multiline_ps_output(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="12345\n67890\n")
        with mock.patch("subprocess.run", return_value=mock_result):
            rss = process._measure_current_rss_mb()
            assert abs(rss - 12345 / 1024) < 0.01

    def test_ps_output_with_leading_whitespace(self) -> None:
        mock_result = mock.MagicMock(returncode=0, stdout="  54321  \n")
        with mock.patch("subprocess.run", return_value=mock_result):
            rss = process._measure_current_rss_mb()
            assert abs(rss - 54321 / 1024) < 0.01


class TestFindChildPids:
    """Tests for _find_child_pids()."""

    def test_returns_empty_when_no_children(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            assert process._find_child_pids() == []

    def test_returns_pids(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="123\n456\n")):
            assert process._find_child_pids() == [123, 456]

    def test_oserror_returns_empty(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("nope")):
            assert process._find_child_pids() == []

    def test_invalid_pid_lines_skipped(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(
            returncode=0, stdout="123\nnot_a_number\n456\n"
        )):
            assert process._find_child_pids() == [123, 456]


class TestKillOrphanedChildren:
    """Tests for _kill_orphaned_children()."""

    def test_returns_zero_when_none(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=1, stdout="")):
            assert process._kill_orphaned_children() == 0

    def test_kills_found_children(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="999\n")):
            with mock.patch("os.kill") as mock_kill:
                killed = process._kill_orphaned_children()
                assert killed == 1
                mock_kill.assert_called_once_with(999, signal.SIGKILL)

    def test_handles_already_dead_child(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.MagicMock(returncode=0, stdout="999\n")):
            with mock.patch("os.kill", side_effect=OSError("No such process")):
                killed = process._kill_orphaned_children()
                assert killed == 0


class TestLogMemoryUsage:
    """Tests for _log_memory_usage() reporting."""

    def test_prints_memory_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(process, "_measure_current_rss_mb", return_value=42.0):
            with mock.patch.object(process, "_find_child_pids", return_value=[]):
                process._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "42MB" in out
        assert "0 child" in out

    def test_kills_orphans_when_found(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(process, "_measure_current_rss_mb", return_value=100.0):
            with mock.patch.object(process, "_find_child_pids", return_value=[123, 456]):
                with mock.patch.object(process, "_kill_orphaned_children", return_value=2):
                    process._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "2 child" in out
        assert "Warning" in out
        assert "Killed 2" in out

    def test_orphans_found_but_none_killed(self, capsys: pytest.CaptureFixture[str]) -> None:
        with mock.patch.object(process, "_measure_current_rss_mb", return_value=50.0):
            with mock.patch.object(process, "_find_child_pids", return_value=[999]):
                with mock.patch.object(process, "_kill_orphaned_children", return_value=0):
                    process._log_memory_usage("test-label")
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "Killed" not in out
