"""Tests for checkloop.checks — check definitions, tiers, and danger guard."""

from __future__ import annotations

from checkloop import checks


class TestConstants:
    """Tests for module-level constants and data structures."""

    def test_check_ids_match(self) -> None:
        assert checks.CHECK_IDS == [check["id"] for check in checks.CHECKS]

    def test_default_tier_checks_are_valid(self) -> None:
        for check_id in checks.TIERS[checks.DEFAULT_TIER]:
            assert check_id in checks.CHECK_IDS

    def test_all_checks_have_required_keys(self) -> None:
        for check in checks.CHECKS:
            assert "id" in check
            assert "label" in check
            assert "prompt" in check


class TestPerfCheckPrompt:
    """Tests for the perf check prompt content."""

    def test_mentions_n_plus_1_queries(self) -> None:
        perf_check = next(c for c in checks.CHECKS if c["id"] == "perf")
        assert "N+1 queries" in perf_check["prompt"]

    def test_mentions_quadratic_algorithms(self) -> None:
        perf_check = next(c for c in checks.CHECKS if c["id"] == "perf")
        assert "O(N²)" in perf_check["prompt"]


class TestLooksDangerous:
    """Tests for the looks_dangerous() prompt safety guard."""

    def test_safe_prompt(self) -> None:
        assert checks.looks_dangerous("Review all code for quality") is False

    def test_rm_rf_root(self) -> None:
        assert checks.looks_dangerous("rm -rf /") is True

    def test_case_insensitive(self) -> None:
        assert checks.looks_dangerous("DROP DATABASE users") is True

    def test_drop_table(self) -> None:
        assert checks.looks_dangerous("drop table foo") is True

    def test_sudo_rm(self) -> None:
        assert checks.looks_dangerous("sudo rm something") is True

    def test_fork_bomb(self) -> None:
        assert checks.looks_dangerous(":(){:|:&};:") is True

    def test_dd_dev_zero(self) -> None:
        assert checks.looks_dangerous("dd if=/dev/zero of=/dev/sda") is True

    def test_chmod_777_root(self) -> None:
        assert checks.looks_dangerous("chmod 777 /") is True

    def test_delete_all_files(self) -> None:
        assert checks.looks_dangerous("delete all files now") is True
        assert checks.looks_dangerous("delete all records") is False

    def test_truncate_table(self) -> None:
        assert checks.looks_dangerous("truncate table users") is True
        assert checks.looks_dangerous("truncate the string") is False

    def test_format_keyword(self) -> None:
        assert checks.looks_dangerous("format c: drive") is True
        assert checks.looks_dangerous("format /dev/sda") is True

    def test_wipe(self) -> None:
        assert checks.looks_dangerous("wipe disk now") is True
        assert checks.looks_dangerous("wipe drive") is True
        assert checks.looks_dangerous("wipe partition") is True
        assert checks.looks_dangerous("wipe the cache") is False

    def test_etc_passwd(self) -> None:
        assert checks.looks_dangerous("cat /etc/passwd") is True

    def test_embedded_safe_word(self) -> None:
        assert checks.looks_dangerous("run mkfs on disk") is True
        assert checks.looks_dangerous("format the code") is False

    def test_format_dev(self) -> None:
        assert checks.looks_dangerous("format /dev/sda") is True

    def test_dd_of_dev(self) -> None:
        assert checks.looks_dangerous("dd of=/dev/sda bs=1M") is True

    def test_empty_string(self) -> None:
        assert checks.looks_dangerous("") is False

    def test_whitespace_only(self) -> None:
        assert checks.looks_dangerous("   \t\n  ") is False

    def test_unicode_prompt(self) -> None:
        assert checks.looks_dangerous("レビューコード 🔍") is False


class TestLooksDangerousEdgeCases:
    """Additional edge case tests for looks_dangerous()."""

    def test_very_long_safe_string(self) -> None:
        assert checks.looks_dangerous("a" * 100_000) is False

    def test_null_character_in_string(self) -> None:
        assert checks.looks_dangerous("safe\x00text") is False

    def test_newlines_around_keyword(self) -> None:
        assert checks.looks_dangerous("something\nrm -rf /\nsomething") is True

    def test_tabs_around_keyword(self) -> None:
        assert checks.looks_dangerous("\t\tdd if=/dev/zero\t\t") is True

    def test_mixed_case_drop_database(self) -> None:
        assert checks.looks_dangerous("DrOp DaTaBaSe users") is True


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


class TestLooksDangerousAdditional:
    """Additional edge cases for looks_dangerous()."""

    def test_keyword_at_start_of_string(self) -> None:
        assert checks.looks_dangerous("rm -rf / now") is True

    def test_keyword_at_end_of_string(self) -> None:
        assert checks.looks_dangerous("please rm -rf /") is True

    def test_multiple_dangerous_keywords(self) -> None:
        """String with multiple dangerous keywords should still be detected."""
        assert checks.looks_dangerous("rm -rf / and drop database") is True

    def test_partial_keyword_not_detected(self) -> None:
        """'format' alone should not match if not followed by matching pattern."""
        assert checks.looks_dangerous("reformat the code") is False

    def test_keyword_surrounded_by_punctuation(self) -> None:
        assert checks.looks_dangerous("(rm -rf /)") is True

    def test_very_long_string_with_keyword_at_end(self) -> None:
        """Keyword buried at the end of a long string should still be found."""
        padding = "safe text " * 10000
        assert checks.looks_dangerous(padding + "rm -rf /") is True


class TestTierConsistency:
    """Tests for tier configuration consistency."""

    def test_all_tier_ids_are_valid(self) -> None:
        """Every check ID in every tier must exist in CHECK_IDS."""
        for tier_name, tier_ids in checks.TIERS.items():
            for check_id in tier_ids:
                assert check_id in checks.CHECK_IDS, f"{check_id} from tier {tier_name} not in CHECK_IDS"

    def test_exhaustive_tier_includes_all_checks(self) -> None:
        """The exhaustive tier should include every defined check."""
        assert set(checks.TIER_EXHAUSTIVE) == set(checks.CHECK_IDS)

    def test_basic_is_subset_of_thorough(self) -> None:
        assert set(checks.TIER_BASIC).issubset(set(checks.TIER_THOROUGH))

    def test_thorough_is_subset_of_exhaustive(self) -> None:
        assert set(checks.TIER_THOROUGH).issubset(set(checks.TIER_EXHAUSTIVE))

    def test_bookend_checks_in_all_tiers(self) -> None:
        """test-fix and test-validate should appear in all tiers."""
        for tier_name, tier_ids in checks.TIERS.items():
            assert "test-fix" in tier_ids, f"test-fix missing from {tier_name}"
            assert "test-validate" in tier_ids, f"test-validate missing from {tier_name}"

    def test_check_ids_have_unique_values(self) -> None:
        """No duplicate check IDs."""
        assert len(checks.CHECK_IDS) == len(set(checks.CHECK_IDS))

    def test_all_checks_have_nonempty_prompt(self) -> None:
        for check in checks.CHECKS:
            assert check["prompt"].strip(), f"Check {check['id']} has empty prompt"

    def test_all_checks_have_nonempty_label(self) -> None:
        for check in checks.CHECKS:
            assert check["label"].strip(), f"Check {check['id']} has empty label"
