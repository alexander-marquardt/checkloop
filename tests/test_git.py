"""Tests for checkloop.git — core git operations: run, repo detection, commit, diff stats."""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from checkloop import git
from tests.helpers import make_git_result


class TestGitRun:
    """Tests for _git_run()."""

    def test_empty_args(self) -> None:
        result = git._git_run("/tmp")
        assert result.returncode != 0

    def test_git_not_found(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(FileNotFoundError):
                git._git_run("/tmp", "status")

    def test_oserror_reraised(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("permission denied")):
            with pytest.raises(OSError):
                git._git_run("/tmp", "status")


class TestIsGitRepo:
    """Tests for is_git_repo() detection."""

    def test_true_when_git_succeeds(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result()):
            assert git.is_git_repo("/tmp") is True

    def test_false_when_git_fails(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=128)):
            assert git.is_git_repo("/tmp") is False

    def test_oserror_returns_false(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            assert git.is_git_repo("/tmp") is False


class TestGitHeadSha:
    """Tests for git_head_sha() SHA retrieval."""

    def test_returns_sha(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="abc123\n")):
            assert git.git_head_sha("/tmp") == "abc123"

    def test_returns_none_on_failure(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(returncode=128)):
            assert git.git_head_sha("/tmp") is None

    def test_empty_stdout_returns_none(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result()):
            assert git.git_head_sha("/tmp") is None

    def test_whitespace_only_stdout_returns_none(self) -> None:
        with mock.patch("subprocess.run", return_value=make_git_result(stdout="   \n  ")):
            assert git.git_head_sha("/tmp") is None

    def test_oserror_returns_none(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("no git")):
            assert git.git_head_sha("/tmp") is None


class TestGitCommitAll:
    """Tests for git_commit_all() post-cycle commit."""

    def test_commits_when_changes_exist(self) -> None:
        num_patterns = len(git._CHECKLOOP_UNSTAGE_PATTERNS)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_git_result(),                                   # git add -A
                *[make_git_result() for _ in range(num_patterns)],   # git reset per pattern
                make_git_result(stdout=""),                          # diff --cached --name-only -z (no artifacts)
                make_git_result(returncode=1),                       # git diff --cached --quiet (changes exist)
                make_git_result(),                                   # git commit
                make_git_result(stdout="abc123\n"),                  # git rev-parse HEAD
            ]
            assert git.git_commit_all("/tmp", "test commit") is True

    def test_no_commit_when_clean(self) -> None:
        num_patterns = len(git._CHECKLOOP_UNSTAGE_PATTERNS)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_git_result(),                                   # git add -A
                *[make_git_result() for _ in range(num_patterns)],   # git reset per pattern
                make_git_result(stdout=""),                          # diff --cached --name-only -z (no artifacts)
                make_git_result(),                                   # git diff --cached --quiet (no changes)
            ]
            assert git.git_commit_all("/tmp", "test commit") is False

    def test_unstages_artifacts_before_committing(self) -> None:
        """When git add -A stages artifact paths, they are unstaged before commit."""
        num_patterns = len(git._CHECKLOOP_UNSTAGE_PATTERNS)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_git_result(),                                          # git add -A
                *[make_git_result() for _ in range(num_patterns)],          # git reset per pattern
                make_git_result(stdout="src/a.py\0search-ui/coverage/x.html"),  # diff --cached --name-only -z
                make_git_result(),                                          # git rm --cached --ignore-unmatch ...
                make_git_result(returncode=1),                              # git diff --cached --quiet
                make_git_result(),                                          # git commit
                make_git_result(stdout="abc123\n"),                         # git rev-parse HEAD
            ]
            assert git.git_commit_all("/tmp", "test commit") is True

        # Find the `git rm --cached` call.
        rm_calls = [c for c in mock_run.call_args_list if "rm" in (c[0][0] if c[0] else [])]
        assert len(rm_calls) == 1
        rm_args = rm_calls[0][0][0]
        assert "--cached" in rm_args
        assert "search-ui/coverage/x.html" in rm_args
        assert "src/a.py" not in rm_args

    def test_returns_false_on_error(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git")):
            assert git.git_commit_all("/tmp", "test commit") is False

    def test_git_add_failure(self) -> None:
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "git add")):
            assert git.git_commit_all("/tmp", "test commit") is False

    def test_oserror_during_commit(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("disk full")):
            assert git.git_commit_all("/tmp", "test commit") is False


class TestParseShortstat:
    """Tests for _parse_shortstat() diff output parsing.

    _parse_shortstat returns (insertions, deletions) tuple.
    """

    def test_insertions_and_deletions(self) -> None:
        text = " 3 files changed, 20 insertions(+), 10 deletions(-)"
        assert git._parse_shortstat(text) == (20, 10)

    def test_insertions_only(self) -> None:
        text = " 1 file changed, 5 insertions(+)"
        assert git._parse_shortstat(text) == (5, 0)

    def test_deletions_only(self) -> None:
        text = " 2 files changed, 8 deletions(-)"
        assert git._parse_shortstat(text) == (0, 8)

    def test_empty_string(self) -> None:
        assert git._parse_shortstat("") == (0, 0)

    def test_no_match(self) -> None:
        assert git._parse_shortstat("nothing here") == (0, 0)

    def test_zero_insertions_and_deletions(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 0 insertions(+), 0 deletions(-)") == (0, 0)

    def test_whitespace_only(self) -> None:
        assert git._parse_shortstat("   \n\t  ") == (0, 0)

    def test_only_files_changed(self) -> None:
        assert git._parse_shortstat(" 3 files changed") == (0, 0)

    def test_large_numbers(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 999999 insertions(+), 888888 deletions(-)") == (999999, 888888)

    def test_singular_insertion(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 1 insertion(+)") == (1, 0)

    def test_singular_deletion(self) -> None:
        assert git._parse_shortstat(" 1 file changed, 1 deletion(-)") == (0, 1)


class TestCountLinesChanged:
    """Tests for _count_lines_changed().

    _count_lines_changed returns (insertions, deletions, total) tuple.
    """

    def test_empty_base_sha_returns_zero(self) -> None:
        result = git._count_lines_changed("/tmp", "")
        assert result == (0, 0, 0)

    def test_valid_sha_with_empty_target(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result(
            stdout=" 1 file changed, 3 insertions(+)"
        )) as mock_git:
            result = git._count_lines_changed("/tmp", "abc123", target="")
        assert result == (3, 0, 3)
        call_args = mock_git.call_args[0]
        assert "abc123" in call_args
        assert "" not in call_args[2:]

    def test_git_diff_nonzero_returncode(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result(
            returncode=128, stderr="fatal: bad revision"
        )):
            result = git._count_lines_changed("/tmp", "badref")
        assert result == (0, 0, 0)

    def test_git_diff_oserror(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            result = git._count_lines_changed("/tmp", "abc123")
        assert result == (0, 0, 0)

    def test_whitespace_only_base_sha_proceeds(self) -> None:
        """Whitespace-only SHA is truthy, so git is called (and likely fails)."""
        with mock.patch.object(git, "_git_run", return_value=make_git_result(
            returncode=128, stderr="fatal: bad revision"
        )):
            result = git._count_lines_changed("/tmp", "   ")
        assert result == (0, 0, 0)


class TestParseShortstatEdgeCases:
    """Additional edge case tests for _parse_shortstat()."""

    def test_multiple_insertion_matches_uses_first(self) -> None:
        """If text somehow has multiple insertion matches, first is used."""
        text = " 1 file changed, 5 insertions(+), 10 insertions(+)"
        assert git._parse_shortstat(text) == (5, 0)

    def test_very_large_insertion_count(self) -> None:
        """Ensure parsing handles very large numbers without overflow."""
        text = f" 1 file changed, {2**31} insertions(+)"
        assert git._parse_shortstat(text) == (2**31, 0)

    def test_negative_number_before_insertion(self) -> None:
        """\\d+ matches digits after the minus sign (git never outputs negatives)."""
        assert git._parse_shortstat(" 1 file changed, -5 insertions(+)") == (5, 0)

    def test_decimal_number_partial_match(self) -> None:
        """'1.5 insertions' — regex matches '5' as the digits before ' insertion'."""
        result = git._parse_shortstat(" 1 file changed, 1.5 insertions(+)")
        assert result == (5, 0)

    def test_no_space_before_insertion(self) -> None:
        """Without a space before 'insertion', the regex might not match."""
        result = git._parse_shortstat(" 1 file changed,5 insertions(+)")
        assert result == (5, 0)


class TestParseShortstatLocalized:
    """Tests verifying _parse_shortstat fails on non-English git output.

    These document why _git_run forces LC_ALL=C: without it, localized
    git --shortstat output would cause convergence detection to silently
    report zero lines changed.
    """

    def test_german_locale_returns_zero_without_fix(self) -> None:
        """German git output uses 'Einfügungen' and 'Löschung', not matched by regex."""
        german = " 3 Dateien geändert, 20 Einfügungen(+), 10 Löschungen(-)"
        assert git._parse_shortstat(german) == (0, 0)

    def test_french_locale_returns_zero_without_fix(self) -> None:
        """French git output uses 'insertions' (matches!) but 'suppressions' (doesn't)."""
        french = " 3 fichiers modifiés, 20 insertions(+), 10 suppressions(-)"
        # 'insertions' matches the English regex, but 'suppressions' does not
        assert git._parse_shortstat(french) == (20, 0)  # only insertions matched

    def test_japanese_locale_returns_zero_without_fix(self) -> None:
        """Japanese git output uses completely different characters."""
        japanese = " 3個のファイル変更, 20行追加(+), 10行削除(-)"
        assert git._parse_shortstat(japanese) == (0, 0)


class TestGitRunLocaleEnv:
    """Tests verifying _git_run passes LC_ALL=C to git subprocesses."""

    def test_git_run_passes_lc_all_c(self) -> None:
        """_git_run should set LC_ALL=C in the subprocess environment."""
        with mock.patch("subprocess.run", return_value=make_git_result()) as mock_run:
            git._git_run("/tmp", "status")
        call_kwargs = mock_run.call_args[1]
        assert "env" in call_kwargs
        assert call_kwargs["env"].get("LC_ALL") == "C"

    def test_git_env_preserves_path(self) -> None:
        """The git env should preserve PATH and other important variables."""
        import os
        assert "PATH" in git._GIT_ENV
        assert git._GIT_ENV["PATH"] == os.environ.get("PATH", "")

    def test_git_env_overrides_lc_all(self) -> None:
        """LC_ALL=C should be set regardless of parent environment."""
        assert git._GIT_ENV["LC_ALL"] == "C"


class TestGetChangedFilesEdgeCases:
    """Edge cases for get_changed_files with empty/whitespace base_ref."""

    def test_empty_base_ref_returns_empty(self) -> None:
        assert git.get_changed_files("/tmp", "") == []

    def test_whitespace_base_ref_returns_empty(self) -> None:
        assert git.get_changed_files("/tmp", "   ") == []

    def test_none_like_empty_base_ref(self) -> None:
        """An empty string base_ref should not invoke git merge-base."""
        with mock.patch.object(git, "_git_stdout") as mock_stdout:
            result = git.get_changed_files("/tmp", "")
            mock_stdout.assert_not_called()
            assert result == []


class TestGitCommitAllUnstagePatterns:
    """Tests verifying that git_commit_all unstages checkloop's own files."""

    def test_unstages_checkloop_files_after_add(self) -> None:
        """git_commit_all should run git reset to unstage checkloop files after git add -A."""
        num_patterns = len(git._CHECKLOOP_UNSTAGE_PATTERNS)
        with mock.patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_git_result(),                                   # git add -A
                *[make_git_result() for _ in range(num_patterns)],   # git reset per pattern
                make_git_result(stdout=""),                          # diff --cached --name-only -z (no artifacts)
                make_git_result(returncode=1),                       # git diff --cached --quiet
                make_git_result(),                                   # git commit
                make_git_result(stdout="abc123\n"),                  # git rev-parse HEAD
            ]
            git.git_commit_all("/tmp", "test commit")
        # First call is git add -A (no pathspec excludes)
        add_call_args = mock_run.call_args_list[0][0][0]
        assert add_call_args[-1] == "-A"
        # Next calls are git reset for each pattern
        for i, pattern in enumerate(git._CHECKLOOP_UNSTAGE_PATTERNS):
            reset_args = mock_run.call_args_list[1 + i][0][0]
            assert "reset" in reset_args
            assert pattern in reset_args

    def test_unstage_patterns_constant_is_nonempty(self) -> None:
        """The unstage patterns list should contain at least the log and checkpoint files."""
        assert len(git._CHECKLOOP_UNSTAGE_PATTERNS) >= 2
        patterns_str = " ".join(git._CHECKLOOP_UNSTAGE_PATTERNS)
        assert "checkloop-run.log" in patterns_str
        assert "checkloop-checkpoint.json" in patterns_str


class TestGitRunTimeout:
    """Tests for _git_run timeout handling."""

    def test_timeout_raises_oserror(self) -> None:
        """When git times out, _git_run raises OSError with a descriptive message."""
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=120)):
            with pytest.raises(OSError, match="timed out"):
                git._git_run("/tmp", "status")


# =============================================================================
# has_uncommitted_changes
# =============================================================================

class TestHasUncommittedChanges:
    """Tests for has_uncommitted_changes()."""

    def test_clean_working_tree(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value=""):
            assert git.has_uncommitted_changes("/tmp") is False

    def test_dirty_working_tree(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value=" M foo.py\n?? bar.py"):
            assert git.has_uncommitted_changes("/tmp") is True

    def test_git_failure_returns_false(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value=None):
            assert git.has_uncommitted_changes("/tmp") is False


# =============================================================================
# get_uncommitted_diff
# =============================================================================

class TestGetUncommittedDiff:
    """Tests for get_uncommitted_diff()."""

    def test_returns_diff_from_head(self) -> None:
        def side_effect(workdir: str, *args: str) -> str | None:
            if args[:2] == ("diff", "HEAD"):
                return "diff content"
            # ls-files --others returns nothing (no untracked files).
            return ""
        with mock.patch.object(git, "_git_stdout", side_effect=side_effect):
            assert git.get_uncommitted_diff("/tmp") == "diff content"

    def test_falls_back_to_cached_when_no_head(self) -> None:
        def side_effect(workdir: str, *args: str) -> str | None:
            if args[:2] == ("diff", "HEAD"):
                return None
            if args[:2] == ("diff", "--cached"):
                return "staged diff"
            return ""  # no untracked files
        with mock.patch.object(git, "_git_stdout", side_effect=side_effect):
            assert git.get_uncommitted_diff("/tmp") == "staged diff"

    def test_returns_empty_when_no_changes_anywhere(self) -> None:
        def side_effect(workdir: str, *args: str) -> str | None:
            if args[:2] == ("diff", "HEAD"):
                return ""
            return ""  # no untracked either
        with mock.patch.object(git, "_git_stdout", side_effect=side_effect):
            assert git.get_uncommitted_diff("/tmp") == ""

    def test_includes_untracked_files_in_diff(self) -> None:
        """Untracked files are rendered via git diff --no-index so the generator sees them."""
        def stdout_side_effect(workdir: str, *args: str) -> str | None:
            if args[:2] == ("diff", "HEAD"):
                return ""  # no tracked modifications
            if args[0] == "ls-files":
                # Null-separated list of untracked paths
                return "new_file.py\0another.py"
            return None

        def run_side_effect(workdir: str, *args: str, **kwargs: object) -> mock.MagicMock:
            # _git_run is called by _untracked_files_as_diff for each untracked file.
            # Each returns a synthetic new-file patch.
            path = args[-1]
            return make_git_result(
                returncode=1,
                stdout=f"diff --git a/{path} b/{path}\nnew file mode 100644\n+content of {path}\n",
            )

        with (
            mock.patch.object(git, "_git_stdout", side_effect=stdout_side_effect),
            mock.patch.object(git, "_git_run", side_effect=run_side_effect),
        ):
            diff = git.get_uncommitted_diff("/tmp")

        assert "new_file.py" in diff
        assert "another.py" in diff

    def test_combines_tracked_and_untracked(self) -> None:
        """When both tracked changes and untracked files exist, both show up."""
        def stdout_side_effect(workdir: str, *args: str) -> str | None:
            if args[:2] == ("diff", "HEAD"):
                return "tracked-diff-content"
            if args[0] == "ls-files":
                return "newfile.py"
            return None

        def run_side_effect(workdir: str, *args: str, **kwargs: object) -> mock.MagicMock:
            return make_git_result(returncode=1, stdout="UNTRACKED-PATCH\n")

        with (
            mock.patch.object(git, "_git_stdout", side_effect=stdout_side_effect),
            mock.patch.object(git, "_git_run", side_effect=run_side_effect),
        ):
            diff = git.get_uncommitted_diff("/tmp")

        assert "tracked-diff-content" in diff
        assert "UNTRACKED-PATCH" in diff

    def test_caps_untracked_files_shown(self) -> None:
        """When many untracked files exist, only the first N are expanded."""
        many = "\0".join(f"f{i}.py" for i in range(git._MAX_UNTRACKED_FILES_IN_DIFF + 10))

        def stdout_side_effect(workdir: str, *args: str) -> str | None:
            if args[:2] == ("diff", "HEAD"):
                return ""
            if args[0] == "ls-files":
                return many
            return None

        calls = []

        def run_side_effect(workdir: str, *args: str, **kwargs: object) -> mock.MagicMock:
            calls.append(args[-1])
            return make_git_result(returncode=1, stdout=f"+{args[-1]}\n")

        with (
            mock.patch.object(git, "_git_stdout", side_effect=stdout_side_effect),
            mock.patch.object(git, "_git_run", side_effect=run_side_effect),
        ):
            diff = git.get_uncommitted_diff("/tmp")

        assert len(calls) == git._MAX_UNTRACKED_FILES_IN_DIFF
        assert "more untracked files omitted" in diff


class TestLooksLikeArtifact:
    """Tests for _looks_like_artifact() path classification."""

    def test_coverage_html_matches(self) -> None:
        assert git._looks_like_artifact("search-ui/coverage/index.html")

    def test_htmlcov_matches(self) -> None:
        assert git._looks_like_artifact("htmlcov/index.html")

    def test_dist_matches(self) -> None:
        assert git._looks_like_artifact("packages/foo/dist/bundle.js")

    def test_build_matches(self) -> None:
        assert git._looks_like_artifact("src/build/output.o")

    def test_next_matches(self) -> None:
        assert git._looks_like_artifact("apps/web/.next/static/chunks/abc.js")

    def test_node_modules_matches(self) -> None:
        assert git._looks_like_artifact("node_modules/react/index.js")

    def test_pycache_matches(self) -> None:
        assert git._looks_like_artifact("pkg/__pycache__/module.cpython-312.pyc")

    def test_coverage_filename_matches(self) -> None:
        assert git._looks_like_artifact(".coverage")
        assert git._looks_like_artifact(".coverage.hostname.12345")
        assert git._looks_like_artifact("coverage.xml")
        assert git._looks_like_artifact("lcov.info")

    def test_source_file_does_not_match(self) -> None:
        assert not git._looks_like_artifact("src/checkloop/git.py")

    def test_word_coverage_in_filename_does_not_match(self) -> None:
        """A file named coverage_report.py in src/ should not match (no /coverage/ path)."""
        assert not git._looks_like_artifact("src/tests/coverage_report.py")

    def test_backslash_paths_normalized(self) -> None:
        """Windows-style paths are accepted (normalized to forward slashes)."""
        assert git._looks_like_artifact("search-ui\\coverage\\index.html")


class TestUnstageGeneratedArtifacts:
    """Tests for _unstage_generated_artifacts() cleanup."""

    def test_unstages_artifact_paths(self) -> None:
        staged = "search-ui/coverage/foo.html\0src/real.py\0htmlcov/index.html"
        calls: list[tuple[str, ...]] = []

        def stdout_side_effect(workdir: str, *args: str) -> str | None:
            if args[:3] == ("diff", "--cached", "--name-only"):
                return staged
            return None

        def run_side_effect(workdir: str, *args: str, **kwargs: object) -> mock.MagicMock:
            calls.append(args)
            return make_git_result()

        with (
            mock.patch.object(git, "_git_stdout", side_effect=stdout_side_effect),
            mock.patch.object(git, "_git_run", side_effect=run_side_effect),
        ):
            unstaged = git._unstage_generated_artifacts("/tmp")

        assert "search-ui/coverage/foo.html" in unstaged
        assert "htmlcov/index.html" in unstaged
        assert "src/real.py" not in unstaged
        # Ensure git rm --cached was called with both artifact paths and not the source file.
        assert len(calls) == 1
        rm_args = calls[0]
        assert "rm" in rm_args
        assert "--cached" in rm_args
        assert "--ignore-unmatch" in rm_args
        assert "search-ui/coverage/foo.html" in rm_args
        assert "htmlcov/index.html" in rm_args
        assert "src/real.py" not in rm_args

    def test_no_staged_files_returns_empty(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value=""):
            assert git._unstage_generated_artifacts("/tmp") == []

    def test_no_artifacts_among_staged(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value="src/a.py\0src/b.py"):
            with mock.patch.object(git, "_git_run") as mock_run:
                assert git._unstage_generated_artifacts("/tmp") == []
                mock_run.assert_not_called()

    def test_oserror_returns_empty(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value="a/coverage/x"):
            with mock.patch.object(git, "_git_run", side_effect=OSError("disk full")):
                assert git._unstage_generated_artifacts("/tmp") == []


class TestCurrentBranchName:
    """Tests for current_branch_name()."""

    def test_returns_branch(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value="main"):
            assert git.current_branch_name("/tmp") == "main"

    def test_none_when_detached(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value=None):
            assert git.current_branch_name("/tmp") is None


class TestBranchExists:
    """Tests for branch_exists()."""

    def test_true_when_ref_resolves(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value="abc123"):
            assert git.branch_exists("/tmp", "checkloop/run-x") is True

    def test_false_when_ref_missing(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value=None):
            assert git.branch_exists("/tmp", "missing") is False


class TestCheckoutBranch:
    """Tests for checkout_branch()."""

    def test_returns_true_on_success(self) -> None:
        with mock.patch.object(git, "_git_run", return_value=make_git_result()):
            assert git.checkout_branch("/tmp", "main") is True

    def test_returns_false_on_called_process_error(self) -> None:
        err = subprocess.CalledProcessError(1, ["git", "checkout", "main"])
        with mock.patch.object(git, "_git_run", side_effect=err):
            assert git.checkout_branch("/tmp", "main") is False

    def test_returns_false_on_oserror(self) -> None:
        with mock.patch.object(git, "_git_run", side_effect=OSError("no git")):
            assert git.checkout_branch("/tmp", "main") is False


class TestCreateScratchBranch:
    """Tests for create_scratch_branch()."""

    def test_returns_branch_info_on_success(self) -> None:
        with mock.patch.object(git, "git_head_sha", return_value="abc1234567890"), \
             mock.patch.object(git, "current_branch_name", return_value="main"), \
             mock.patch.object(git, "_git_run", return_value=make_git_result()):
            info = git.create_scratch_branch("/tmp")
        assert info is not None
        branch, base, original = info
        assert branch.startswith("checkloop/run-")
        assert branch.endswith("-abc1234")
        assert base == "abc1234567890"
        assert original == "main"

    def test_returns_none_when_no_head_sha(self) -> None:
        with mock.patch.object(git, "git_head_sha", return_value=None):
            assert git.create_scratch_branch("/tmp") is None

    def test_returns_none_when_checkout_fails(self) -> None:
        err = subprocess.CalledProcessError(1, ["git", "checkout", "-b"])
        with mock.patch.object(git, "git_head_sha", return_value="abc1234"), \
             mock.patch.object(git, "current_branch_name", return_value="main"), \
             mock.patch.object(git, "_git_run", side_effect=err):
            assert git.create_scratch_branch("/tmp") is None

    def test_detached_head_original_branch_is_none(self) -> None:
        with mock.patch.object(git, "git_head_sha", return_value="abc1234"), \
             mock.patch.object(git, "current_branch_name", return_value=None), \
             mock.patch.object(git, "_git_run", return_value=make_git_result()):
            info = git.create_scratch_branch("/tmp")
        assert info is not None
        assert info[2] is None


class TestCountCommitsBetween:
    """Tests for count_commits_between()."""

    def test_returns_count(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value="3"):
            assert git.count_commits_between("/tmp", "abc123") == 3

    def test_zero_when_no_base_sha(self) -> None:
        assert git.count_commits_between("/tmp", "") == 0

    def test_zero_on_git_failure(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value=None):
            assert git.count_commits_between("/tmp", "abc123") == 0

    def test_zero_on_unparseable_output(self) -> None:
        with mock.patch.object(git, "_git_stdout", return_value="not-a-number"):
            assert git.count_commits_between("/tmp", "abc123") == 0
