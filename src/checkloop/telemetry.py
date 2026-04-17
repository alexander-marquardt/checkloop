"""Continuous telemetry sampler — post-hoc observability for stalls and OOMs.

A background thread snapshots parent RSS, child-tree RSS, system free memory,
swap usage, and the currently running check every ``_SAMPLE_INTERVAL`` seconds.
Each sample is written as a single JSONL line (flushed + fsynced) to
``<workdir>/.checkloop-telemetry/telemetry-YYYY-MM-DD.jsonl``.

Because samples are written out-of-band from the main ``.checkloop-run.log``
(which is rotated per-run and capped at 3 files), telemetry survives across
runs, reboots, and OOM kills.  On startup, files older than ``_RETENTION_DAYS``
are pruned, and if the total directory size exceeds ``_MAX_DIR_BYTES`` the
oldest files are dropped until the budget is met — so the directory cannot
grow without bound.

The sampler also exposes ``get_system_free_mb()`` for callers that need a
cheap, cross-platform read of available system memory without spawning a
``ps`` or reading ``/proc/meminfo`` themselves.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from checkloop import monitoring

logger = logging.getLogger(__name__)


# Sampling cadence.  3s is frequent enough to catch fast-growing allocators
# (each claude subprocess forks ts-server, mypy, pytest, etc. which can
# balloon in seconds) but infrequent enough that the ps/vm_stat overhead
# stays negligible.
_SAMPLE_INTERVAL = 3.0

# Retention for the on-disk telemetry directory.
_RETENTION_DAYS = 14
_MAX_DIR_BYTES = 200 * 1024 * 1024  # 200 MB hard cap

_TELEMETRY_DIRNAME = ".checkloop-telemetry"
_TELEMETRY_FILE_PREFIX = "telemetry-"
_TELEMETRY_FILE_SUFFIX = ".jsonl"

_TOP_CHILDREN_LIMIT = 5


# --- System memory readers ---------------------------------------------------

def _run_cmd(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("telemetry: %s failed: %s", cmd[0], exc)
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _read_macos_memory() -> dict[str, float]:
    """Read system memory on macOS via ``vm_stat`` + ``sysctl``."""
    out: dict[str, float] = {}
    vmstat = _run_cmd(["vm_stat"])
    if vmstat:
        page_size = 4096
        stats: dict[str, int] = {}
        for line in vmstat.splitlines():
            page_match = re.search(r"page size of (\d+) bytes", line)
            if page_match:
                page_size = int(page_match.group(1))
                continue
            kv = re.match(r"^(.+?):\s+(\d+)\.?$", line.strip())
            if kv:
                stats[kv.group(1)] = int(kv.group(2))
        mb = page_size / (1024 * 1024)
        free_pages = stats.get("Pages free", 0) + stats.get("Pages inactive", 0)
        out["system_free_mb"] = round(free_pages * mb, 1)
        out["system_active_mb"] = round(stats.get("Pages active", 0) * mb, 1)
        out["system_wired_mb"] = round(stats.get("Pages wired down", 0) * mb, 1)
        out["system_compressed_mb"] = round(stats.get("Pages occupied by compressor", 0) * mb, 1)

    total = _run_cmd(["sysctl", "-n", "hw.memsize"])
    if total and total.strip().isdigit():
        out["system_total_mb"] = round(int(total.strip()) / (1024 * 1024), 1)

    swap = _run_cmd(["sysctl", "-n", "vm.swapusage"])
    if swap:
        # Example: "total = 4096.00M  used = 1337.25M  free = 2758.75M  (encrypted)"
        used_match = re.search(r"used\s*=\s*([\d.]+)M", swap)
        total_match = re.search(r"total\s*=\s*([\d.]+)M", swap)
        if used_match:
            out["swap_used_mb"] = float(used_match.group(1))
        if total_match:
            out["swap_total_mb"] = float(total_match.group(1))
    return out


def _read_linux_memory() -> dict[str, float]:
    """Read system memory on Linux from ``/proc/meminfo``."""
    out: dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 2:
                    continue
                key = parts[0].rstrip(":")
                try:
                    val_kb = int(parts[1])
                except ValueError:
                    continue
                if key == "MemTotal":
                    out["system_total_mb"] = round(val_kb / 1024, 1)
                elif key == "MemAvailable":
                    out["system_free_mb"] = round(val_kb / 1024, 1)
                elif key == "SwapTotal":
                    out["swap_total_mb"] = round(val_kb / 1024, 1)
                elif key == "SwapFree":
                    out["swap_free_mb"] = round(val_kb / 1024, 1)
    except OSError as exc:
        logger.debug("telemetry: /proc/meminfo read failed: %s", exc)
    if "swap_total_mb" in out and "swap_free_mb" in out:
        out["swap_used_mb"] = round(out["swap_total_mb"] - out["swap_free_mb"], 1)
    return out


def read_system_memory() -> dict[str, float]:
    """Return a dict of system memory metrics in MB (may be empty on failure)."""
    if sys.platform == "darwin":
        return _read_macos_memory()
    return _read_linux_memory()


def get_system_free_mb() -> float | None:
    """Return current system free memory in MB, or None if unavailable.

    Used by the pressure-kill path in ``process._check_memory_limit``.  Returns
    ``None`` (not 0.0) on failure so callers can distinguish "measurement
    unavailable" from "genuinely zero memory free".
    """
    mem = read_system_memory()
    if "system_free_mb" in mem:
        return float(mem["system_free_mb"])
    return None


# --- Sampler state -----------------------------------------------------------

class _SamplerState:
    """Module-level singleton, wrapped so tests can reset cleanly."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.file_handle: IO[str] | None = None
        self.file_path: Path | None = None
        self.run_id: str = ""
        self.current_label: str = "startup"
        self.write_lock = threading.Lock()


_state = _SamplerState()


def set_current_label(label: str) -> None:
    """Update the ``label`` field attached to every subsequent telemetry sample.

    Called from the check runner when a check starts/ends so telemetry lines
    can be correlated to the activity that produced them.
    """
    _state.current_label = label


# --- Sample collection -------------------------------------------------------

def _collect_sample() -> dict[str, object]:
    """Collect one telemetry snapshot.  Must never raise."""
    sample: dict[str, object] = {
        "t": round(time.time(), 3),
        "iso": datetime.now().isoformat(timespec="seconds"),
        "run_id": _state.run_id,
        "pid": os.getpid(),
        "label": _state.current_label,
    }

    try:
        sample["parent_rss_mb"] = round(monitoring._measure_current_rss_mb(), 1)
    except Exception as exc:  # pragma: no cover — defensive
        sample["parent_err"] = str(exc)

    try:
        child_pids = monitoring._find_child_pids()
        sample["child_count"] = len(child_pids)
        if child_pids:
            entries = monitoring.snapshot_process_rss(set(child_pids))
            sample["children_rss_mb"] = round(sum(rss for _, rss, _ in entries), 1)
            top = sorted(entries, key=lambda e: -e[1])[:_TOP_CHILDREN_LIMIT]
            sample["top_children"] = [
                {"pid": p, "rss_mb": round(r, 1), "cmd": c} for p, r, c in top
            ]
        else:
            sample["children_rss_mb"] = 0.0
    except Exception as exc:  # pragma: no cover — defensive
        sample["children_err"] = str(exc)

    # Residual RSS across tracked sessions/descendants that escaped direct parent-child tracking.
    try:
        session_rss = sum(
            monitoring.measure_session_rss_mb(sid)
            for sid in monitoring.previous_session_ids
        )
        descendant_rss = (
            monitoring.measure_pid_rss_mb(monitoring.previous_descendant_pids)
            if monitoring.previous_descendant_pids else 0.0
        )
        sample["session_rss_mb"] = round(session_rss, 1)
        sample["descendant_rss_mb"] = round(descendant_rss, 1)
        sample["tracked_sessions"] = len(monitoring.previous_session_ids)
        sample["tracked_descendants"] = len(monitoring.previous_descendant_pids)
    except Exception:  # pragma: no cover
        pass

    try:
        sample.update(read_system_memory())
    except Exception as exc:  # pragma: no cover
        sample["sysmem_err"] = str(exc)

    return sample


def _write_sample(sample: dict[str, object]) -> None:
    """Serialise + flush one sample.  Holds the write lock so event markers
    emitted from the main thread don't interleave with the sampler."""
    fh = _state.file_handle
    if fh is None:
        return
    line = json.dumps(sample, default=str) + "\n"
    with _state.write_lock:
        try:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        except (OSError, ValueError) as exc:
            logger.debug("telemetry write failed: %s", exc)


def _sampler_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _write_sample(_collect_sample())
        except Exception as exc:  # pragma: no cover — sampler must never die
            logger.debug("telemetry sampler iteration failed: %s", exc)
        stop_event.wait(_SAMPLE_INTERVAL)


# --- Retention / pruning -----------------------------------------------------

def _list_telemetry_files(dir_path: Path) -> list[tuple[Path, os.stat_result]]:
    entries: list[tuple[Path, os.stat_result]] = []
    # iterdir() is a generator — the underlying listdir() only fires during
    # iteration, so the try/except must wrap the for-loop itself.
    try:
        for p in dir_path.iterdir():
            if not p.name.startswith(_TELEMETRY_FILE_PREFIX):
                continue
            if not p.name.endswith(_TELEMETRY_FILE_SUFFIX):
                continue
            try:
                entries.append((p, p.stat()))
            except OSError:
                continue
    except OSError:
        return entries
    return entries


def prune_old_telemetry(
    dir_path: Path,
    retention_days: int = _RETENTION_DAYS,
    max_dir_bytes: int = _MAX_DIR_BYTES,
) -> int:
    """Delete telemetry files older than *retention_days* and cap total size.

    Returns the number of files deleted.  Parameters are explicit (rather
    than only reading the module constants) so tests can exercise retention
    without sleeping or creating gigabytes of data.
    """
    deleted = 0
    cutoff = time.time() - (retention_days * 86400)
    files = _list_telemetry_files(dir_path)
    survivors: list[tuple[Path, os.stat_result]] = []
    for path, st in files:
        if st.st_mtime < cutoff:
            try:
                path.unlink()
                deleted += 1
                logger.debug("telemetry: pruned old %s (mtime=%.0f)", path.name, st.st_mtime)
            except OSError as exc:
                logger.debug("telemetry: could not unlink %s: %s", path.name, exc)
        else:
            survivors.append((path, st))

    total = sum(st.st_size for _, st in survivors)
    if total > max_dir_bytes:
        survivors.sort(key=lambda e: e[1].st_mtime)  # oldest first
        while total > max_dir_bytes and survivors:
            path, st = survivors.pop(0)
            try:
                path.unlink()
                total -= st.st_size
                deleted += 1
                logger.debug("telemetry: pruned over-cap %s (size=%d)", path.name, st.st_size)
            except OSError as exc:
                logger.debug("telemetry: could not unlink %s: %s", path.name, exc)
    return deleted


# --- Lifecycle ---------------------------------------------------------------

def _telemetry_path_for_today(dir_path: Path) -> Path:
    fname = f"{_TELEMETRY_FILE_PREFIX}{datetime.now().strftime('%Y-%m-%d')}{_TELEMETRY_FILE_SUFFIX}"
    return dir_path / fname


def start(workdir: str, run_id: str) -> None:
    """Start the background telemetry sampler for this run.

    Safe to call more than once — subsequent calls are no-ops.  Failures
    (cannot create dir, cannot open file) are logged and swallowed so
    telemetry never prevents checkloop itself from starting.
    """
    if _state.thread is not None:
        return

    dir_path = Path(workdir) / _TELEMETRY_DIRNAME
    try:
        dir_path.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        logger.warning("telemetry: cannot create %s: %s", dir_path, exc)
        return

    try:
        prune_old_telemetry(dir_path)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("telemetry: pruning failed: %s", exc)

    file_path = _telemetry_path_for_today(dir_path)
    try:
        file_handle = open(file_path, "a", encoding="utf-8")
    except OSError as exc:
        logger.warning("telemetry: cannot open %s: %s", file_path, exc)
        return
    try:
        os.chmod(file_path, 0o600)
    except OSError:
        pass

    _state.file_handle = file_handle
    _state.file_path = file_path
    _state.run_id = run_id
    _state.current_label = "startup"
    stop_event = threading.Event()
    _state.stop_event = stop_event

    # Write a synchronous run-start marker so the file begins with a record
    # anchored to wall-clock time — useful when the sampler thread is killed
    # by a stall before its first tick.
    try:
        marker = _collect_sample()
        marker["event"] = "run_start"
        _write_sample(marker)
    except Exception:  # pragma: no cover
        pass

    thread = threading.Thread(
        target=_sampler_loop,
        args=(stop_event,),
        daemon=True,
        name="checkloop-telemetry",
    )
    _state.thread = thread
    thread.start()
    logger.info(
        "Telemetry started: %s (interval=%.1fs, retention=%dd)",
        file_path, _SAMPLE_INTERVAL, _RETENTION_DAYS,
    )


def stop(event: str = "run_end") -> None:
    """Stop the sampler thread and close the telemetry file.

    Writes a terminal marker so post-mortem analysis can distinguish a clean
    exit from a stall (where the last line is a regular sample, not an
    end marker).  Safe to call when no sampler is running.
    """
    stop_event = _state.stop_event
    thread = _state.thread
    fh = _state.file_handle

    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)

    if fh is not None:
        try:
            marker = _collect_sample()
            marker["event"] = event
            _write_sample(marker)
        except Exception:  # pragma: no cover
            pass
        try:
            fh.close()
        except OSError:
            pass

    _state.thread = None
    _state.stop_event = None
    _state.file_handle = None
    _state.file_path = None


def record_event(event: str, **fields: object) -> None:
    """Write an out-of-band marker line (e.g. check_start, kill).

    Used by check_runner and process.py to annotate the timeline with
    specific events, independent of the periodic sampling cadence.
    Safe to call before ``start()`` or after ``stop()`` — silently no-ops.
    """
    if _state.file_handle is None:
        return
    try:
        sample = _collect_sample()
        sample["event"] = event
        sample.update(fields)
        _write_sample(sample)
    except Exception:  # pragma: no cover — must never raise
        pass
