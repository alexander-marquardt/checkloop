"""Checkpoint persistence: save, load, clear, and prompt for resume.

After each completed check, the suite state is saved to a JSON file in the
target project directory.  If checkloop is interrupted, the next run detects
the checkpoint and offers to resume from where it left off.  Writes are
atomic (temp file + ``os.replace``) to avoid corruption on crash.
"""

from __future__ import annotations

import json
import logging
import os
import select
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

from checkloop.terminal import BOLD, CYAN, RESET, YELLOW, print_status

logger = logging.getLogger(__name__)

_CHECKPOINT_FILENAME = ".checkloop-checkpoint.json"
_CHECKPOINT_VERSION = 1
_DEFAULT_RESUME_TIMEOUT = 10  # seconds to wait for user response


class CheckpointData(TypedDict):
    """Schema for the ``.checkloop-checkpoint.json`` file.

    Persisted after each completed check so the suite can resume from
    the exact point of interruption on the next run.

    Attributes:
        version: Checkpoint format version (currently 1).
        started_at: ISO 8601 timestamp when the suite was originally started.
        workdir: Absolute path to the project directory being reviewed.
        check_ids: Full ordered list of selected check IDs for this run.
        num_cycles: Total number of cycles configured.
        convergence_threshold: Percentage threshold for early-stop convergence.
        current_cycle: The cycle number that was in progress (1-based).
        current_check_index: 0-based index of the next check to run within
            *active_check_ids* for the current cycle.
        active_check_ids: Ordered list of check IDs scheduled for the current
            cycle (may differ from *check_ids* if no-op checks were filtered out).
        changed_this_cycle: Check IDs that produced changes so far this cycle.
        previously_changed_ids: Check IDs that made changes in the prior cycle,
            or ``None`` if this is the first cycle.
        prev_change_pct: Percentage of lines changed in the prior cycle, or
            ``None`` if not yet computed.
    """

    version: int
    started_at: str
    workdir: str
    check_ids: list[str]
    num_cycles: int
    convergence_threshold: float
    current_cycle: int
    current_check_index: int
    active_check_ids: list[str]
    changed_this_cycle: list[str]
    previously_changed_ids: list[str] | None
    prev_change_pct: float | None


# --- Path helpers ------------------------------------------------------------

def _checkpoint_path(workdir: str) -> Path:
    return Path(workdir) / _CHECKPOINT_FILENAME


# --- Save / Load / Clear -----------------------------------------------------

def _unlink_quietly(path: str) -> None:
    """Remove a file, ignoring errors if it no longer exists."""
    try:
        os.unlink(path)
    except OSError as exc:
        logger.debug("Failed to remove temp file %s: %s", path, exc)


def save_checkpoint(workdir: str, data: CheckpointData) -> None:
    """Write checkpoint data atomically (temp file + rename)."""
    target = _checkpoint_path(workdir)
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=workdir, prefix=".checkloop-ckpt-", suffix=".tmp",
        )
    except OSError as exc:
        logger.warning("Failed to save checkpoint: %s", exc, exc_info=True)
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, target)
    except (OSError, TypeError, ValueError) as exc:
        _unlink_quietly(tmp_path)
        logger.warning("Failed to save checkpoint: %s", exc, exc_info=True)
        return
    except BaseException:
        _unlink_quietly(tmp_path)
        raise
    logger.debug("Checkpoint saved: cycle=%d, check_index=%d",
                  data["current_cycle"], data["current_check_index"])


def load_checkpoint(workdir: str) -> CheckpointData | None:
    """Load and validate a checkpoint file. Returns None if missing or invalid."""
    path = _checkpoint_path(workdir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Corrupted checkpoint file %s: %s", path, exc, exc_info=True)
        return None

    if not isinstance(data, dict):
        logger.warning("Checkpoint file is not a JSON object: %s", path)
        return None
    if data.get("version") != _CHECKPOINT_VERSION:
        logger.warning("Unsupported checkpoint version %s in %s", data.get("version"), path)
        return None

    required_keys = {
        "version", "started_at", "workdir", "check_ids", "num_cycles",
        "current_cycle", "current_check_index", "active_check_ids",
        "changed_this_cycle",
    }
    missing = required_keys - data.keys()
    if missing:
        logger.warning("Checkpoint missing keys %s: %s", missing, path)
        return None

    if not _has_valid_field_types(data):
        return None

    return cast(CheckpointData, data)


_CHECKPOINT_INT_FIELDS: list[tuple[str, int]] = [
    ("current_cycle", 1),       # (field_name, minimum_value)
    ("current_check_index", 0),
    ("num_cycles", 1),
]
"""Integer fields and their minimum valid values for checkpoint validation."""

_CHECKPOINT_LIST_FIELDS: list[str] = ["check_ids", "active_check_ids", "changed_this_cycle"]
"""Fields that must be lists for checkpoint validation."""


def _is_strict_int(value: object, min_value: int = 0) -> bool:
    """Return True if value is an int (not bool) >= min_value."""
    return not isinstance(value, bool) and isinstance(value, int) and value >= min_value


def _is_strict_number(value: object) -> bool:
    """Return True if value is an int or float (not bool)."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _is_string_list(value: object, *, allow_empty: bool = True) -> bool:
    """Return True if value is a list of strings, optionally non-empty."""
    return (isinstance(value, list)
            and (allow_empty or len(value) > 0)
            and all(isinstance(item, str) for item in value))


def _has_valid_field_types(data: dict[str, object]) -> bool:
    """Check that critical checkpoint fields have the expected types.

    Guards against corrupted or tampered checkpoint files causing
    unexpected control-flow behaviour.  Returns False (and logs a
    warning) on the first invalid field encountered.
    """
    for field_name, min_value in _CHECKPOINT_INT_FIELDS:
        if not _is_strict_int(data.get(field_name), min_value):
            logger.warning("Checkpoint has invalid %s: %r", field_name, data.get(field_name))
            return False

    for field_name in _CHECKPOINT_LIST_FIELDS:
        must_be_nonempty = field_name in ("check_ids", "active_check_ids")
        if not _is_string_list(data.get(field_name), allow_empty=not must_be_nonempty):
            logger.warning("Checkpoint has invalid %s: %r", field_name, data.get(field_name))
            return False

    for field_name in ("workdir", "started_at"):
        if not isinstance(data.get(field_name), str):
            logger.warning("Checkpoint has invalid %s type: %r", field_name, type(data.get(field_name)))
            return False

    if not _is_strict_number(data.get("convergence_threshold")):
        logger.warning("Checkpoint has invalid convergence_threshold: %r", data.get("convergence_threshold"))
        return False

    prev_pct = data.get("prev_change_pct")
    if prev_pct is not None and not _is_strict_number(prev_pct):
        logger.warning("Checkpoint has invalid prev_change_pct: %r", prev_pct)
        return False

    prev_ids = data.get("previously_changed_ids")
    if prev_ids is not None and not _is_string_list(prev_ids):
        logger.warning("Checkpoint has invalid previously_changed_ids: %r", prev_ids)
        return False

    # Cross-field bounds checks — types were already validated above, so we
    # use cast() to inform mypy that the runtime types are narrowed.
    check_index = cast(int, data["current_check_index"])
    active_ids = cast(list[str], data["active_check_ids"])
    if check_index > len(active_ids):
        logger.warning("Checkpoint current_check_index (%d) exceeds active_check_ids length (%d)",
                       check_index, len(active_ids))
        return False

    current_cycle = cast(int, data["current_cycle"])
    num_cycles = cast(int, data["num_cycles"])
    if current_cycle > num_cycles:
        logger.warning("Checkpoint current_cycle (%d) exceeds num_cycles (%d)",
                       current_cycle, num_cycles)
        return False

    return True


def clear_checkpoint(workdir: str) -> None:
    """Delete the checkpoint file if it exists."""
    path = _checkpoint_path(workdir)
    try:
        path.unlink(missing_ok=True)
        logger.debug("Checkpoint cleared: %s", path)
    except OSError as exc:
        logger.warning("Failed to clear checkpoint: %s", exc)


# --- Resume prompt -----------------------------------------------------------

def _format_checkpoint_summary(data: CheckpointData) -> str:
    started = data["started_at"]
    cycle = data["current_cycle"]
    num_cycles = data["num_cycles"]
    check_idx = data["current_check_index"]
    active_ids = data["active_check_ids"]
    total_checks = len(data["check_ids"])
    completed = check_idx
    return (
        f"  Started     : {started}\n"
        f"  Progress    : cycle {cycle}/{num_cycles}, "
        f"check {completed}/{len(active_ids)} completed\n"
        f"  Total checks: {total_checks}\n"
        f"  Next check  : {active_ids[check_idx] if check_idx < len(active_ids) else 'done'}"
    )


def prompt_resume(workdir: str, timeout: int = _DEFAULT_RESUME_TIMEOUT) -> bool:
    """Display checkpoint info and ask whether to resume.

    Returns True to resume, False to start fresh.  If stdin is not a terminal
    or no response is received within *timeout* seconds, defaults to False
    (start fresh).
    """
    data = load_checkpoint(workdir)
    if data is None:
        return False

    print(f"\n{BOLD}{CYAN}Previous incomplete run detected:{RESET}")
    print(_format_checkpoint_summary(data))

    if not sys.stdin.isatty():
        logger.info("Non-interactive session — declining checkpoint resume")
        print_status("Non-interactive session — starting fresh.", YELLOW)
        return False

    print(f"\n  Resume from checkpoint? [y/N] "
          f"(defaulting to N in {timeout}s): ", end="", flush=True)

    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError) as exc:
        logger.warning("select() failed during resume prompt: %s", exc)
        print()
        return False

    if ready:
        try:
            answer = sys.stdin.readline().strip().lower()
        except OSError as exc:
            logger.warning("Failed to read stdin for resume prompt: %s", exc)
            print()
            return False
        resume = answer in ("y", "yes")
        logger.info("User chose to %s from checkpoint", "resume" if resume else "start fresh")
        return resume

    logger.info("No user response within %ds — declining checkpoint resume", timeout)
    print("\n  No response — starting fresh.")
    return False


# --- Checkpoint builder ------------------------------------------------------

def build_checkpoint(
    workdir: str,
    check_ids: list[str],
    num_cycles: int,
    convergence_threshold: float,
    current_cycle: int,
    current_check_index: int,
    active_check_ids: list[str],
    changed_this_cycle: set[str],
    previously_changed_ids: set[str] | None,
    prev_change_pct: float | None,
    started_at: str | None = None,
) -> CheckpointData:
    """Construct a CheckpointData dict from current suite state.

    Args:
        workdir: Absolute path to the project directory being reviewed.
        check_ids: Full ordered list of selected check IDs for this run.
        num_cycles: Total number of cycles configured.
        convergence_threshold: Percentage threshold for early-stop convergence.
        current_cycle: The cycle that is in progress (1-based).
        current_check_index: 0-based index of the next check to run.
        active_check_ids: Ordered check IDs scheduled for the current cycle.
        changed_this_cycle: Check IDs that produced changes so far this cycle.
        previously_changed_ids: Check IDs that made changes in the prior
            cycle, or None if this is the first cycle.
        prev_change_pct: Percentage of lines changed in the prior cycle,
            or None if not yet computed.
        started_at: ISO 8601 timestamp when the suite was originally started.
            Defaults to the current UTC time if not provided.
    """
    return CheckpointData(
        version=_CHECKPOINT_VERSION,
        started_at=started_at or datetime.now(timezone.utc).isoformat(),
        workdir=workdir,
        check_ids=check_ids,
        num_cycles=num_cycles,
        convergence_threshold=convergence_threshold,
        current_cycle=current_cycle,
        current_check_index=current_check_index,
        active_check_ids=active_check_ids,
        changed_this_cycle=sorted(changed_this_cycle),
        previously_changed_ids=sorted(previously_changed_ids) if previously_changed_ids is not None else None,
        prev_change_pct=prev_change_pct,
    )
