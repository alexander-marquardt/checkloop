"""Commit message generation via Claude Code.

Asks Claude to write a concise commit message summarizing a git diff.
Uses plain-text output (no JSONL streaming) since this is a short,
tool-free request.  Separated from the streaming subprocess infrastructure
in ``process.py`` because it uses a simple synchronous ``subprocess.run``
call with no process-group management, timeout escalation, or RSS monitoring.
"""

from __future__ import annotations

import logging
import subprocess

from checkloop.process import _SANITIZED_ENV

logger = logging.getLogger(__name__)

_COMMIT_MSG_TIMEOUT = 60  # seconds to wait for Claude to generate a commit message


def generate_commit_message(
    diff_text: str, workdir: str, *, skip_permissions: bool = False,
) -> str | None:
    """Ask Claude to write a commit message summarizing a diff.

    Returns the generated message, or None on any failure.
    """
    prompt = (
        "Here is a git diff of uncommitted changes. Write a commit message for these changes.\n"
        "The commit message must be 2-3 sentences describing what was changed and why.\n"
        "Do NOT mention Claude, AI, checkloop, or any AI tools.\n"
        "Do NOT add Co-Authored-By or Signed-off-by trailers.\n"
        "Reply with ONLY the commit message text, nothing else.\n\n"
        f"```diff\n{diff_text}\n```"
    )
    cmd = ["claude"]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd += ["-p", prompt]
    logger.info("Generating commit message for uncommitted changes (diff_len=%d)", len(diff_text))
    try:
        result = subprocess.run(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_COMMIT_MSG_TIMEOUT,
            env=_SANITIZED_ENV,
        )
        if result.returncode != 0:
            logger.warning("Commit message generation failed (rc=%d): %s",
                           result.returncode, result.stderr[:200])
            return None
        message = result.stdout.strip()
        logger.info("Generated commit message: %s", message[:120])
        return message or None
    except subprocess.TimeoutExpired:
        logger.warning("Commit message generation timed out after %ds", _COMMIT_MSG_TIMEOUT)
        return None
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Failed to run claude for commit message generation: %s", exc)
        return None
