"""Tests for checkloop.tier_config — plan parsing, including per-check effort."""

from __future__ import annotations

import pytest

from checkloop import tier_config


def _plan(*check_tables: str) -> dict:
    """Build a minimal parsed-TOML dict with the given [[checks]] entries."""
    return {
        "tier": {"name": "t", "description": "d"},
        "checks": list(check_tables),
    }


class TestEffortParsing:
    """Tests for the per-check effort field in _parse_plan_toml/PlanConfig."""

    def test_effort_parsed_and_exposed(self) -> None:
        cfg = tier_config._parse_plan_toml(
            _plan({"id": "security", "model": "opus", "effort": "xhigh"})
        )
        assert cfg.checks[0].effort == "xhigh"
        assert cfg.effort_map() == {"security": "xhigh"}

    def test_effort_optional(self) -> None:
        cfg = tier_config._parse_plan_toml(_plan({"id": "dry", "model": "sonnet"}))
        assert cfg.checks[0].effort is None
        assert cfg.effort_map() == {}

    def test_effort_map_omits_checks_without_effort(self) -> None:
        cfg = tier_config._parse_plan_toml(
            _plan(
                {"id": "dry", "model": "sonnet"},
                {"id": "security", "model": "opus", "effort": "high"},
            )
        )
        assert cfg.effort_map() == {"security": "high"}

    @pytest.mark.parametrize("level", tier_config.VALID_EFFORT_LEVELS)
    def test_all_valid_levels_accepted(self, level: str) -> None:
        cfg = tier_config._parse_plan_toml(
            _plan({"id": "x", "model": "sonnet", "effort": level})
        )
        assert cfg.checks[0].effort == level

    def test_invalid_effort_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid 'effort'"):
            tier_config._parse_plan_toml(
                _plan({"id": "x", "model": "sonnet", "effort": "bogus"})
            )

    def test_builtin_plans_carry_expected_efforts(self) -> None:
        em = tier_config.load_builtin_plan("exhaustive").effort_map()
        assert em["architecture-boundaries"] == "xhigh"
        assert em["security"] == "xhigh"
        assert em["readability"] == "medium"
        assert em["test-fix"] == "high"
