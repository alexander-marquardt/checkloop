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
_LINE_COUNT_CHUNK_SIZE = 65536  # bytes per chunk when counting newlines
_GIT_CMD_TIMEOUT = 120  # seconds before a git subprocess is killed to prevent indefinite hangs

# Force English output from git regardless of the user's locale.  Without
# this, commands like ``git diff --shortstat`` produce localized strings
# (e.g. German "Einfügungen" instead of "insertions") that break the
# regex-based ``_parse_shortstat`` parser, causing convergence detection
# to silently report zero lines changed.
_GIT_ENV: dict[str, str] = {**os.environ, "LC_ALL": "C"}


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
    """Run a git command in *workdir* with captured output."""
    logger.debug("git %s (cwd=%s)", " ".join(args), workdir)
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
    """Return True if workdir is inside a git repository."""
    is_repo = _git_stdout(workdir, "rev-parse", "--is-inside-work-tree") is not None
    if not is_repo:
        logger.info("Not a git repo: %s", workdir)
    return is_repo


def git_head_sha(workdir: str) -> str | None:
    """Return the current HEAD commit SHA, or None if unavailable."""
    return _git_stdout(workdir, "rev-parse", "HEAD") or None


# --- Commit -------------------------------------------------------------------

_CHECKLOOP_PATHSPEC_EXCLUDES: list[str] = [
    ":(exclude).checkloop-run.log",
    ":(exclude).checkloop-checkpoint.json",
    ":(exclude).checkloop-ckpt-*.tmp",
]
"""Pathspec excludes to prevent checkloop's own files from being staged.

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
        _git_run(workdir, "add", "-A", "--", *_CHECKLOOP_PATHSPEC_EXCLUDES, check=True)
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

def _parse_shortstat(text: str) -> int:
    """Parse ``git diff --shortstat`` output into total lines changed."""
    insertions = deletions = 0
    match = re.search(r"(\d+) insertion", text)
    if match:
        insertions = int(match.group(1))
    match = re.search(r"(\d+) deletion", text)
    if match:
        deletions = int(match.group(1))
    if insertions == 0 and deletions == 0 and text.strip():
        logger.debug("No insertions/deletions parsed from shortstat: %r", text)
    return insertions + deletions


def _count_lines_changed(workdir: str, base_sha: str, target: str = "HEAD") -> int:
    """Return total lines changed (insertions + deletions) between two refs.

    If *target* is ``"HEAD"``, compares *base_sha* to ``HEAD``.  Pass a different
    ref or SHA to compare arbitrary points.  To include uncommitted working-tree
    changes, pass ``target=""`` (empty string triggers ``git diff <base>``).
    """
    if not base_sha:
        logger.warning("_count_lines_changed called with empty base_sha")
        return 0
    diff_args = ["diff", "--shortstat", base_sha]
    if target:  # empty string means diff against working tree (uncommitted changes)
        diff_args.append(target)
    output = _git_stdout(workdir, *diff_args)
    return _parse_shortstat(output) if output is not None else 0


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


# Cache: resolved workdir path → total tracked line count. Avoids re-scanning per check.
_total_lines_cache: dict[str, int] = {}


def _cached_total_tracked_lines(workdir: str) -> int:
    """Return cached total line count for all tracked files in *workdir*."""
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
    """Return the name of the default branch (main or master), falling back to 'main'."""
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


def compute_change_stats(workdir: str, base_sha: str) -> tuple[int, float]:
    """Return ``(lines_changed, change_percentage)`` since *base_sha*.

    *change_percentage* is calculated relative to the total number of
    tracked lines in the repository, clamped to a minimum of 1 to avoid
    division by zero.  Returns ``(0, 0.0)`` on any unexpected error so
    callers can continue safely.
    """
    try:
        lines_changed = _count_lines_changed(workdir, base_sha)
        if lines_changed == 0:
            return 0, 0.0
        return lines_changed, (lines_changed / _cached_total_tracked_lines(workdir)) * 100
    except Exception as exc:
        logger.error("Failed to compute change stats for %s vs %s: %s", workdir, base_sha, exc, exc_info=True)
        return 0, 0.0
