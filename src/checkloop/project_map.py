"""Project map generation and caching.

Generates a concise structural overview of the target project by sending the
file tree to Claude Code.  The map is cached at ``.checkloop-project-map.md``
in the workdir and regenerated when the set of tracked files changes (detected
via a hash of ``git ls-files`` output).  The map is prepended to every check
prompt so Claude doesn't waste time rediscovering the project layout.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import subprocess

from checkloop.process import SANITIZED_ENV

logger = logging.getLogger(__name__)

MAP_FILENAME = ".checkloop-project-map.md"

_GENERATION_TIMEOUT = 120  # seconds

_HASH_LINE_RE = re.compile(r"^<!-- checkloop-fingerprint: ([a-f0-9]+) -->$")

_MAP_PROMPT = """\
Here is the complete file tree of a software project:

```
{file_tree}
```

Write a concise project structure overview (max ~60 lines) that would help \
a code reviewer quickly understand this codebase. Include:

1. **What the project is** — language(s), framework(s), what it does (infer from file names and structure)
2. **Directory layout** — what lives where, briefly
3. **Key entry points** — main files, CLI entry points, app entry points
4. **Test setup** — where tests live, what framework (pytest, jest, etc.), how to run them
5. **Build / config** — notable config files (pyproject.toml, package.json, tsconfig, docker, CI, etc.)

Be factual, not speculative. Only describe what the file tree shows. Do NOT \
include any markdown fences around the whole response. Do NOT include a title \
or heading — start directly with the content. Keep it compact — this will be \
prepended to many prompts.
"""


def _compute_file_tree_fingerprint(workdir: str) -> str | None:
    """Hash the sorted list of tracked files to detect structural changes."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        files = sorted(result.stdout.strip().splitlines())
        return hashlib.sha256("\n".join(files).encode()).hexdigest()[:16]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("Could not compute file tree fingerprint: %s", exc)
        return None


def _read_cached_map(workdir: str) -> tuple[str | None, str | None]:
    """Read the cached map file and extract its fingerprint.

    Returns ``(fingerprint, map_body)`` or ``(None, None)`` if missing/unreadable.
    """
    map_path = os.path.join(workdir, MAP_FILENAME)
    try:
        with open(map_path, encoding="utf-8") as f:
            lines = f.readlines()
    except (FileNotFoundError, OSError):
        return None, None

    if not lines:
        return None, None

    match = _HASH_LINE_RE.match(lines[0].strip())
    if not match:
        return None, None

    fingerprint = match.group(1)
    body = "".join(lines[1:]).strip()
    return fingerprint, body


def _generate_map(
    workdir: str,
    *,
    skip_permissions: bool = False,
    model: str | None = None,
    claude_command: str = "claude",
) -> str | None:
    """Ask Claude to generate a project structure overview.

    Returns the map text, or None on failure.
    """
    # Build the file tree.
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning("git ls-files failed (rc=%d)", result.returncode)
            return None
        file_tree = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("Could not get file tree: %s", exc)
        return None

    if not file_tree:
        logger.info("No tracked files — skipping project map generation")
        return None

    prompt = _MAP_PROMPT.format(file_tree=file_tree)

    cmd = [claude_command]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if model:
        cmd += ["--model", model]
    cmd += ["-p", prompt]

    logger.info("Generating project map (file_count=%d)", file_tree.count("\n") + 1)
    try:
        result = subprocess.run(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_GENERATION_TIMEOUT,
            env=SANITIZED_ENV,
            start_new_session=True,  # prevent Ctrl+C from reaching this subprocess
        )
    except FileNotFoundError:
        shell = os.environ.get("SHELL", "/bin/sh")
        logger.info("Retrying map generation via %s -ic — %r not found on PATH", shell, cmd[0])
        try:
            result = subprocess.run(
                [shell, "-ic", shlex.join(cmd)],
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_GENERATION_TIMEOUT,
                env=SANITIZED_ENV,
                start_new_session=True,  # prevent Ctrl+C from reaching this subprocess
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Project map generation failed (shell retry): %s", exc)
            return None
    except subprocess.TimeoutExpired:
        logger.warning("Project map generation timed out after %ds", _GENERATION_TIMEOUT)
        return None
    except OSError as exc:
        logger.warning("Project map generation failed: %s", exc)
        return None

    if result.returncode != 0:
        logger.warning("Project map generation failed (rc=%d): %s",
                       result.returncode, result.stderr[:200])
        return None

    body = result.stdout.strip()
    if not body:
        logger.warning("Project map generation returned empty output")
        return None

    logger.info("Project map generated (%d chars)", len(body))
    return body


def _save_map(workdir: str, fingerprint: str, body: str) -> None:
    """Write the map file with its fingerprint header."""
    map_path = os.path.join(workdir, MAP_FILENAME)
    try:
        with open(map_path, "w", encoding="utf-8") as f:
            f.write(f"<!-- checkloop-fingerprint: {fingerprint} -->\n")
            f.write(body)
            f.write("\n")
        logger.info("Saved project map to %s", map_path)
    except OSError as exc:
        logger.warning("Could not save project map: %s", exc)


def ensure_project_map(
    workdir: str,
    *,
    skip_permissions: bool = False,
    model: str | None = None,
    claude_command: str = "claude",
) -> str:
    """Return the project map text, generating or regenerating as needed.

    Returns the map body (without the fingerprint header) for injection into
    check prompts, or an empty string if generation fails.
    """
    current_fp = _compute_file_tree_fingerprint(workdir)
    if current_fp is None:
        logger.info("Not a git repo or git unavailable — skipping project map")
        return ""

    cached_fp, cached_body = _read_cached_map(workdir)
    if cached_fp == current_fp and cached_body:
        logger.info("Project map is current (fingerprint=%s)", current_fp)
        return cached_body

    if cached_fp is not None:
        logger.info("Project map stale (cached=%s, current=%s) — regenerating", cached_fp, current_fp)
    else:
        logger.info("No cached project map — generating")

    body = _generate_map(
        workdir,
        skip_permissions=skip_permissions,
        model=model,
        claude_command=claude_command,
    )
    if body:
        _save_map(workdir, current_fp, body)
        return body

    # Generation failed — use stale cache if available.
    if cached_body:
        logger.info("Using stale project map as fallback")
        return cached_body

    return ""


def load_project_map(workdir: str) -> str:
    """Load the cached project map without validation or generation.

    Used for prompt injection when the map has already been ensured at suite
    start.  Returns empty string if missing.
    """
    _, body = _read_cached_map(workdir)
    return body or ""
