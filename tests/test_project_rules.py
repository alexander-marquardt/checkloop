"""Tests for checkloop.project_rules — binding-rule extraction for prompt injection."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from checkloop import project_rules


class TestLoadProjectRules:
    """load_project_rules reads CLAUDE.md / AGENTS.md / CONTRIBUTING.md."""

    def test_empty_workdir_returns_empty_string(self, tmp_path: Path) -> None:
        assert project_rules.load_project_rules(str(tmp_path)) == ""

    def test_only_contributing_present(self, tmp_path: Path) -> None:
        body = "# Contributing\n\nRule: do not push to main without approval.\n"
        (tmp_path / "CONTRIBUTING.md").write_text(body)
        out = project_rules.load_project_rules(str(tmp_path))
        assert "PROJECT-SPECIFIC RULES" in out
        assert "--- CONTRIBUTING.md ---" in out
        assert "do not push to main without approval" in out
        assert "END OF PROJECT-SPECIFIC RULES" in out

    def test_only_claude_md_present(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# Claude rules\n\nNo AI-attribution lines.\n")
        out = project_rules.load_project_rules(str(tmp_path))
        assert "--- CLAUDE.md ---" in out
        assert "No AI-attribution lines" in out

    def test_priority_order_claude_first_then_agents_then_contributing(
        self, tmp_path: Path,
    ) -> None:
        # CLAUDE.md comes first (most agent-relevant), then AGENTS.md, then CONTRIBUTING.md.
        (tmp_path / "CLAUDE.md").write_text("CLAUDE-rules-marker\n")
        (tmp_path / "AGENTS.md").write_text("AGENTS-rules-marker\n")
        (tmp_path / "CONTRIBUTING.md").write_text("CONTRIBUTING-rules-marker\n")
        out = project_rules.load_project_rules(str(tmp_path))
        claude_pos = out.index("CLAUDE-rules-marker")
        agents_pos = out.index("AGENTS-rules-marker")
        contrib_pos = out.index("CONTRIBUTING-rules-marker")
        assert claude_pos < agents_pos < contrib_pos

    def test_all_three_filenames_appear_when_all_present(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text("a\n")
        (tmp_path / "AGENTS.md").write_text("b\n")
        (tmp_path / "CONTRIBUTING.md").write_text("c\n")
        out = project_rules.load_project_rules(str(tmp_path))
        assert "--- CLAUDE.md ---" in out
        assert "--- AGENTS.md ---" in out
        assert "--- CONTRIBUTING.md ---" in out

    def test_large_file_is_truncated_with_marker(self, tmp_path: Path) -> None:
        # Build a file larger than the per-file cap; the result must include
        # the truncation marker and stay under cap + footer + header overhead.
        big = "X" * (project_rules._MAX_PER_FILE_CHARS + 5000)
        (tmp_path / "CONTRIBUTING.md").write_text(big)
        out = project_rules.load_project_rules(str(tmp_path))
        assert "file truncated for prompt budget" in out
        # The truncated body section itself stays around the cap (not the full 13 KB).
        # We allow the header + truncation marker overhead.
        assert len(out) < project_rules._MAX_PER_FILE_CHARS + 1000

    def test_small_file_is_not_truncated(self, tmp_path: Path) -> None:
        small = "Just a short rule file.\n"
        (tmp_path / "CLAUDE.md").write_text(small)
        out = project_rules.load_project_rules(str(tmp_path))
        assert "file truncated" not in out
        assert "Just a short rule file." in out

    def test_unreadable_file_is_skipped(
        self, tmp_path: Path, caplog: "object",
    ) -> None:
        # Create a real file so the path exists, then make read_text raise OSError.
        path = tmp_path / "CLAUDE.md"
        path.write_text("ok\n")
        with mock.patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            out = project_rules.load_project_rules(str(tmp_path))
        # The file existed but couldn't be read; absent → empty result.
        assert out == ""

    def test_subdirectory_files_are_ignored(self, tmp_path: Path) -> None:
        # Files in subdirectories don't count — only the workdir root.
        sub = tmp_path / "docs"
        sub.mkdir()
        (sub / "CLAUDE.md").write_text("nested\n")
        assert project_rules.load_project_rules(str(tmp_path)) == ""

    def test_directory_named_like_rule_file_is_ignored(self, tmp_path: Path) -> None:
        # `is_file()` rejects directories — a `CLAUDE.md/` dir shouldn't trip the loader.
        (tmp_path / "CLAUDE.md").mkdir()
        assert project_rules.load_project_rules(str(tmp_path)) == ""

    def test_header_and_footer_present_when_any_rules_found(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "CLAUDE.md").write_text("body\n")
        out = project_rules.load_project_rules(str(tmp_path))
        assert out.startswith("====")
        assert "binding" in out.lower()
        assert "END OF PROJECT-SPECIFIC RULES" in out

    def test_content_is_stripped(self, tmp_path: Path) -> None:
        # Leading and trailing whitespace in the file body is collapsed.
        (tmp_path / "CLAUDE.md").write_text("\n\n\n  content  \n\n\n")
        out = project_rules.load_project_rules(str(tmp_path))
        assert "content" in out
        # No giant whitespace blob between the filename marker and the content.
        assert "--- CLAUDE.md ---\n\ncontent" in out
