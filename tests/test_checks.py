"""Tests for checkloop.checks — check definitions, tiers, and danger guard."""

from __future__ import annotations

from checkloop import checks


class TestConstants:
    """Tests for module-level constants and data structures."""

    def test_check_ids_match(self) -> None:
        assert checks.CHECK_IDS == [p["id"] for p in checks.CHECKS]

    def test_default_tier_passes_are_valid(self) -> None:
        for check_id in checks.TIERS[checks.DEFAULT_TIER]:
            assert check_id in checks.CHECK_IDS

    def test_all_checks_have_required_keys(self) -> None:
        for p in checks.CHECKS:
            assert "id" in p
            assert "label" in p
            assert "prompt" in p


class TestLooksDangerous:
    """Tests for the _looks_dangerous() prompt safety guard."""

    def test_safe_prompt(self) -> None:
        assert checks._looks_dangerous("Review all code for quality") is False

    def test_rm_rf_root(self) -> None:
        assert checks._looks_dangerous("rm -rf /") is True

    def test_case_insensitive(self) -> None:
        assert checks._looks_dangerous("DROP DATABASE users") is True

    def test_drop_table(self) -> None:
        assert checks._looks_dangerous("drop table foo") is True

    def test_sudo_rm(self) -> None:
        assert checks._looks_dangerous("sudo rm something") is True

    def test_fork_bomb(self) -> None:
        assert checks._looks_dangerous(":(){:|:&};:") is True

    def test_dd_dev_zero(self) -> None:
        assert checks._looks_dangerous("dd if=/dev/zero of=/dev/sda") is True

    def test_chmod_777_root(self) -> None:
        assert checks._looks_dangerous("chmod 777 /") is True

    def test_delete_all_files(self) -> None:
        assert checks._looks_dangerous("delete all files now") is True
        assert checks._looks_dangerous("delete all records") is False

    def test_truncate_table(self) -> None:
        assert checks._looks_dangerous("truncate table users") is True
        assert checks._looks_dangerous("truncate the string") is False

    def test_format_keyword(self) -> None:
        assert checks._looks_dangerous("format c: drive") is True
        assert checks._looks_dangerous("format /dev/sda") is True

    def test_wipe(self) -> None:
        assert checks._looks_dangerous("wipe disk now") is True
        assert checks._looks_dangerous("wipe drive") is True
        assert checks._looks_dangerous("wipe partition") is True
        assert checks._looks_dangerous("wipe the cache") is False

    def test_etc_passwd(self) -> None:
        assert checks._looks_dangerous("cat /etc/passwd") is True

    def test_embedded_safe_word(self) -> None:
        assert checks._looks_dangerous("run mkfs on disk") is True
        assert checks._looks_dangerous("format the code") is False

    def test_format_dev(self) -> None:
        assert checks._looks_dangerous("format /dev/sda") is True

    def test_dd_of_dev(self) -> None:
        assert checks._looks_dangerous("dd of=/dev/sda bs=1M") is True

    def test_empty_string(self) -> None:
        assert checks._looks_dangerous("") is False

    def test_whitespace_only(self) -> None:
        assert checks._looks_dangerous("   \t\n  ") is False

    def test_unicode_prompt(self) -> None:
        assert checks._looks_dangerous("レビューコード 🔍") is False


class TestLooksDangerousEdgeCases:
    """Additional edge case tests for _looks_dangerous()."""

    def test_very_long_safe_string(self) -> None:
        assert checks._looks_dangerous("a" * 100_000) is False

    def test_null_character_in_string(self) -> None:
        assert checks._looks_dangerous("safe\x00text") is False

    def test_newlines_around_keyword(self) -> None:
        assert checks._looks_dangerous("something\nrm -rf /\nsomething") is True

    def test_tabs_around_keyword(self) -> None:
        assert checks._looks_dangerous("\t\tdd if=/dev/zero\t\t") is True

    def test_mixed_case_drop_database(self) -> None:
        assert checks._looks_dangerous("DrOp DaTaBaSe users") is True


class TestCompileDangerPatternsEdgeCases:
    """Edge case tests for _compile_danger_patterns()."""

    def test_empty_keyword_skipped(self) -> None:
        original = checks._DANGEROUS_PROMPT_KEYWORDS[:]
        try:
            checks._DANGEROUS_PROMPT_KEYWORDS.insert(0, "")
            patterns = checks._compile_danger_patterns()
            assert len(patterns) == len(checks._DANGEROUS_PROMPT_KEYWORDS) - 1
        finally:
            checks._DANGEROUS_PROMPT_KEYWORDS[:] = original
