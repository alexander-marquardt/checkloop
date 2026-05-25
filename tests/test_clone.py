"""Tests for checkloop.clone — disposable clone preparation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from checkloop import clone


@pytest.fixture(autouse=True)
def _isolate_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CHECKLOOP_STATE_HOME at a fresh tmp dir for every test."""
    monkeypatch.setenv("CHECKLOOP_STATE_HOME", str(tmp_path))


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with one commit on 'main'."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True, capture_output=True)
    (path / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


# =============================================================================
# _plan_clone_path
# =============================================================================

class TestPlanClonePath:
    """Tests for _plan_clone_path()."""

    def test_path_format(self, tmp_path: Path) -> None:
        path = clone._plan_clone_path("/tmp/my-project", "2026-04-21T10-00-00Z")
        assert path.name == "my-project-2026-04-21T10-00-00Z"
        assert path.parent == tmp_path / "checkloop-runs"

    def test_sanitizes_basename(self) -> None:
        path = clone._plan_clone_path("/tmp/weird name@v1", "2026-01-01T00-00-00Z")
        assert path.name == "weird-name-v1-2026-01-01T00-00-00Z"

    def test_uses_generated_timestamp_when_none(self) -> None:
        path = clone._plan_clone_path("/tmp/proj", None)
        assert path.name.startswith("proj-")
        assert path.name.endswith("Z")


# =============================================================================
# _resolve_review_ref
# =============================================================================

class TestResolveReviewRef:
    """Tests for _resolve_review_ref()."""

    def test_origin_prefix_passes_through(self, tmp_path: Path) -> None:
        # Origin-prefixed refs skip the preference check.
        with mock.patch.object(clone, "_git_stdout"):
            result = clone._resolve_review_ref(tmp_path, "origin/main")
        assert result == "origin/main"

    def test_sha_passes_through(self, tmp_path: Path) -> None:
        # Looks like a SHA (7+ hex chars) — used verbatim.
        with mock.patch.object(clone, "_git_stdout"):
            result = clone._resolve_review_ref(tmp_path, "abc1234567")
        assert result == "abc1234567"

    def test_prefers_origin_variant_when_present(self, tmp_path: Path) -> None:
        # When origin/main exists, it's preferred over local main.
        def stdout(_workdir: str, *args: str) -> str | None:
            if args == ("rev-parse", "--verify", "origin/main"):
                return "abcdef1234"
            return None
        with mock.patch.object(clone, "_git_stdout", side_effect=stdout):
            result = clone._resolve_review_ref(tmp_path, "main")
        assert result == "origin/main"

    def test_falls_back_to_literal_ref(self, tmp_path: Path) -> None:
        # origin/main doesn't exist, but local main does.
        def stdout(_workdir: str, *args: str) -> str | None:
            if args == ("rev-parse", "--verify", "origin/main"):
                return None
            if args == ("rev-parse", "--verify", "main"):
                return "abcdef1234"
            return None
        with mock.patch.object(clone, "_git_stdout", side_effect=stdout):
            result = clone._resolve_review_ref(tmp_path, "main")
        assert result == "main"

    def test_raises_when_ref_not_found(self, tmp_path: Path) -> None:
        with mock.patch.object(clone, "_git_stdout", return_value=None):
            with pytest.raises(clone.CloneError, match="Review ref"):
                clone._resolve_review_ref(tmp_path, "nonexistent")


# =============================================================================
# prepare_clone — integration
# =============================================================================

class TestPrepareClone:
    """End-to-end tests for prepare_clone against a real git repo."""

    def test_rejects_non_git_repo(self, tmp_path: Path) -> None:
        non_git = tmp_path / "not-a-repo"
        non_git.mkdir()
        with pytest.raises(clone.CloneError, match="requires a git repo"):
            clone.prepare_clone(str(non_git), "main")

    def test_clones_and_checks_out_ref(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _init_git_repo(src)

        dst = clone.prepare_clone(str(src), "main", timestamp="2026-04-21T10-00-00Z")

        assert dst.exists()
        assert dst.name == "src-2026-04-21T10-00-00Z"
        # Clone should have the source's file.
        assert (dst / "README.md").read_text() == "hello"
        # Should be in detached-HEAD state after checkout.
        out = subprocess.run(
            ["git", "-C", str(dst), "symbolic-ref", "HEAD"],
            capture_output=True, text=True,
        )
        assert out.returncode != 0  # detached → symbolic-ref fails

    def test_rejects_existing_destination(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _init_git_repo(src)
        # Pre-create the destination so the clone step aborts.
        existing = tmp_path / "checkloop-runs" / "src-2026-04-21T10-00-00Z"
        existing.mkdir(parents=True)
        with pytest.raises(clone.CloneError, match="already exists"):
            clone.prepare_clone(str(src), "main", timestamp="2026-04-21T10-00-00Z")

    def test_raises_on_bad_review_ref(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _init_git_repo(src)
        with pytest.raises(clone.CloneError, match="Review ref"):
            clone.prepare_clone(str(src), "does-not-exist", timestamp="2026-04-21T10-00-00Z")

    def test_rewrites_origin_when_source_has_remote(self, tmp_path: Path) -> None:
        # Source has a real-looking GitHub origin URL configured.
        src = tmp_path / "src"
        _init_git_repo(src)
        subprocess.run(
            ["git", "-C", str(src), "remote", "add", "origin", "git@github.com:user/repo.git"],
            check=True, capture_output=True,
        )

        dst = clone.prepare_clone(str(src), "main", timestamp="2026-04-21T10-00-00Z")

        # The clone's origin should now point at the GitHub URL, not the local path.
        out = subprocess.run(
            ["git", "-C", str(dst), "config", "remote.origin.url"],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.strip() == "git@github.com:user/repo.git"

    def test_fetches_upstream_after_url_rewrite_by_default(self, tmp_path: Path) -> None:
        # The default behaviour: when source has a remote URL, prepare_clone
        # fetches twice — first from the local source (to capture local-only
        # state), then from the rewritten upstream URL (to capture any
        # commits that landed upstream since the user last pulled locally).
        src = tmp_path / "src"
        _init_git_repo(src)
        subprocess.run(
            ["git", "-C", str(src), "remote", "add", "origin", "git@github.com:user/repo.git"],
            check=True, capture_output=True,
        )

        with mock.patch.object(clone, "_fetch_origin", wraps=clone._fetch_origin) as spy:
            clone.prepare_clone(str(src), "main", timestamp="2026-04-21T10-00-00Z")

        # Expect two fetches: one before the rewrite, one after.
        assert spy.call_count == 2

    def test_no_fetch_upstream_skips_the_second_fetch(self, tmp_path: Path) -> None:
        # With fetch_upstream=False, only the first fetch runs (the local
        # source fetch).  The clone stays offline / local-only.
        src = tmp_path / "src"
        _init_git_repo(src)
        subprocess.run(
            ["git", "-C", str(src), "remote", "add", "origin", "git@github.com:user/repo.git"],
            check=True, capture_output=True,
        )

        with mock.patch.object(clone, "_fetch_origin", wraps=clone._fetch_origin) as spy:
            clone.prepare_clone(
                str(src), "main", timestamp="2026-04-21T10-00-00Z", fetch_upstream=False,
            )

        assert spy.call_count == 1

    def test_no_upstream_url_skips_the_second_fetch(self, tmp_path: Path) -> None:
        # When the source has no remote configured, the URL rewrite is a no-op
        # and there is no real upstream to fetch from.  Only the local fetch runs,
        # regardless of fetch_upstream — the flag is conditional on a remote URL.
        src = tmp_path / "src"
        _init_git_repo(src)  # no remote

        with mock.patch.object(clone, "_fetch_origin", wraps=clone._fetch_origin) as spy:
            clone.prepare_clone(
                str(src), "main", timestamp="2026-04-21T10-00-00Z", fetch_upstream=True,
            )

        assert spy.call_count == 1

    def test_leaves_origin_local_when_source_has_no_remote(self, tmp_path: Path) -> None:
        # Source has no "origin" — the clone's origin is left pointing at the source path.
        src = tmp_path / "src"
        _init_git_repo(src)  # does not configure a remote

        dst = clone.prepare_clone(str(src), "main", timestamp="2026-04-21T10-00-00Z")

        out = subprocess.run(
            ["git", "-C", str(dst), "config", "remote.origin.url"],
            capture_output=True, text=True, check=True,
        )
        # git clone --local sets origin to the source path — should still be that (not a GitHub URL).
        url = out.stdout.strip()
        assert not url.startswith(("git@", "ssh://", "https://", "http://", "git://"))


# =============================================================================
# _import_claude_memory
# =============================================================================

class TestImportClaudeMemory:
    """Tests for _import_claude_memory()."""

    @pytest.fixture
    def fake_projects_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect the Claude projects root to a tmp dir so tests are isolated."""
        root = tmp_path / "claude-projects"
        root.mkdir()
        monkeypatch.setattr(clone, "_claude_projects_root", lambda: root)
        return root

    def test_slug_format(self) -> None:
        slug = clone._slug_for_path(Path("/Users/alex/Documents/foo"))
        assert slug == "-Users-alex-Documents-foo"

    def test_copies_memory_when_original_has_it(
        self, tmp_path: Path, fake_projects_root: Path,
    ) -> None:
        original = tmp_path / "src"
        original.mkdir()
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        src_memory = fake_projects_root / clone._slug_for_path(original) / "memory"
        src_memory.mkdir(parents=True)
        (src_memory / "MEMORY.md").write_text("# project memory\n")
        (src_memory / "feedback_x.md").write_text("rule\n")

        clone._import_claude_memory(original, clone_dir)

        dst_memory = fake_projects_root / clone._slug_for_path(clone_dir) / "memory"
        assert dst_memory.is_dir()
        assert (dst_memory / "MEMORY.md").read_text() == "# project memory\n"
        assert (dst_memory / "feedback_x.md").read_text() == "rule\n"

    def test_silent_when_original_has_no_memory(
        self, tmp_path: Path, fake_projects_root: Path,
    ) -> None:
        # No memory dir for original — import is a no-op, no exception.
        clone._import_claude_memory(tmp_path / "src", tmp_path / "clone")
        # Clone slug dir should not have been created either.
        clone_slug = clone._slug_for_path(tmp_path / "clone")
        assert not (fake_projects_root / clone_slug).exists()

    def test_writes_to_clone_do_not_affect_original(
        self, tmp_path: Path, fake_projects_root: Path,
    ) -> None:
        # Critical safety property: a check session writing to its memory
        # dir must NOT mutate the original repo's memory.
        original = tmp_path / "src"
        original.mkdir()
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        src_memory = fake_projects_root / clone._slug_for_path(original) / "memory"
        src_memory.mkdir(parents=True)
        (src_memory / "MEMORY.md").write_text("original\n")

        clone._import_claude_memory(original, clone_dir)

        dst_memory = fake_projects_root / clone._slug_for_path(clone_dir) / "memory"
        (dst_memory / "MEMORY.md").write_text("MUTATED\n")
        (dst_memory / "new_file.md").write_text("new\n")

        # Original is untouched.
        assert (src_memory / "MEMORY.md").read_text() == "original\n"
        assert not (src_memory / "new_file.md").exists()

    def test_skips_when_clone_memory_already_populated(
        self, tmp_path: Path, fake_projects_root: Path,
    ) -> None:
        original = tmp_path / "src"
        original.mkdir()
        clone_dir = tmp_path / "clone"
        clone_dir.mkdir()

        src_memory = fake_projects_root / clone._slug_for_path(original) / "memory"
        src_memory.mkdir(parents=True)
        (src_memory / "MEMORY.md").write_text("from-original\n")

        # Pre-populate clone memory with different content.
        dst_memory = fake_projects_root / clone._slug_for_path(clone_dir) / "memory"
        dst_memory.mkdir(parents=True)
        (dst_memory / "MEMORY.md").write_text("pre-existing\n")

        clone._import_claude_memory(original, clone_dir)

        # Pre-existing content must be preserved (no overwrite).
        assert (dst_memory / "MEMORY.md").read_text() == "pre-existing\n"


# =============================================================================
# cleanup_empty_clone
# =============================================================================

class TestCleanupEmptyClone:
    """Tests for cleanup_empty_clone()."""

    def test_removes_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "to-remove"
        target.mkdir()
        (target / "file.txt").write_text("x")
        clone.cleanup_empty_clone(target)
        assert not target.exists()

    def test_silent_on_missing_dir(self, tmp_path: Path) -> None:
        # Should not raise if the directory is already gone.
        clone.cleanup_empty_clone(tmp_path / "does-not-exist")


# =============================================================================
# format_adoption_commands
# =============================================================================

class TestFormatAdoptionCommands:
    """Tests for format_adoption_commands()."""

    def test_with_scratch_branch(self) -> None:
        cmds = clone.format_adoption_commands(
            Path("/clone/dir"), "main-cl-2026-04-21T10-00-00Z", "/orig/dir",
        )
        joined = "\n".join(cmds)
        assert "cd /orig/dir" in joined
        assert "git fetch /clone/dir main-cl-2026-04-21T10-00-00Z" in joined
        assert "git merge --ff-only main-cl-2026-04-21T10-00-00Z" in joined

    def test_without_scratch_branch(self) -> None:
        cmds = clone.format_adoption_commands(Path("/clone/dir"), None, "/orig/dir")
        joined = "\n".join(cmds)
        assert "cd /orig/dir" in joined
        assert "/clone/dir" in joined
