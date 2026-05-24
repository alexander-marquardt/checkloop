"""Reject runs whose review base is too old to be worth reviewing against.

The motivating failure mode: a checkloop run started against a base commit that
is days behind the upstream branch produces extractions and refactors which the
human reviewer then has to manually re-apply against current HEAD because the
files have moved since.  The ``--require-base-fresh DURATION`` flag draws a
line — if the base is older than the configured threshold, the run refuses to
start and tells the operator to either rebase or pass ``ignore`` to bypass.

Public surface:

* :func:`parse_duration` — parse ``"30m"`` / ``"12h"`` / ``"1d"`` / ``"1w"`` into seconds.
* :func:`enforce_base_freshness` — exit the process if the base is too old.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

from checkloop.terminal import fatal

logger = logging.getLogger(__name__)

# Duration strings require an explicit suffix so the unit is unambiguous.  A
# bare integer is rejected — past projects that accepted bare integers as
# "minutes by default" produced operator confusion when the same string was
# treated as seconds elsewhere in the codebase.
_DURATION_RE = re.compile(r"^(\d+)([mhdw])$")
_DURATION_MULTIPLIERS = {
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

# Magic string the user can pass to --require-base-fresh to disable the check
# without removing the flag from a wrapper script.
IGNORE_TOKEN = "ignore"


class FreshnessParseError(ValueError):
    """Raised by :func:`parse_duration` on a malformed duration string."""


def parse_duration(s: str) -> int:
    """Parse a duration string into a number of seconds.

    Accepted forms (suffix is required):

    * ``"30m"`` — 30 minutes
    * ``"12h"`` — 12 hours
    * ``"1d"``  — 1 day
    * ``"1w"``  — 1 week

    Raises :class:`FreshnessParseError` on any other input.  The IGNORE_TOKEN
    is the caller's responsibility to handle — this function only parses real
    duration strings.
    """
    match = _DURATION_RE.match(s.strip())
    if not match:
        raise FreshnessParseError(
            f"could not parse duration {s!r}. "
            "Expected forms: 30m (minutes), 12h (hours), 1d (days), 1w (weeks).",
        )
    value, suffix = match.groups()
    return int(value) * _DURATION_MULTIPLIERS[suffix]


def _get_commit_timestamp(workdir: str, sha: str) -> int | None:
    """Return the committer Unix timestamp of *sha*, or None on git error."""
    try:
        result = subprocess.run(
            ["git", "-C", workdir, "show", "-s", "--format=%ct", sha],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("Could not read commit timestamp for %s: %s", sha[:7], exc)
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _get_commits_since(workdir: str, base_sha: str, upstream_ref: str) -> int | None:
    """Return how many commits *upstream_ref* has since *base_sha*, or None on error.

    Used only to enrich the failure message — never blocks the check.
    """
    try:
        result = subprocess.run(
            ["git", "-C", workdir, "rev-list", "--count", f"{base_sha}..{upstream_ref}"],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _format_age(seconds: int) -> str:
    """Format an age in seconds as a human-friendly short string.

    Examples: ``"5d 3h"``, ``"3h 12m"``, ``"45m"``.  Picks the two largest
    non-zero units so the result is precise without being verbose.
    """
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


def enforce_base_freshness(
    workdir: str,
    base_sha: str,
    max_age_seconds: int,
    review_branch: str | None,
    *,
    now: float | None = None,
) -> None:
    """Exit with a fatal error if *base_sha* is older than *max_age_seconds*.

    When *review_branch* is provided, the error message also reports how many
    commits ``origin/<review_branch>`` has accumulated since *base_sha* — a
    second-order signal that's often more actionable than the raw age (a 5-day
    base with zero upstream commits is a different problem than a 5-day base
    with 23 commits since).

    *now* is injectable for testability; defaults to wall-clock ``time.time()``.
    """
    commit_ts = _get_commit_timestamp(workdir, base_sha)
    if commit_ts is None:
        # We could not determine the age — fail open rather than blocking the run on
        # a transient git error.  The freshness check is a guardrail, not a gate.
        logger.warning(
            "Skipping --require-base-fresh check: could not determine age of %s",
            base_sha[:7],
        )
        return
    age = int((now if now is not None else time.time()) - commit_ts)
    if age <= max_age_seconds:
        return
    msg_lines = [
        f"review base {base_sha[:7]} is {_format_age(age)} old "
        f"(threshold {_format_age(max_age_seconds)}).",
    ]
    if review_branch:
        ahead = _get_commits_since(workdir, base_sha, f"origin/{review_branch}")
        if ahead is not None and ahead > 0:
            commits_word = "commit" if ahead == 1 else "commits"
            msg_lines.append(
                f"origin/{review_branch} has {ahead} {commits_word} since.",
            )
    msg_lines.append(
        f"Rerun against a fresh checkout, or pass --require-base-fresh {IGNORE_TOKEN} to disable.",
    )
    fatal("\n       ".join(msg_lines))
