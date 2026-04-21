"""Create a disposable clone of the target repo for checkloop to operate on.

In the default (clone) mode, checkloop clones the target repo into
``~/checkloop-runs/<target-basename>-<iso-timestamp>/`` and runs every check
inside that clone.  The user's working tree and branches are never touched,
so checkloop can run while the user keeps coding, and an interrupted run
leaves nothing to clean up in the original repo.

The clone is made with ``git clone --local`` which uses hardlinks on the same
filesystem, so the disk cost is approximately zero — only uniquely modified
blobs consume space.  The clone's ``origin`` points at the user's local
workdir, which means ``origin/<branch>`` in the clone reflects the user's
local view.  A ``git fetch origin --prune`` runs at startup so those refs are
current relative to the user's last fetch.

Review ref selection
--------------------
The caller passes the branch name (or any git ref) to review.  Resolution
logic:

  1. If the ref starts with ``origin/`` or looks like a SHA/tag, use as-is.
  2. Otherwise, prefer ``origin/<ref>`` when that remote-tracking ref exists
     (so ``--review-branch main`` gives you the remote state, not a possibly
     stale local branch).
  3. Fall back to the literal ref.

After resolution, the clone is checked out in detached-HEAD state at the
resolved ref so any local commits checkloop creates on top don't implicitly
push back to the remote through the upstream.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from checkloop.git import _git_run, _git_stdout, is_git_repo
from checkloop.run_storage import (
    _sanitize_basename,
    get_runs_root,
    iso_timestamp,
)

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class CloneError(RuntimeError):
    """Raised when the clone cannot be prepared.  Fatal — the run should abort."""


def _plan_clone_path(workdir: str, timestamp: str | None) -> Path:
    ts = timestamp or iso_timestamp()
    base = _sanitize_basename(Path(workdir).name)
    return get_runs_root() / f"{base}-{ts}"


def _ensure_runs_root() -> None:
    root = get_runs_root()
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise CloneError(f"Could not create runs root {root}: {exc}") from exc


def _git_clone_local(src: Path, dst: Path) -> None:
    """Run ``git clone --local src dst`` — uses hardlinks when same filesystem."""
    try:
        subprocess.run(
            ["git", "clone", "--local", str(src), str(dst)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise CloneError(f"git clone failed: {stderr}") from exc
    except OSError as exc:
        raise CloneError(f"git clone could not run: {exc}") from exc


def _fetch_origin(clone_dir: Path) -> None:
    """Best-effort ``git fetch origin --prune`` so origin/* refs are current."""
    try:
        _git_run(str(clone_dir), "fetch", "origin", "--prune", check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        # Non-fatal: the clone's origin/* refs may be slightly stale but the
        # checkout below will still succeed for anything already present.
        logger.warning("git fetch origin failed in clone (non-fatal): %s", exc)


def _resolve_review_ref(clone_dir: Path, requested: str) -> str:
    """Return the ref that should be passed to ``git checkout``.

    Prefers ``origin/<requested>`` when available so ``--review-branch main``
    gives the remote state.  Falls back to the literal ref when no remote
    variant exists (tags, SHAs, unusual ref paths).
    """
    if requested.startswith("origin/") or _SHA_RE.match(requested):
        return requested
    candidate = f"origin/{requested}"
    if _git_stdout(str(clone_dir), "rev-parse", "--verify", candidate) is not None:
        return candidate
    if _git_stdout(str(clone_dir), "rev-parse", "--verify", requested) is not None:
        return requested
    raise CloneError(
        f"Review ref {requested!r} not found (tried {candidate!r} and {requested!r}). "
        "Make sure the branch exists in the source repo and `git fetch origin` "
        "has been run there at least once.",
    )


def _checkout_ref(clone_dir: Path, ref: str) -> None:
    """Check out *ref* in detached-HEAD state so local commits don't track it."""
    try:
        _git_run(str(clone_dir), "checkout", "--detach", ref, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise CloneError(f"git checkout {ref} failed in clone: {exc}") from exc


def prepare_clone(
    workdir: str,
    review_branch: str,
    *,
    timestamp: str | None = None,
) -> Path:
    """Clone *workdir* and check out *review_branch* in the clone.

    Steps, each abort-on-failure:

      1. Verify *workdir* is a git repo (cloning a non-git dir is meaningless).
      2. Reserve the clone path under ``get_runs_root()``.
      3. ``git clone --local`` with hardlinks (near-zero disk).
      4. ``git fetch origin --prune`` in the clone (best effort).
      5. Resolve *review_branch* to a ref (prefers ``origin/<name>``).
      6. ``git checkout --detach <ref>`` so checkloop's commits don't push back.

    Returns the clone directory path.  Raises :class:`CloneError` on any
    step the run can't recover from.
    """
    src = Path(workdir).resolve()
    if not is_git_repo(str(src)):
        raise CloneError(
            f"--review-branch requires a git repo; {workdir!r} isn't one. "
            "Use --in-place to run checkloop on a non-git directory.",
        )

    _ensure_runs_root()
    dst = _plan_clone_path(str(src), timestamp)
    if dst.exists():
        raise CloneError(f"Clone path already exists: {dst}")

    _git_clone_local(src, dst)
    logger.info("Cloned %s → %s", src, dst)

    _fetch_origin(dst)
    ref = _resolve_review_ref(dst, review_branch)
    _checkout_ref(dst, ref)
    logger.info("Checked out %s in clone (detached)", ref)

    return dst


def format_adoption_commands(
    clone_dir: Path,
    scratch_branch: str | None,
    original_workdir: str,
) -> list[str]:
    """Return copy-pasteable shell commands the user can run to adopt checkloop's work.

    The user runs these from anywhere — they cd into the original repo, fetch
    the scratch branch from the clone directory, and can merge, cherry-pick,
    or discard.  The clone dir is a valid git remote path so ``git fetch`` Just
    Works.
    """
    if scratch_branch is None:
        return [
            f"cd {original_workdir}",
            f"# The clone is at: {clone_dir}",
            f"# To diff: git -C {clone_dir} diff HEAD",
        ]
    return [
        f"cd {original_workdir}",
        f"git fetch {clone_dir} {scratch_branch}:{scratch_branch}",
        f"git log --oneline {scratch_branch}  # review what checkloop did",
        f"git merge --ff-only {scratch_branch}  # or: git cherry-pick <sha>",
        f"# To discard: rm -rf {clone_dir} && git branch -D {scratch_branch}",
    ]


def cleanup_empty_clone(clone_dir: Path) -> None:
    """Remove a clone directory that produced no useful changes.

    Called from the error-handling path when a run is aborted before doing
    anything meaningful.  Falls back silently on permission errors.
    """
    try:
        shutil.rmtree(clone_dir)
        logger.info("Removed empty clone: %s", clone_dir)
    except OSError as exc:
        logger.debug("Could not remove clone %s: %s", clone_dir, exc)
