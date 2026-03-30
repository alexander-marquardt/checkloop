"""Execution plan loading from TOML files.

Execution plans define which checks run and what model each check uses.
Three plans ship pre-populated (basic, thorough, exhaustive) as TOML files
in the ``execution_plans/`` directory at the project root.  Users can also
write their own plan files and point to them with ``--plan``.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlanCheckEntry:
    """A single check entry within a plan, specifying the check ID and model."""

    id: str
    model: str


@dataclass(frozen=True)
class PlanConfig:
    """Parsed execution plan from a TOML file."""

    name: str
    description: str
    checks: list[PlanCheckEntry]

    def check_ids(self) -> list[str]:
        """Return the ordered list of check IDs in this plan."""
        return [entry.id for entry in self.checks]

    def model_map(self) -> dict[str, str]:
        """Return a mapping of check ID to model name."""
        return {entry.id: entry.model for entry in self.checks}


# Pre-populated plan names, matching the TOML filenames in execution_plans/.
BUILTIN_PLAN_NAMES: list[str] = ["basic", "thorough", "exhaustive"]
DEFAULT_PLAN_NAME: str = "basic"


def _find_plans_dir() -> Path:
    """Locate the execution_plans directory.

    Checks two locations:
    1. Installed mode — ``execution_plans/`` is force-included inside the
       package directory during wheel build.
    2. Dev mode — ``execution_plans/`` lives at the repository root, which
       is three levels up from this file (``src/checkloop/tier_config.py``).
    """
    # Installed: <site-packages>/checkloop/execution_plans/
    pkg_dir = Path(__file__).parent / "execution_plans"
    if pkg_dir.is_dir():
        return pkg_dir
    # Dev: <repo>/execution_plans/  (this file is at <repo>/src/checkloop/)
    repo_dir = Path(__file__).parent.parent.parent / "execution_plans"
    if repo_dir.is_dir():
        return repo_dir
    raise FileNotFoundError(
        "Cannot find execution_plans directory. Expected it next to the "
        "package or at the repository root."
    )


def _parse_plan_toml(data: dict[str, object]) -> PlanConfig:
    """Parse a raw TOML dict into a PlanConfig."""
    tier_section = data.get("tier")
    if not isinstance(tier_section, dict):
        raise ValueError("Plan file must contain a [tier] section")

    name = tier_section.get("name", "")
    description = tier_section.get("description", "")
    if not isinstance(name, str) or not name:
        raise ValueError("[tier].name must be a non-empty string")
    if not isinstance(description, str):
        description = ""

    raw_checks = data.get("checks")
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("Plan file must contain at least one [[checks]] entry")

    entries: list[PlanCheckEntry] = []
    for item in raw_checks:
        if not isinstance(item, dict):
            raise ValueError("Each [[checks]] entry must be a table")
        check_id = item.get("id")
        model = item.get("model", "sonnet")
        if not isinstance(check_id, str) or not check_id:
            raise ValueError("Each [[checks]] entry must have a non-empty 'id'")
        if not isinstance(model, str) or not model:
            raise ValueError(f"Check '{check_id}' has invalid 'model' value")
        entries.append(PlanCheckEntry(id=check_id, model=model))

    return PlanConfig(name=name, description=description, checks=entries)


def load_builtin_plan(name: str) -> PlanConfig:
    """Load a pre-populated plan by name (basic, thorough, exhaustive)."""
    if name not in BUILTIN_PLAN_NAMES:
        raise ValueError(
            f"Unknown plan '{name}'. "
            f"Pre-populated plans: {', '.join(BUILTIN_PLAN_NAMES)}"
        )
    plans_dir = _find_plans_dir()
    toml_path = plans_dir / f"{name}.toml"
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    return _parse_plan_toml(data)


def load_plan_file(path: str) -> PlanConfig:
    """Load a plan from any TOML file path."""
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Plan file not found: {file_path}")
    with open(file_path, "rb") as f:
        data = tomllib.load(f)
    return _parse_plan_toml(data)


def load_all_builtin_plans() -> dict[str, PlanConfig]:
    """Load all pre-populated plans and return them keyed by name."""
    return {name: load_builtin_plan(name) for name in BUILTIN_PLAN_NAMES}
