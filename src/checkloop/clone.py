"""Create a disposable clone of the target repo for checkloop to operate on.

In the default (clone) mode, checkloop clones the target repo into
``~/checkloop-runs/<target-basename>-<iso-timestamp>/`` and runs every check
inside that clone.  The user's working tree and branches are never touched,
so checkloop can run while the user keeps coding, and an interrupted run
leaves nothing to clean up in the original repo.

The clone is made with ``git clone --local`` which uses hardlinks on the same
filesystem, so the disk cost is approximately zero — only uniquely modified
blobs consume space.  The startup ``git fetch origin --prune`` runs against
the source directory (fast, no network), *then* the clone's ``origin`` is
rewritten to the source repo's real remote URL (e.g. the GitHub URL) when
one is configured.  This means the user can ``git push origin <branch>``
from inside the clone to push directly to GitHub — no two-hop
``git fetch <clone-dir>`` dance through the original repo.

When the source has no pushable ``origin`` (e.g. a local-only repo), the
clone's origin is left pointing at the source path and the adoption flow
falls back to the fetch-from-clone instructions.

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
_REMOTE_URL_SCHEMES = ("git@", "ssh://", "https://", "http://", "git://")


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


def is_remote_url(url: str | None) -> bool:
    """Return True if *url* looks like a pushable remote URL (not a local path)."""
    if not url:
        return False
    return url.startswith(_REMOTE_URL_SCHEMES)


def _rewrite_origin_to_source_remote(src: Path, clone_dir: Path) -> str | None:
    """Point the clone's ``origin`` at the source repo's real remote, if any.

    Runs *after* the startup ``git fetch origin --prune`` (which fetches from
    the source path — fast, no network).  Once the fetched refs are in place,
    we swap ``origin`` over to whatever URL the source's own ``origin`` points
    at, so ``git push origin <branch>`` from inside the clone lands on that
    remote (typically GitHub) instead of the user's local source directory.

    Returns the new origin URL when a rewrite was applied and the URL looks
    pushable, or ``None`` when left alone (no origin in source, or it's a
    local path).  The caller uses the return value to decide which adoption
    flow to print after the run.
    """
    source_origin = _git_stdout(str(src), "config", "remote.origin.url")
    if source_origin is None or not is_remote_url(source_origin):
        return None
    try:
        _git_run(str(clone_dir), "remote", "set-url", "origin", source_origin, check=True)
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Could not rewrite clone origin to %s (non-fatal): %s", source_origin, exc)
        return None
    logger.info("Clone origin rewritten to %s (push target)", source_origin)
    return source_origin


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


def _claude_projects_root() -> Path:
    """Return ``~/.claude/projects/`` — the parent of every per-project memory dir."""
    return Path.home() / ".claude" / "projects"


def _slug_for_path(path: Path) -> str:
    """Translate an absolute filesystem path to Claude Code's project-slug format.

    Claude stores per-project state under ``~/.claude/projects/<slug>/`` where
    ``<slug>`` is the absolute path with every ``/`` replaced by ``-``.  For
    ``/Users/alex/Documents/foo`` the slug is ``-Users-alex-Documents-foo``.
    """
    return str(path.resolve()).replace("/", "-")


def _import_claude_memory(original_workdir: Path, clone_dir: Path) -> None:
    """Copy the original repo's Claude auto-memory into the clone's slug.

    Read-only import: the original repo's ``memory/`` directory is never
    modified, and any memory Claude writes during a check run lands in the
    clone's slug — so it is intentionally orphaned when the clone is cleaned
    up.  This is the design choice: checkloop should be able to *read*
    project context (prior incidents, user preferences, pending follow-ups)
    so checks stay consistent with established conventions, but it must
    never persist state back into the user's authoritative memory.

    Silently skips when the original has no memory dir (most projects), or
    when the clone's slug is already populated (defensive — clone is fresh
    on the happy path).  All copy errors are non-fatal.
    """
    projects_root = _claude_projects_root()
    src = projects_root / _slug_for_path(original_workdir) / "memory"
    if not src.is_dir():
        logger.debug("No Claude memory at %s — nothing to import", src)
        return
    dst_parent = projects_root / _slug_for_path(clone_dir)
    dst = dst_parent / "memory"
    if dst.exists():
        logger.debug("Clone memory dir already exists at %s — skipping import", dst)
        return
    try:
        dst_parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    except OSError as exc:
        logger.warning("Could not import Claude memory from %s (non-fatal): %s", src, exc)
        return
    logger.info("Imported Claude memory %s → %s (read-only; writes during the run are orphaned)", src, dst)


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
    fetch_upstream: bool = True,
) -> Path:
    """Clone *workdir* and check out *review_branch* in the clone.

    Steps, each abort-on-failure unless noted:

      1. Verify *workdir* is a git repo (cloning a non-git dir is meaningless).
      2. Reserve the clone path under ``get_runs_root()``.
      3. ``git clone --local`` with hardlinks (near-zero disk).
      4. ``git fetch origin --prune`` in the clone — first pass, against the
         local source (no network), so the clone has all of the user's
         local-only commits and branches.
      5. Rewrite ``origin`` to the source repo's real remote URL.
      6. If *fetch_upstream* is true and a remote URL was set, ``git fetch
         origin --prune`` again — second pass, now against the real remote
         (typically GitHub).  This updates the clone's ``origin/<branch>``
         refs to reflect current upstream, so the scratch branch we are
         about to fork is based on real upstream HEAD rather than on the
         user's possibly-stale local mirror.  Best-effort: a network or
         auth failure falls back to the locally-fetched state with a warning.
      7. Resolve *review_branch* to a ref (prefers ``origin/<name>``, which
         now reflects upstream after step 6).
      8. ``git checkout --detach <ref>`` so checkloop's commits don't push back.
      9. Import the original repo's Claude auto-memory into the clone's slug
         (read-only; best effort) so check sessions inherit project context.

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
    # Rewrite origin to the source's real remote URL before the second fetch
    # so the network fetch (if requested) hits the actual upstream — typically
    # GitHub — rather than re-fetching from the local source path.
    upstream_url = _rewrite_origin_to_source_remote(src, dst)
    if fetch_upstream and upstream_url is not None:
        # Second fetch, now against the real remote.  Without this, the
        # clone's origin/<branch> refs reflect only what the user's local
        # source had at clone time — which may be days behind real upstream.
        # The scratch branch we are about to fork would then be based on a
        # stale commit, and every extraction or refactor the run produces
        # against that base would need manual re-application by the human
        # reviewer once they adopt the work into the up-to-date repo.
        logger.info("Fetching from upstream %s to refresh origin/* refs", upstream_url)
        _fetch_origin(dst)
    # Ref resolution AFTER both fetches so origin/<branch> reflects upstream.
    ref = _resolve_review_ref(dst, review_branch)
    _checkout_ref(dst, ref)
    logger.info("Checked out %s in clone (detached)", ref)

    _import_claude_memory(src, dst)

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
