"""Tests for checkloop.power — macOS power-assertion management."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from checkloop import power


class TestPreventIdleSleep:
    """Tests for prevent_idle_sleep() — caffeinate spawning and platform gating."""

    def test_non_macos_returns_none(self) -> None:
        with mock.patch.object(power.sys, "platform", "linux"):
            with mock.patch.object(power.subprocess, "Popen") as mock_popen:
                assert power.prevent_idle_sleep() is None
                mock_popen.assert_not_called()

    def test_missing_caffeinate_returns_none_and_warns(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        with mock.patch.object(power.sys, "platform", "darwin"):
            with mock.patch.object(power.shutil, "which", return_value=None):
                with mock.patch.object(power.subprocess, "Popen") as mock_popen:
                    assert power.prevent_idle_sleep() is None
                    mock_popen.assert_not_called()
        assert "caffeinate not found" in capsys.readouterr().out

    def test_spawns_caffeinate_bound_to_pid(self) -> None:
        fake_proc = mock.MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 4321
        with mock.patch.object(power.sys, "platform", "darwin"):
            with mock.patch.object(power.shutil, "which", return_value="/usr/bin/caffeinate"):
                with mock.patch.object(power.os, "getpid", return_value=1234):
                    with mock.patch.object(power.subprocess, "Popen", return_value=fake_proc) as mock_popen:
                        with mock.patch.object(power.atexit, "register") as mock_register:
                            result = power.prevent_idle_sleep()
        assert result is fake_proc
        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == "/usr/bin/caffeinate"
        assert "-i" in cmd
        assert cmd[-2:] == ["-w", "1234"]
        mock_register.assert_called_once_with(power._release, fake_proc)

    def test_popen_oserror_returns_none(self) -> None:
        with mock.patch.object(power.sys, "platform", "darwin"):
            with mock.patch.object(power.shutil, "which", return_value="/usr/bin/caffeinate"):
                with mock.patch.object(power.subprocess, "Popen", side_effect=OSError("boom")):
                    assert power.prevent_idle_sleep() is None


class TestRelease:
    """Tests for _release() — caffeinate teardown."""

    def test_terminates_running_process(self) -> None:
        proc = mock.MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        power._release(proc)
        proc.terminate.assert_called_once()

    def test_skips_already_exited_process(self) -> None:
        proc = mock.MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = 0
        power._release(proc)
        proc.terminate.assert_not_called()

    def test_oserror_on_terminate_is_swallowed(self) -> None:
        proc = mock.MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = None
        proc.terminate.side_effect = OSError("already gone")
        power._release(proc)  # must not raise
