"""Keep the host awake for the duration of a run.

A checkloop run is unattended agent work that routinely takes hours. On a
laptop left idle, macOS will idle-sleep mid-check; the Claude subprocess is
suspended, no JSONL is produced, and the run looks stalled until the machine
is woken again. ``caffeinate`` holds a power assertion that blocks idle
sleep. Binding it to checkloop's PID with ``-w`` means the assertion is
released automatically when checkloop exits — including on a crash or kill —
so there is no lingering assertion to clean up.

This is macOS-only. On other platforms the functions here are no-ops; the
Linux equivalent (``systemd-inhibit``) is intentionally not wired up because
no Linux idle-sleep stall has been reported against checkloop.
"""

from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import sys

from checkloop.terminal import BOLD, RESET, YELLOW

logger = logging.getLogger(__name__)

# -i: block idle system sleep.
# -m: block disk idle sleep.
# -s: block system sleep (honoured only while on AC power).
# Display sleep is deliberately not blocked — a dark screen does not suspend
# the run, and keeping the panel lit overnight serves no purpose.
_CAFFEINATE_FLAGS = ["-i", "-m", "-s"]


def prevent_idle_sleep() -> subprocess.Popen[bytes] | None:
    """Spawn ``caffeinate`` bound to this process so the host stays awake.

    Returns the ``caffeinate`` handle, or ``None`` when the platform is not
    macOS or the binary is unavailable. ``caffeinate -w <pid>`` exits on its
    own once checkloop's PID goes away, so callers do not need to manage the
    handle's lifetime; it is also terminated at interpreter exit as a
    belt-and-braces cleanup.
    """
    if sys.platform != "darwin":
        logger.debug("Not macOS — skipping caffeinate power assertion")
        return None
    caffeinate = shutil.which("caffeinate")
    if caffeinate is None:
        logger.warning("caffeinate not found on PATH — power assertion disabled")
        print(
            f"\n{YELLOW}{BOLD}Note: caffeinate not found on PATH.{RESET}\n"
            f"{YELLOW}  checkloop cannot hold a macOS power assertion, so the host may\n"
            f"  idle-sleep during a long run and suspend the active check until the\n"
            f"  machine is woken. caffeinate normally ships at /usr/bin/caffeinate;\n"
            f"  if it is missing, adjust System Settings to keep the Mac awake, or\n"
            f"  start the run yourself with: caffeinate -is checkloop ...{RESET}",
        )
        return None
    cmd = [caffeinate, *_CAFFEINATE_FLAGS, "-w", str(os.getpid())]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning(
            "Could not start caffeinate (%s) — the host may idle-sleep during a long run.",
            exc,
        )
        return None
    logger.info("Holding macOS power assertion via caffeinate (pid=%d)", proc.pid)
    atexit.register(_release, proc)
    return proc


def _release(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the caffeinate process if it is still running."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except OSError:
        pass
