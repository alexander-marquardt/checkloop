"""Tests for checkloop.process — streaming output, draining, and stdout reading."""

from __future__ import annotations

import io
import json
import signal
import subprocess
import time
from unittest import mock

import pytest

from checkloop import process


class TestStreamProcessOutput:
    """Tests for _stream_process_output() with mocked subprocesses."""

    def test_process_exits_immediately(self, capsys: pytest.CaptureFixture[str]) -> None:
        mock_proc = mock.MagicMock()
        event = json.dumps({"type": "system", "message": "done"})
        mock_proc.stdout = io.BytesIO(f"{event}\n".encode())
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.return_value = 0
        with mock.patch("select.select", return_value=([mock_proc.stdout], [], [])):
            start, kill_reason = process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
        assert isinstance(start, float)
        assert kill_reason is None
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
        start, kill_reason = process._stream_process_output(mock_proc, idle_timeout=120, debug=False)
        assert isinstance(start, float)
        assert kill_reason is None

    def test_output_buffer_truncated_on_overflow(self) -> None:
        """Buffer is cleared when it exceeds _MAX_BUFFER_SIZE during streaming."""
        mock_proc = mock.MagicMock()
        big_chunk = b"x" * (process._MAX_BUFFER_SIZE + 1)
        mock_stdout = mock.MagicMock()
        mock_stdout.read1 = mock.MagicMock(side_effect=[big_chunk, b""])
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = io.BytesIO(b"")
        mock_proc.poll.side_effect = [None, None, 0]

        with mock.patch("select.select", side_effect=[
            ([mock_stdout], [], []),
            ([mock_stdout], [], []),
        ]):
            with mock.patch.object(process, "process_jsonl_buffer", side_effect=lambda buf, *a: buf):
                process._stream_process_output(mock_proc, idle_timeout=120, debug=False)


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


class TestDrainBufferOverflow:
    """Tests for _drain_remaining_stdout() buffer overflow truncation."""

    def test_drain_truncates_on_overflow(self) -> None:
        mock_stdout = mock.MagicMock()
        mock_stdout.fileno.return_value = 99
        # Return a large chunk that exceeds _MAX_BUFFER_SIZE, then EOF.
        big_chunk = b"x" * (process._MAX_BUFFER_SIZE + 1)
        with mock.patch("os.read", side_effect=[big_chunk, b""]):
            with mock.patch.object(process, "process_jsonl_buffer", side_effect=lambda buf, *a: buf):
                result = process._drain_remaining_stdout(
                    mock_stdout, bytearray(), time.time(), debug=False,
                )
        assert result == bytearray()


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
