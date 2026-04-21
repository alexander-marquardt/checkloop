"""Run-scoped storage under ``~/checkloop-runs/``.

Each checkloop run lives in its own timestamped directory under the runs root.
In the default (clone) mode this directory *is* a full local clone of the
target repo with all checkloop commits, debug artifacts, and recommendations
inside it — disposable, zero interference with the user's working tree, and a
hard backup of the repo state at the moment the run began.  In ``--in-place``
mode the directory only holds debug artifacts; the target repo is edited
directly.

Directory layout (clone mode)::

    ~/checkloop-runs/
        <target-basename>-<iso-timestamp>/
            <full clone of the target repo>
            .checkloop-run.log
            .checkloop-telemetry/telemetry-YYYY-MM-DD.jsonl
            .checkloop-logs/<check-id>_cycle<N>.jsonl

Runs older than ``_DEFAULT_MAX_AGE_DAYS`` are pruned at startup.  The root
can be overridden with the ``CHECKLOOP_STATE_HOME`` environment variable —
useful for tests.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_DAYS = 14
_RUNS_DIRNAME = "checkloop-runs"

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def get_runs_root() -> Path:
    """Return the root directory under which per-run dirs are created."""
    override = os.environ.get("CHECKLOOP_STATE_HOME")
    if override:
        return Path(override) / _RUNS_DIRNAME
    return Path.home() / _RUNS_DIRNAME


def iso_timestamp() -> str:
    """UTC ISO-8601 timestamp with ``:`` replaced by ``-`` (branch- and filename-safe)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _sanitize_basename(name: str) -> str:
    cleaned = _SANITIZE_RE.sub("-", name).strip("-")
    return cleaned or "target"


def create_run_dir(workdir: str, *, timestamp: str | None = None) -> Path:
    """Create and return a new run-scoped debug directory.

    Directory name: ``<target-basename>-<iso-ts>`` under ``get_runs_root()``.
    Failures (permission denied, read-only home) are logged and the directory
    path is returned anyway — callers must tolerate a non-existent dir since
    downstream writers (telemetry, logging, check_runner) already log-and-skip
    on OSError.
    """
    ts = timestamp or iso_timestamp()
    base = _sanitize_basename(Path(workdir).name)
    root = get_runs_root()
    run_dir = root / f"{base}-{ts}"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        logger.info("Run debug dir: %s", run_dir)
    except OSError as exc:
        logger.warning("Could not create run dir %s: %s", run_dir, exc)
    return run_dir


def prune_old_runs(max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> int:
    """Remove run directories whose mtime is older than ``max_age_days``.

    Returns the number of directories removed.  Errors on individual entries
    are swallowed so pruning never prevents a new run from starting.
    """
    root = get_runs_root()
    if not root.exists():
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).timestamp()
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        logger.debug("Could not list runs root %s: %s", root, exc)
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
            logger.debug("Pruned old run dir: %s", entry.name)
        except OSError as exc:
            logger.debug("Could not remove %s: %s", entry, exc)
    if removed:
        logger.info("Pruned %d run dir(s) older than %d days", removed, max_age_days)
    return removed
