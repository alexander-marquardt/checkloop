#!/usr/bin/env python3
"""
claudeloop — Autonomous multi-pass code review using Claude Code.

Runs a configurable suite of review passes (readability, DRY, tests, security,
etc.) over an existing codebase. Point it at a directory and walk away.

Usage:
    claudeloop                          # review current directory
    claudeloop --dir ~/my-project
    claudeloop --cycles 3               # repeat the full suite 3x
    claudeloop --passes readability dry tests
    claudeloop --all-passes --cycles 2
    claudeloop --dry-run                # preview without running
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# --- ANSI helpers -------------------------------------------------------------

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
BLUE   = "\033[94m"


def banner(msg: str, colour: str = CYAN) -> None:
    rule = "\u2500" * 72  # ─
    print(f"\n{colour}{BOLD}{rule}")
    print(f"  {msg}")
    print(f"{rule}{RESET}\n")


def log(msg: str, colour: str = DIM) -> None:
    print(f"{colour}{msg}{RESET}")


# --- Review passes ------------------------------------------------------------

REVIEW_PASSES: list[dict[str, str]] = [
    {
        "id": "readability",
        "label": "Readability & Code Quality",
        "prompt": (
            "Review ALL code in this project (not just recently written code). "
            "Improve naming (variables, functions, classes) throughout. "
            "Break up any function that does more than one logical thing, "
            "or that requires scrolling to read in full. "
            "Prefer small, named functions where the name removes the need for a comment. "
            "Add or improve inline comments where logic is non-obvious, "
            "and ensure consistent formatting across the entire codebase. "
            "Do NOT change any behaviour — only improve clarity."
        ),
    },
    {
        "id": "dry",
        "label": "DRY / Eliminate Repetition",
        "prompt": (
            "Audit the entire codebase for repeated or near-repeated logic. "
            "Extract shared helpers, base classes, or utility modules to eliminate "
            "duplication. Consolidate config values or magic numbers into constants. "
            "Ensure each concept has a single canonical home in the code. "
            "Do NOT change observable behaviour — only reduce repetition."
        ),
    },
    {
        "id": "tests",
        "label": "Write / Improve Tests",
        "prompt": (
            "Measure and improve test coverage across the ENTIRE codebase "
            "(not just recently written code). "
            "Cover: happy paths, edge cases, and error conditions for all modules. "
            "Use the testing framework already in the project (or pytest/jest if none). "
            "Target >=90% line coverage across the whole project. "
            "Run the test suite and fix any failures before finishing. "
            "Report the final coverage figure when done."
        ),
    },
    {
        "id": "docs",
        "label": "Documentation",
        "prompt": (
            "Add or improve documentation across the whole project: "
            "update (or create) a README section describing what was built, "
            "add docstrings/JSDoc to all public functions and classes, "
            "and document any non-obvious environment variables or config."
        ),
    },
    {
        "id": "security",
        "label": "Security Review",
        "prompt": (
            "Do a security review of the entire codebase. "
            "Look for: injection vulnerabilities, insecure defaults, "
            "hardcoded secrets, missing input validation, "
            "overly broad permissions, and unsafe dependencies. "
            "Fix any issues you find and explain what you changed."
        ),
    },
    {
        "id": "perf",
        "label": "Performance",
        "prompt": (
            "Review the codebase for obvious performance issues: "
            "N+1 queries, missing indexes, unnecessary re-renders, "
            "blocking I/O that could be async, large allocations in loops. "
            "Fix anything significant and add a comment explaining the optimisation."
        ),
    },
    {
        "id": "errors",
        "label": "Error Handling",
        "prompt": (
            "Audit error handling across the entire codebase. "
            "Ensure all I/O operations, network calls, and parsing steps "
            "have proper try/except (or try/catch) with meaningful error messages. "
            "Add logging where it would help diagnose production issues."
        ),
    },
]

PASS_IDS: list[str] = [p["id"] for p in REVIEW_PASSES]
DEFAULT_PASSES: list[str] = ["readability", "dry", "tests", "docs"]

# --- Dangerous-prompt guard ---------------------------------------------------

_DANGER_KEYWORDS: list[str] = [
    "rm -rf /",
    "format",
    "wipe",
    "delete all",
    "drop database",
    "drop table",
    "truncate",
    ":(){:|:&};:",
    "sudo rm",
    "chmod 777 /",
    "/etc/passwd",
    "dd if=/dev/zero",
]


def _looks_dangerous(text: str) -> bool:
    return any(
        re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE)
        for kw in _DANGER_KEYWORDS
    )


# --- Claude runner ------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _summarise_tool_use(tool: str, inp: dict[str, Any]) -> str:
    """Return a short human-readable summary for a tool-use event."""
    if tool in ("Read", "read_file") and "file_path" in inp:
        return f" {inp['file_path']}"
    if tool in ("Edit", "edit_file") and "file_path" in inp:
        return f" {inp['file_path']}"
    if tool in ("Write", "write_file") and "file_path" in inp:
        return f" {inp['file_path']}"
    if tool in ("Bash", "bash") and "command" in inp:
        cmd = inp["command"]
        return f" $ {cmd[:77]}..." if len(cmd) > 80 else f" $ {cmd}"
    if tool in ("Glob", "glob") and "pattern" in inp:
        return f" {inp['pattern']}"
    if tool in ("Grep", "grep") and "pattern" in inp:
        return f" /{inp['pattern']}/"
    return ""


def _print_event(event: dict[str, Any], start: float) -> None:
    """Parse a stream-json event and print a human-readable progress line."""
    elapsed = _format_duration(time.time() - start)
    tag = f"{DIM}[{elapsed}]{RESET} "
    msg_type = event.get("type", "")

    if msg_type == "assistant":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text":
                text = block.get("text", "")
                if text.strip():
                    print(f"{tag}{text}")

    elif msg_type == "tool_use":
        tool = event.get("tool", event.get("name", "unknown"))
        detail = _summarise_tool_use(tool, event.get("input", {}))
        print(f"{tag}{BLUE}[{tool}]{RESET}{detail}")

    elif msg_type == "system":
        msg = event.get("message", "")
        if msg:
            print(f"{tag}{DIM}{msg}{RESET}")

    elif msg_type == "result":
        result_text = event.get("result", "")
        if result_text:
            print(f"\n{tag}{GREEN}--- Result ---{RESET}")
            print(result_text)


def _process_lines(
    line_buffer: bytes,
    start: float,
    verbose: bool,
) -> bytes:
    """Process complete JSONL lines from the buffer, return the remainder."""
    while b"\n" in line_buffer:
        line, line_buffer = line_buffer.split(b"\n", 1)
        line_str = line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue
        try:
            _print_event(json.loads(line_str), start)
        except json.JSONDecodeError:
            if verbose:
                print(f"{DIM}{line_str}{RESET}")
    return line_buffer


def run_claude(
    prompt: str,
    workdir: str,
    *,
    skip_permissions: bool = True,
    dry_run: bool = False,
    idle_timeout: int = 120,
    verbose: bool = False,
) -> int:
    """Run a single Claude Code review pass.

    Uses ``--output-format stream-json`` so progress events stream in real time.
    There is no hard timeout — the process runs as long as it produces output.
    It is only killed after *idle_timeout* seconds of silence.
    """
    cmd = ["claude"]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    cmd += ["-p", prompt, "--output-format", "stream-json", "--verbose"]

    log(f"$ {' '.join(cmd[:3])} [prompt omitted for brevity]", DIM)

    if dry_run:
        print(f"{YELLOW}[DRY RUN] Would run in {workdir}:{RESET}")
        print(f"  Prompt: {prompt[:120]}...")
        return 0

    # Allow launching from within a Claude Code session.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    try:
        process = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        print(f"{RED}Error: `claude` not found. Is Claude Code installed?{RESET}")
        print("  Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    start = time.time()
    last_output = time.time()
    buf = b""

    try:
        while True:
            now = time.time()
            if now - last_output > idle_timeout:
                print(
                    f"\n{RED}Idle for {idle_timeout}s — killing "
                    f"(ran {_format_duration(now - start)}).{RESET}"
                )
                process.kill()
                break

            ready, _, _ = select.select([process.stdout], [], [], 1.0)
            if not ready:
                if process.poll() is not None:
                    remaining = process.stdout.read()
                    if remaining:
                        buf += remaining
                    break
                continue

            chunk = (
                process.stdout.read1(8192)
                if hasattr(process.stdout, "read1")
                else os.read(process.stdout.fileno(), 8192)
            )
            if not chunk:
                break

            last_output = time.time()
            buf += chunk
            buf = _process_lines(buf, start, verbose)

    finally:
        buf = _process_lines(buf + b"\n", start, verbose)

        stderr_bytes = process.stderr.read() if process.stderr else b""
        if stderr_bytes:
            stderr_str = stderr_bytes.decode("utf-8", errors="replace").strip()
            if stderr_str:
                print(f"{RED}stderr: {stderr_str}{RESET}")

    process.wait()
    elapsed = _format_duration(time.time() - start)
    rc = process.returncode
    colour = GREEN if rc == 0 else YELLOW
    status = "completed" if rc == 0 else f"exited with code {rc}"
    log(f"  Pass {status} in {elapsed}", colour)
    return rc


# --- CLI entry point ----------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Autonomous multi-pass code review using Claude Code.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "Available review passes (use with --passes):",
            *(f"  {p['id']:14s}  {p['label']}" for p in REVIEW_PASSES),
            "",
            f"Default passes: {', '.join(DEFAULT_PASSES)}",
            "",
            "Examples:",
            "  claudeloop",
            "  claudeloop --dir ~/my-project",
            "  claudeloop --cycles 3",
            "  claudeloop --passes readability dry tests security",
            "  claudeloop --all-passes --cycles 2",
            "  claudeloop --dry-run",
        ]),
    )

    parser.add_argument(
        "--dir", "-d", default=".",
        help="Project directory to review (default: current directory)",
    )
    parser.add_argument(
        "--passes", nargs="+", choices=PASS_IDS, default=DEFAULT_PASSES,
        metavar="PASS",
        help=f"Review passes to run. Choices: {', '.join(PASS_IDS)}",
    )
    parser.add_argument(
        "--all-passes", action="store_true",
        help="Run every available review pass",
    )
    parser.add_argument(
        "--cycles", "-c", type=int, default=1, metavar="N",
        help="Repeat the full suite N times (default: 1)",
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=120, metavar="SECS",
        help="Kill a pass after this many seconds of silence (default: 120)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would run without invoking Claude",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show raw non-JSON output from Claude",
    )
    parser.add_argument(
        "--pause", type=int, default=2,
        help="Seconds to pause between passes (default: 2)",
    )

    args = parser.parse_args()

    workdir = str(Path(args.dir).resolve())
    if not Path(workdir).is_dir():
        print(f"{RED}Directory not found: {workdir}{RESET}")
        sys.exit(1)

    passes = REVIEW_PASSES if args.all_passes else [
        p for p in REVIEW_PASSES if p["id"] in args.passes
    ]
    cycles = max(1, args.cycles)
    total = len(passes) * cycles

    print(f"\n{BOLD}claudeloop{RESET}")
    print(f"  Directory    : {workdir}")
    print(f"  Passes       : {', '.join(p['id'] for p in passes)}")
    print(f"  Cycles       : {cycles}")
    print(f"  Total steps  : {total}  ({len(passes)} passes x {cycles} cycle{'s' if cycles != 1 else ''})")
    print(f"  Idle timeout : {args.idle_timeout}s (no hard limit)")
    if args.dry_run:
        print(f"  {YELLOW}DRY RUN{RESET}")

    t0 = time.time()
    step = 0

    for cycle in range(1, cycles + 1):
        if cycles > 1:
            print(f"\n{BOLD}{CYAN}===  Cycle {cycle}/{cycles}  ==={RESET}")

        for pass_cfg in passes:
            time.sleep(args.pause)
            step += 1
            cycle_label = f" (cycle {cycle}/{cycles})" if cycles > 1 else ""
            banner(f"[{step}/{total}] {pass_cfg['label']}{cycle_label}", CYAN)

            if _looks_dangerous(pass_cfg["prompt"]):
                print(f"{YELLOW}Skipping '{pass_cfg['id']}' — dangerous keywords detected.{RESET}")
                continue

            rc = run_claude(
                pass_cfg["prompt"],
                workdir,
                dry_run=args.dry_run,
                idle_timeout=args.idle_timeout,
                verbose=args.verbose,
            )
            if rc != 0:
                print(f"{YELLOW}Pass '{pass_cfg['id']}' exited with code {rc}. Continuing...{RESET}")

    banner(f"All done! ({_format_duration(time.time() - t0)} total)", GREEN)


if __name__ == "__main__":
    main()
