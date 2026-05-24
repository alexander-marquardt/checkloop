"""Extract binding rules from the target project's standards files for prompt injection.

The motivating failure mode: check prompts already tell the agent to read
``CLAUDE.md`` / ``AGENTS.md`` / ``CONTRIBUTING.md``, but in practice the agent
does not always do so — and when it skips them, the project-specific rules
(no-AI-attribution, test-for-every-behaviour-change, no-net-neutral-churn,
proprietary-data scoping) go unhonored.  This module reads those files at run
start and packages them for inclusion at the very top of every check prompt,
so the rules are physically present in the agent's context without depending
on a side-quest read.

Public surface:

* :func:`load_project_rules` — read rule files in *workdir* and return formatted text
  ready to prepend to a check prompt, or ``""`` if no rule files are present.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Priority order. CLAUDE.md and AGENTS.md are agent-targeted and usually terse
# (the project author wrote them specifically for an AI/agent reader), so they
# come first in the injected text.  CONTRIBUTING.md is human-targeted but the
# rules apply equally and the agent should obey them.
_RULE_FILES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md")

# Per-file cap.  Most projects' rule files fit comfortably in 8 KB; large
# CONTRIBUTING.md files (PRISM's is ~12 KB) get truncated, with the first 8 KB
# preserved — that's where the most binding sections typically live (overview,
# push policy, commit messages, testing) and where the project author put them
# for visibility reasons.  Going larger risks bloating every check's prompt
# without proportional value.
_MAX_PER_FILE_CHARS = 8000

_HEADER = (
    "=========================================================================\n"
    "PROJECT-SPECIFIC RULES\n"
    "The text below is extracted verbatim from this project's standards files.\n"
    "These rules are binding and OVERRIDE any generic guidance later in this\n"
    "prompt. Read them before making any change.\n"
    "=========================================================================\n\n"
)

_FOOTER = (
    "\n=========================================================================\n"
    "END OF PROJECT-SPECIFIC RULES — generic check guidance follows\n"
    "=========================================================================\n\n"
)

_TRUNCATION_MARKER = (
    "\n\n... (file truncated for prompt budget; "
    "read the file directly for the remainder)\n"
)


def load_project_rules(workdir: str) -> str:
    """Read the target project's rule files and format them for prompt injection.

    Looks for :data:`_RULE_FILES` at the root of *workdir*, in priority order.
    Each file found is included verbatim, capped at :data:`_MAX_PER_FILE_CHARS`
    characters with a truncation marker if the file is larger.  Sections are
    separated by clear ``--- <filename> ---`` headers so the agent can tell
    which file each rule came from.  Returns the empty string when no rule
    files are present, so callers can concatenate the result unconditionally.

    Files that exist but cannot be read (permissions, encoding error) are
    logged at WARNING level and skipped — a missing rule file is better than
    a crashed run.
    """
    sections: list[str] = []
    for name in _RULE_FILES:
        path = Path(workdir) / name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s: %s", path, exc)
            continue
        if len(content) > _MAX_PER_FILE_CHARS:
            content = content[:_MAX_PER_FILE_CHARS].rstrip() + _TRUNCATION_MARKER
        sections.append(f"--- {name} ---\n\n{content.strip()}")
    if not sections:
        return ""
    body = "\n\n".join(sections)
    return _HEADER + body + _FOOTER
