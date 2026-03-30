"""Check definitions (loaded from checks/ directory), plan configuration, and dangerous-prompt safety guard."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

from checkloop.tier_config import (
    DEFAULT_PLAN_NAME,
    PlanConfig,
    load_all_builtin_plans,
)


class CheckDef(TypedDict):
    """A single check definition with its identifier, display label, and prompt.

    Attributes:
        id: Short identifier used on the CLI (e.g. ``"readability"``, ``"dry"``).
        label: Human-readable name shown in banners and summaries.
        prompt: The review prompt sent to Claude Code for this check.
    """

    id: str
    label: str
    prompt: str


# --- Check loading ------------------------------------------------------------
#
# Each check is a Markdown file in the checks/ directory with YAML frontmatter
# containing ``id`` and ``label``.  The body (everything after the closing
# ``---``) is the prompt text.

def _find_checks_dir() -> Path:
    """Locate the checks/ directory.

    Checks two locations:
    1. Installed mode — ``checks/`` is force-included inside the package
       directory during wheel build.
    2. Dev mode — ``checks/`` lives at the repository root, which is three
       levels up from this file (``src/checkloop/checks.py``).
    """
    pkg_dir = Path(__file__).parent / "checks"
    if pkg_dir.is_dir():
        return pkg_dir
    repo_dir = Path(__file__).parent.parent.parent / "checks"
    if repo_dir.is_dir():
        return repo_dir
    raise FileNotFoundError(
        "Cannot find checks directory. Expected it next to the "
        "package or at the repository root."
    )


def _parse_check_file(path: Path) -> CheckDef:
    """Parse a single check Markdown file into a CheckDef.

    Expects YAML frontmatter delimited by ``---`` lines, with ``id`` and
    ``label`` fields.  Everything after the closing ``---`` is the prompt.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"Check file {path.name} must start with '---' (YAML frontmatter)")

    # Split on the second '---' to separate frontmatter from body.
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"Check file {path.name} has malformed frontmatter (missing closing '---')")

    frontmatter = parts[1].strip()
    prompt = parts[2].strip()

    # Simple YAML parsing — only need id and label, both simple strings.
    check_id = ""
    label = ""
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("id:"):
            check_id = line[3:].strip().strip('"').strip("'")
        elif line.startswith("label:"):
            label = line[6:].strip().strip('"').strip("'")

    if not check_id:
        raise ValueError(f"Check file {path.name} missing 'id' in frontmatter")
    if not label:
        raise ValueError(f"Check file {path.name} missing 'label' in frontmatter")
    if not prompt:
        raise ValueError(f"Check file {path.name} has empty prompt body")

    return CheckDef(id=check_id, label=label, prompt=prompt)


def _load_all_checks() -> list[CheckDef]:
    """Load all check definitions from the checks/ directory.

    Returns checks ordered to match the exhaustive plan (which defines the
    canonical ordering for all checks).  Any checks not in the exhaustive
    plan are appended at the end in alphabetical order.
    """
    checks_dir = _find_checks_dir()
    checks_by_id: dict[str, CheckDef] = {}
    for md_file in sorted(checks_dir.glob("*.md")):
        check = _parse_check_file(md_file)
        checks_by_id[check["id"]] = check

    # Order by the exhaustive plan to maintain canonical check ordering.
    exhaustive = load_all_builtin_plans().get("exhaustive")
    if exhaustive:
        ordered_ids = exhaustive.check_ids()
    else:
        ordered_ids = sorted(checks_by_id.keys())

    result: list[CheckDef] = []
    seen: set[str] = set()
    for cid in ordered_ids:
        if cid in checks_by_id:
            result.append(checks_by_id[cid])
            seen.add(cid)
    # Append any checks not in the exhaustive plan.
    for cid in sorted(checks_by_id.keys()):
        if cid not in seen:
            result.append(checks_by_id[cid])
    return result


# The canonical ordered list of all available checks, loaded at import time.
CHECKS: list[CheckDef] = _load_all_checks()

# All valid check IDs, derived from CHECKS to stay in sync.
CHECK_IDS: list[str] = [check["id"] for check in CHECKS]

# Lookup by ID for fast access.
_CHECKS_BY_ID: dict[str, CheckDef] = {check["id"]: check for check in CHECKS}


def get_check_by_id(check_id: str) -> CheckDef | None:
    """Return the CheckDef for a given ID, or None if not found."""
    return _CHECKS_BY_ID.get(check_id)


# --- Execution plans ----------------------------------------------------------
# Plans are loaded from TOML files in the ``execution_plans/`` directory at
# the project root.  Each file defines the check IDs and per-check model.
# The constants below are derived from the pre-populated plans.

_BUILTIN_PLAN_CONFIGS: dict[str, PlanConfig] = load_all_builtin_plans()

# Checks that are only run when explicitly requested via --checks, never included in plans.
_ON_DEMAND_ONLY: set[str] = set()

# Public plan lists — derived from plan TOML files for programmatic access.
TIER_BASIC: list[str] = _BUILTIN_PLAN_CONFIGS["basic"].check_ids()
TIER_THOROUGH: list[str] = _BUILTIN_PLAN_CONFIGS["thorough"].check_ids()
TIER_EXHAUSTIVE: list[str] = _BUILTIN_PLAN_CONFIGS["exhaustive"].check_ids()

# Maps plan name to the list of check IDs.
TIERS: dict[str, list[str]] = {
    name: config.check_ids() for name, config in _BUILTIN_PLAN_CONFIGS.items()
}
DEFAULT_TIER: str = DEFAULT_PLAN_NAME

# Maps plan name to its full PlanConfig (including per-check models).
PLAN_CONFIGS: dict[str, PlanConfig] = _BUILTIN_PLAN_CONFIGS


# --- Prompt template loading --------------------------------------------------

def _find_prompt_templates_dir() -> Path:
    """Locate the prompt_templates/ directory."""
    pkg_dir = Path(__file__).parent / "prompt_templates"
    if pkg_dir.is_dir():
        return pkg_dir
    repo_dir = Path(__file__).parent.parent.parent / "prompt_templates"
    if repo_dir.is_dir():
        return repo_dir
    raise FileNotFoundError(
        "Cannot find prompt_templates directory. Expected it next to the "
        "package or at the repository root."
    )


def _load_prompt_template(filename: str) -> str:
    """Load a prompt template file and return its contents."""
    templates_dir = _find_prompt_templates_dir()
    path = templates_dir / filename
    if not path.is_file():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


FULL_CODEBASE_SCOPE: str = _load_prompt_template("full_codebase_scope.md") + " "
"""Default scope prefix prepended to every check when --changed-only is not used."""

COMMIT_MESSAGE_INSTRUCTIONS: str = _load_prompt_template("commit_message_instructions.md")
"""Instructions appended to every check prompt to enforce clean commit messages."""


# --- Dangerous-prompt guard ---------------------------------------------------
# Safety net: reject check prompts that contain destructive keywords.
# These are checked with word-boundary-aware regexes (see _compile_danger_patterns).

_DANGEROUS_PROMPT_KEYWORDS: list[str] = [
    "rm -rf /",
    "format c:",
    "format /dev",
    "mkfs",
    "wipe disk",
    "wipe drive",
    "wipe partition",
    "delete all files",
    "drop database",
    "drop table",
    "truncate table",
    ":(){:|:&};:",
    "sudo rm",
    "chmod 777 /",
    "/etc/passwd",
    "dd if=/dev/zero",
    "dd of=/dev",
]


def _compile_danger_patterns() -> list[re.Pattern[str]]:
    """Pre-compile regex patterns for all danger keywords.

    Adds word-boundary anchors (\\b) only at alphanumeric edges, so
    "reformat" won't match "format" but "/etc/passwd" still matches.
    """
    patterns: list[re.Pattern[str]] = []
    for keyword in _DANGEROUS_PROMPT_KEYWORDS:
        if not keyword:
            continue
        escaped = re.escape(keyword)
        leading_boundary = r"\b" if keyword[0].isalnum() else ""
        trailing_boundary = r"\b" if keyword[-1].isalnum() else ""
        patterns.append(re.compile(leading_boundary + escaped + trailing_boundary, re.IGNORECASE))
    return patterns


# Perf: compile once at import time instead of rebuilding on every call.
_DANGEROUS_PROMPT_PATTERNS: list[re.Pattern[str]] = _compile_danger_patterns()


def looks_dangerous(text: str) -> bool:
    """Check if a prompt contains any destructive keyword.

    Uses word-boundary anchors (\\b) around alphanumeric edges so e.g.
    "reformat" does not match "format", while keywords containing special
    characters like "rm -rf /" or "/etc/passwd" are still detected.
    """
    return any(pattern.search(text) for pattern in _DANGEROUS_PROMPT_PATTERNS)
