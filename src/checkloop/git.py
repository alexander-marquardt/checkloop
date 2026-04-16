"""Git helpers for convergence detection and change tracking.

Wraps git CLI commands to support change measurement (line diffs, tracked-file
line counting), commit operations, branch detection,
and changed-file listing for the ``--changed-only`` feature.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Literal, overload

logger = logging.getLogger(__name__)

# I/O chunk sizes for file-based line counting (independent of process streaming).
_BINARY_CHECK_SIZE = 8192  # bytes to read when checking for null bytes (binary detection)
_LINE_COUNT_CHUNK_SIZE = 65536
_GIT_CMD_TIMEOUT = 120  # seconds before a git subprocess is killed to prevent indefinite hangs

# Force English output from git regardless of the user's locale.  Without
# this, commands like ``git diff --shortstat`` produce localized strings
# (e.g. German "Einfügungen" instead of "insertions") that break the
# regex-based ``_parse_shortstat`` parser, causing convergence detection
# to silently report zero lines changed.
_GIT_ENV: dict[str, str] = {**os.environ, "LC_ALL": "C"}

# Pre-compiled regexes for _parse_shortstat — called on every diff stat
# computation so avoiding re.compile overhead on each invocation matters.
_RE_INSERTIONS = re.compile(r"(\d+) insertion")
_RE_DELETIONS = re.compile(r"(\d+) deletion")


# --- Low-level git wrappers --------------------------------------------------

@overload
def _git_run(
    workdir: str,
    *args: str,
    check: bool = False,
    text: Literal[True] = ...,
) -> subprocess.CompletedProcess[str]: ...

@overload
def _git_run(
    workdir: str,
    *args: str,
    check: bool = False,
    text: Literal[False] = ...,
) -> subprocess.CompletedProcess[bytes]: ...

def _git_run(
    workdir: str,
    *args: str,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=text,
            check=check,
            timeout=_GIT_CMD_TIMEOUT,
            env=_GIT_ENV,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("git %s timed out after %ds (cwd=%s)", args[0] if args else "", _GIT_CMD_TIMEOUT, workdir)
        raise OSError(f"git {args[0] if args else ''} timed out after {_GIT_CMD_TIMEOUT}s") from exc
    except FileNotFoundError:
        logger.error("git binary not found — is git installed?")
        raise
    except OSError as exc:
        logger.error("Failed to run git %s: %s", args[0] if args else "", exc, exc_info=True)
        raise


def _git_stdout(workdir: str, *args: str) -> str | None:
    """Run a git command and return stripped stdout, or None on any error.

    Consolidates the repeated try/_git_run/except-OSError/check-returncode
    pattern used by most git helper functions.  Returns the stripped stdout
    string on success, or None if the command raised OSError or exited with
    a non-zero return code.
    """
    try:
        result = _git_run(workdir, *args)
    except OSError:
        return None
    if result.returncode != 0:
        logger.debug("git %s failed (rc=%d): %s", args[0] if args else "",
                      result.returncode, (result.stderr or "").strip())
        return None
    return result.stdout.strip()


def is_git_repo(workdir: str) -> bool:
    is_repo = _git_stdout(workdir, "rev-parse", "--is-inside-work-tree") is not None
    if not is_repo:
        logger.info("Not a git repo: %s", workdir)
    return is_repo


def git_head_sha(workdir: str) -> str | None:
    return _git_stdout(workdir, "rev-parse", "HEAD") or None


# --- Working tree status ------------------------------------------------------

def has_uncommitted_changes(workdir: str) -> bool:
    """Return True if there are staged, unstaged, or untracked changes."""
    output = _git_stdout(workdir, "status", "--porcelain")
    return bool(output)


def get_uncommitted_diff(workdir: str) -> str:
    """Return the combined diff of all uncommitted changes relative to HEAD.

    Includes both staged and unstaged modifications.  Falls back to
    ``git diff --cached`` if HEAD does not exist (initial commit scenario).
    Returns an empty string if no diff is available.
    """
    diff = _git_stdout(workdir, "diff", "HEAD")
    if diff is not None:
        return diff
    # No HEAD yet (no commits) — show staged changes only.
    return _git_stdout(workdir, "diff", "--cached") or ""


# --- Commit -------------------------------------------------------------------

_CHECKLOOP_UNSTAGE_PATTERNS: list[str] = [
    ".checkloop-run.log",
    ".checkloop-run.log.*",
    ".checkloop-checkpoint.json",
    ".checkloop-ckpt-*.tmp",
    ".checkloop-logs",
    ".checkloop-project-map.md",
]
"""File patterns to unstage after `git add -A`.

These are checkloop's own operational files that should never be committed.
The log file contains DEBUG-level data (prompts, file paths) that is
intentionally written with 0o600 permissions.  Committing it into git
history would permanently expose that sensitive content.
"""


def git_commit_all(workdir: str, message: str) -> bool:
    """Stage and commit any uncommitted changes.

    Excludes checkloop's own files (.checkloop-run.log, checkpoint files)
    from staging to avoid leaking sensitive operational data into git history.

    Returns True if a commit was created (i.e. there were changes to commit).
    """
    start_time = time.time()
    try:
        _git_run(workdir, "add", "-A", check=True)
        # Unstage checkloop's own files (ignore errors — files may not be staged).
        for pattern in _CHECKLOOP_UNSTAGE_PATTERNS:
            _git_run(workdir, "reset", "HEAD", "--", pattern)
        if _git_run(workdir, "diff", "--cached", "--quiet").returncode == 0:
            logger.debug("No staged changes — nothing to commit")
            return False
        _git_run(workdir, "commit", "-m", message, check=True)
        new_sha = git_head_sha(workdir)
        logger.info("Committed in %.2fs: %s (sha=%s)", time.time() - start_time, message, new_sha)
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Git commit failed after %.2fs: %s", time.time() - start_time, exc, exc_info=True)
        return False


# --- Diff statistics ----------------------------------------------------------

def _parse_shortstat(text: str) -> tuple[int, int]:
    """Parse ``git diff --shortstat`` output and return (insertions, deletions).

    Convergence detection cares about total *churn* — a rename that preserves
    line count but rewrites every line still represents significant change.
    Callers typically sum these to get total lines changed.
    """
    insertions = deletions = 0
    match = _RE_INSERTIONS.search(text)
    if match:
        insertions = int(match.group(1))
    match = _RE_DELETIONS.search(text)
    if match:
        deletions = int(match.group(1))
    if insertions == 0 and deletions == 0 and text.strip():
        logger.debug("No insertions/deletions parsed from shortstat: %r", text)
    return insertions, deletions


def _count_lines_changed(workdir: str, base_sha: str, target: str = "HEAD") -> tuple[int, int, int]:
    """Return (insertions, deletions, total) lines changed between two refs.

    If *target* is ``"HEAD"``, compares *base_sha* to ``HEAD``.  Pass a different
    ref or SHA to compare arbitrary points.  To include uncommitted working-tree
    changes, pass ``target=""`` (empty string triggers ``git diff <base>``).
    """
    if not base_sha:
        logger.warning("_count_lines_changed called with empty base_sha")
        return 0, 0, 0
    diff_args = ["diff", "--shortstat", base_sha]
    if target:  # empty string means diff against working tree (uncommitted changes)
        diff_args.append(target)
    output = _git_stdout(workdir, *diff_args)
    if output is None:
        return 0, 0, 0
    insertions, deletions = _parse_shortstat(output)
    return insertions, deletions, insertions + deletions


# --- Line counting (for convergence percentage) -------------------------------

def _count_file_lines(filepath: Path) -> int:
    """Count newlines in a text file, reading in chunks. Returns 0 for binary files."""
    try:
        with open(filepath, "rb") as raw_file:
            # Read a small header to check for null bytes (binary file indicator).
            header = raw_file.read(_BINARY_CHECK_SIZE)
            if b"\0" in header:
                return 0
            total = header.count(b"\n")
            for chunk in iter(lambda: raw_file.read(_LINE_COUNT_CHUNK_SIZE), b""):
                total += chunk.count(b"\n")
            return total
    except OSError as exc:
        logger.debug("Cannot read file for line counting %s: %s", filepath, exc)
        return 0


def _safe_count_file_in_workdir(resolved_workdir: Path, relative_path: str) -> int:
    """Count lines in a tracked file, returning 0 if outside workdir or unreadable."""
    try:
        absolute_path = (resolved_workdir / relative_path).resolve()
        if not absolute_path.is_relative_to(resolved_workdir):
            return 0  # skip paths that escape the workdir (path traversal guard)
        return _count_file_lines(absolute_path)
    except OSError as exc:
        logger.debug("Could not read tracked file %s: %s", relative_path, exc)
        return 0


def _count_tracked_lines(workdir: str) -> int:
    """Count total lines across all git-tracked text files.

    Reads files in small chunks to avoid loading large files entirely into
    memory, which matters for long-running sessions on big repos.
    """
    start_time = time.time()
    try:
        ls_result = _git_run(workdir, "ls-files", "-z", text=False)
    except OSError as exc:
        logger.warning("git ls-files failed in %s: %s", workdir, exc)
        return 1  # avoid division by zero
    if ls_result.returncode != 0:
        logger.warning("git ls-files failed (rc=%d): %s", ls_result.returncode,
                       (ls_result.stderr or b"").decode("utf-8", errors="replace").strip())
        return 1  # avoid division by zero
    # -z flag outputs null-separated paths; split once, iterate as generator
    # to avoid materialising a full decoded-path list for large repos.
    raw_path_segments = ls_result.stdout.split(b"\0")
    total_lines = 0
    file_count = 0
    resolved_workdir = Path(workdir).resolve()
    for raw_path in raw_path_segments:
        if not raw_path:
            continue
        relative_path = raw_path.decode("utf-8", errors="replace")
        file_count += 1
        total_lines += _safe_count_file_in_workdir(resolved_workdir, relative_path)
    # Clamp to minimum 1 to prevent division-by-zero in convergence percentage calculations.
    total_clamped = max(total_lines, 1)
    elapsed = time.time() - start_time
    logger.info("Counted %d tracked lines across %d files in %.2fs", total_clamped, file_count, elapsed)
    return total_clamped


# Cache: resolved workdir path → total tracked line count.
# Populated once per process invocation and never updated, so all convergence
# percentage calculations use the same denominator throughout a run.  This is
# intentional: we want to measure "what fraction of the original codebase was
# touched this cycle", not a moving target that shrinks as files are deleted.
_total_lines_cache: dict[str, int] = {}


def _cached_total_tracked_lines(workdir: str) -> int:
    """Return the total tracked line count, computing it once and caching for the run.

    The cache is keyed by resolved workdir path and is never invalidated during
    a run.  Using a fixed baseline means convergence percentages stay comparable
    across cycles even if files are added or removed.
    """
    try:
        cache_key = str(Path(workdir).resolve())
    except OSError as exc:
        logger.warning("Cannot resolve workdir '%s' for line counting: %s", workdir, exc)
        return 1  # avoid division by zero
    if cache_key not in _total_lines_cache:
        _total_lines_cache[cache_key] = _count_tracked_lines(workdir)
    return _total_lines_cache[cache_key]


# --- Branch and changed-file helpers -----------------------------------------

def detect_default_branch(workdir: str) -> str:
    """Return the default branch name ('main' or 'master'), falling back to 'main'.

    Probes for 'main' then 'master' via ``git rev-parse --verify``.
    If neither exists (e.g. unusual branch names), returns 'main' as a
    best-guess default so ``--changed-only auto`` still produces a usable
    base ref rather than failing silently.
    """
    for branch in ("main", "master"):
        if _git_stdout(workdir, "rev-parse", "--verify", f"refs/heads/{branch}") is not None:
            logger.info("Detected default branch: %s", branch)
            return branch
    logger.info("No main/master branch found — falling back to 'main'")
    return "main"


def get_changed_files(workdir: str, base_ref: str) -> list[str]:
    """Return list of files changed between *base_ref* and HEAD.

    Uses ``git merge-base`` to find the common ancestor, then ``git diff --name-only``
    to list changed files. Returns an empty list if either command fails.
    """
    start_time = time.time()
    if not base_ref or not base_ref.strip():
        logger.warning("get_changed_files called with empty base_ref")
        return []
    base_sha = _git_stdout(workdir, "merge-base", base_ref, "HEAD")
    if not base_sha:
        logger.warning("Could not determine merge-base for ref '%s'", base_ref)
        return []
    diff_output = _git_stdout(workdir, "diff", "--name-only", base_sha, "HEAD")
    if diff_output is None:
        return []
    files = [f for f in diff_output.split("\n") if f]
    logger.info("Found %d changed file(s) vs %s in %.2fs", len(files), base_ref, time.time() - start_time)
    return files


def get_unpushed_commits(workdir: str) -> list[str]:
    """Return one-line descriptions of local commits not yet pushed to the remote.

    Returns an empty list when there is no upstream branch or on any error,
    which lets callers treat "no upstream" the same as "nothing to push".
    """
    output = _git_stdout(workdir, "log", "--oneline", "@{u}..HEAD")
    if output is None:
        return []
    return [line for line in output.splitlines() if line]


def build_changed_files_prefix(changed_files: list[str]) -> str:
    """Build a prompt prefix that restricts review to the given files.

    Returns an empty string if *changed_files* is empty, since there are
    no files to restrict the review to.
    """
    if not changed_files:
        return ""
    file_list = "\n".join(f"  - {f}" for f in changed_files)
    return (
        f"IMPORTANT: Only review the following {len(changed_files)} file(s) that have changed. "
        "Do NOT review or modify any other files.\n"
        f"Changed files:\n{file_list}\n\n"
    )


def compute_change_stats(workdir: str, base_sha: str) -> tuple[int, int, int, float]:
    """Return ``(lines_added, lines_deleted, lines_changed, change_percentage)`` since *base_sha*.

    *lines_changed* is the sum of insertions + deletions.
    *change_percentage* is calculated relative to the total number of
    tracked lines in the repository, clamped to a minimum of 1 to avoid
    division by zero.  Returns ``(0, 0, 0, 0.0)`` on any unexpected error so
    callers can continue safely.
    """
    try:
        insertions, deletions, lines_changed = _count_lines_changed(workdir, base_sha)
        if lines_changed == 0:
            return 0, 0, 0, 0.0
        pct = (lines_changed / _cached_total_tracked_lines(workdir)) * 100
        return insertions, deletions, lines_changed, pct
    except Exception as exc:
        logger.error("Failed to compute change stats for %s vs %s: %s", workdir, base_sha, exc, exc_info=True)
        return 0, 0, 0, 0.0


def compute_file_stats(workdir: str, base_sha: str) -> tuple[int, int, int]:
    """Return ``(files_added, files_deleted, files_modified)`` since *base_sha*.

    Uses ``git diff --name-status`` to classify each changed file. Returns
    ``(0, 0, 0)`` on error or if *base_sha* is empty.
    """
    if not base_sha:
        logger.warning("compute_file_stats called with empty base_sha")
        return 0, 0, 0
    try:
        output = _git_stdout(workdir, "diff", "--name-status", base_sha, "HEAD")
        if output is None:
            return 0, 0, 0
        added = deleted = modified = 0
        for line in output.strip().splitlines():
            if not line:
                continue
            status = line[0]
            if status == "A":
                added += 1
            elif status == "D":
                deleted += 1
            else:
                # M (modified), R (renamed), C (copied), T (type change) etc
                modified += 1
        return added, deleted, modified
    except Exception as exc:
        logger.error("Failed to compute file stats for %s vs %s: %s", workdir, base_sha, exc, exc_info=True)
        return 0, 0, 0
