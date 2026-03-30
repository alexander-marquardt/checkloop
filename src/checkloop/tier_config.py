"""Tier configuration loading from TOML files.

Tiers define which checks run and what model each check uses.  Built-in
tiers (basic, thorough, exhaustive) ship as TOML files in the ``tiers/``
package directory.  Users can also supply custom tier files via
``--tier-file``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path


@dataclass(frozen=True)
class TierCheckEntry:
    """A single check entry within a tier, specifying the check ID and model."""

    id: str
    model: str


@dataclass(frozen=True)
class TierConfig:
    """Parsed tier configuration from a TOML file."""

    name: str
    description: str
    checks: list[TierCheckEntry]

    def check_ids(self) -> list[str]:
        """Return the ordered list of check IDs in this tier."""
        return [entry.id for entry in self.checks]

    def model_map(self) -> dict[str, str]:
        """Return a mapping of check ID to model name."""
        return {entry.id: entry.model for entry in self.checks}


# Built-in tier names, matching the TOML filenames in the tiers/ directory.
BUILTIN_TIER_NAMES: list[str] = ["basic", "thorough", "exhaustive"]
DEFAULT_TIER_NAME: str = "basic"


def _parse_tier_toml(data: dict[str, object]) -> TierConfig:
    """Parse a raw TOML dict into a TierConfig."""
    tier_section = data.get("tier")
    if not isinstance(tier_section, dict):
        raise ValueError("Tier file must contain a [tier] section")

    name = tier_section.get("name", "")
    description = tier_section.get("description", "")
    if not isinstance(name, str) or not name:
        raise ValueError("Tier [tier].name must be a non-empty string")
    if not isinstance(description, str):
        description = ""

    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("Tier file must contain at least one [[checks]] entry")

    entries: list[TierCheckEntry] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            raise ValueError("Each [[checks]] entry must be a table")
        check_id = item.get("id")
        model = item.get("model", "sonnet")
        if not isinstance(check_id, str) or not check_id:
            raise ValueError("Each [[checks]] entry must have a non-empty 'id'")
        if not isinstance(model, str) or not model:
            raise ValueError(f"Check '{check_id}' has invalid 'model' value")
        entries.append(TierCheckEntry(id=check_id, model=model))

    return TierConfig(name=name, description=description, checks=entries)


def load_builtin_tier(name: str) -> TierConfig:
    """Load a built-in tier by name (basic, thorough, exhaustive)."""
    if name not in BUILTIN_TIER_NAMES:
        raise ValueError(
            f"Unknown built-in tier '{name}'. "
            f"Available: {', '.join(BUILTIN_TIER_NAMES)}"
        )
    tier_files = resources.files("checkloop.tiers")
    toml_path = tier_files.joinpath(f"{name}.toml")
    toml_bytes = toml_path.read_bytes()
    data = tomllib.loads(toml_bytes.decode("utf-8"))
    return _parse_tier_toml(data)


def load_tier_file(path: str) -> TierConfig:
    """Load a custom tier from a TOML file path."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Tier file not found: {file_path}")
    with open(file_path, "rb") as f:
        data = tomllib.load(f)
    return _parse_tier_toml(data)


def load_all_builtin_tiers() -> dict[str, TierConfig]:
    """Load all built-in tiers and return them keyed by name."""
    return {name: load_builtin_tier(name) for name in BUILTIN_TIER_NAMES}
