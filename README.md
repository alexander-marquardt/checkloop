# checkloop

**Autonomous multi-check code review using Claude Code.**

**Writeup:** [Autonomous Multi-Check AI Code Review](https://alexmarquardt.com/ai-tools/checkloop-autonomous-code-review/)

Asking an AI to "review everything" spreads it thin. `checkloop` runs focused, single-concern checks in sequence — readability, then DRY, then tests, then security, and so on — where each check builds on the previous one's cleanup. Splitting a long function reveals duplication; removing the duplication exposes a security gap that was hidden in the repeated code. Multi-cycle runs repeat the full suite on the improved codebase, catching issues that only become visible after the first round of fixes.

## Install

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`).

```bash
git clone https://github.com/alexander-marquardt/checkloop.git
cd checkloop
uv sync
```

## Usage

Run with `uv run checkloop` from the project directory. `--dir` is required:

```bash
# Check a project (basic tier: readability, dry, tests, docs)
uv run checkloop --dir ~/my-project

# Use the thorough tier for deeper checks
uv run checkloop --dir ~/my-project --level thorough

# Exhaustive — all 17 checks, repeat twice
uv run checkloop --dir ~/my-project --level exhaustive --cycles 2

# Pick specific checks manually (overrides tier)
uv run checkloop --dir ~/my-project --checks readability security tests

# Preview without running
uv run checkloop --dir ~/my-project --dry-run

# Only check files changed on this branch (vs main/master)
uv run checkloop --dir ~/my-project --changed-only

# Only check files changed vs a specific branch
uv run checkloop --dir ~/my-project --changed-only develop

# See what Claude is doing in detail
uv run checkloop --dir ~/my-project -v

# Clean up AI-generated code slop (on-demand, not part of any tier)
uv run checkloop --dir ~/my-project --cleanup-ai-slop
```

To make `checkloop` available globally (without `uv run`):

```bash
uv tool install git+https://github.com/alexander-marquardt/checkloop.git
```

## Check Tiers

Choose a check depth with `--level`:

| Tier | Checks | Description |
|------|--------|-------------|
| **basic** (default) | 6 checks | Core code quality — readability, DRY, tests, docs (plus test-fix/test-validate bookends) |
| **thorough** | 10 checks | Adds security, performance, error handling, type safety |
| **exhaustive** | 17 checks | Everything — includes edge cases, complexity, deps, logging, concurrency, a11y, API design |

Every tier automatically includes the `test-fix` (first) and `test-validate` (last) bookend checks to ensure the test suite is green before and after the review.

Use `--checks` to pick individual checks, or `--all-checks` as a shortcut for `--level exhaustive`.

## Available Checks

| Check | Tier | What it does |
|-------|------|-------------|
| `test-fix` | bookend | Runs the existing test suite and fixes any failures in source code. Always runs first. |
| `readability` | basic | Naming, function size, module/class docstrings for design strategy. Avoids rename churn. No behaviour changes. |
| `dry` | basic | Finds repeated logic, extracts helpers, separates mixed concerns into focused modules. |
| `tests` | basic | Behaviour-driven tests for happy paths, edge cases, complex logic correctness. Unit tests with mocks, integration tests separately. |
| `docs` | basic | README, config docs. Module-level docstrings for design strategy, class docstrings for intent. Function docstrings only where name+signature don't tell the full story. |
| `security` | thorough | Injection, hardcoded secrets, input validation. Won't change CORS/retry/auth config without a clear vuln. |
| `perf` | thorough | N+1 queries, O(N²) algorithms, blocking I/O, unnecessary allocations. Selective caching for expensive repeated computations. |
| `errors` | thorough | Centralized error handling for external services. Only where code can meaningfully respond. No wrapping code that can't fail. |
| `types` | thorough | Type annotations, replace `Any`/untyped code, runtime validation at API boundaries (Annotated/Pydantic/Zod). |
| `edge-cases` | exhaustive | Off-by-one, null/empty inputs, overflow, Unicode edge cases. |
| `complexity` | exhaustive | Flatten nested conditionals, reduce cyclomatic complexity. |
| `deps` | exhaustive | Remove verified-unused deps, flag vulnerable/outdated packages. |
| `logging` | exhaustive | Structured logging at entry points. No debug logging on hot paths. |
| `concurrency` | exhaustive | Race conditions, missing locks, async/await correctness. |
| `accessibility` | exhaustive | Semantic HTML, ARIA, keyboard nav, colour contrast (WCAG AA). |
| `api-design` | exhaustive | Consistent naming, HTTP methods, error formats, pagination. |
| `test-validate` | bookend | Re-runs the full test suite after all checks. Fixes any regressions. Always runs last. |
| `cleanup-ai-slop` | on-demand | Removes AI-generated noise: redundant docstrings, unnecessary logging, misleading error handling, coverage-driven tests. Only runs when explicitly requested via `--cleanup-ai-slop`. |

## Why Multi-Check Works

A single "review everything" prompt overwhelms the model. Dimension-specific checks let it focus deeply on one concern at a time. And cycling produces compounding improvements:

1. **Readability** check renames a confusing variable and splits a long function
2. **DRY** check can now see that two of those smaller functions are nearly identical
3. **Security** check catches an injection vulnerability that was hidden inside the duplicated code
4. **Tests** check writes tests for the cleaned-up API surface, which is now testable

Each check builds on the work of the previous ones.

## Token Usage

Each check is a full Claude Code session — reading files, making edits, running tests. A basic-tier run (6 checks) on a medium-sized project typically uses 200K–500K tokens. Thorough or exhaustive runs with multiple cycles can reach several million tokens. Consider running `checkloop` overnight or when stepping away — it's designed to run unattended, and this avoids competing with tokens you need for interactive work during the day.

## Checkpoint & Resume

If `checkloop` is interrupted (Ctrl+C, crash, terminal close), it saves a checkpoint after each completed check. On the next run with the same check selection, it detects the incomplete run and offers to resume:

```
Previous incomplete run detected:
  Started     : 2026-03-08T14:30:00+00:00
  Progress    : cycle 1/2, check 3/6 completed
  Next check  : tests

  Resume from checkpoint? [y/N] (defaulting to N in 10s):
```

If you don't respond within 10 seconds, it starts fresh. Use `--no-resume` to skip the prompt entirely.

The checkpoint file (`.checkloop-checkpoint.json`) is saved in the target project directory and is automatically cleaned up when the suite completes successfully.

## Convergence Detection

When running multiple cycles (`--cycles N`), `checkloop` can stop early once the codebase stabilises. After each cycle it measures what percentage of total tracked lines were modified. If that percentage falls below the `--convergence-threshold` threshold (default 0.1%), the loop exits. This requires the project directory to be a git repo. Set to 0 to disable.

```bash
# Run up to 5 cycles, but stop early if changes drop below 0.5%
uv run checkloop --cycles 5 --convergence-threshold 0.5
```

## Options

```
--dir, -d DIR          Project directory to check (required)
--level, -l TIER       Check depth: basic, thorough, exhaustive (default: basic)
--checks CHECK [...]   Manually select checks (overrides --level)
--all-checks           Run all 17 checks (same as --level exhaustive)
--cycles, -c N         Repeat the full suite N times (default: 1)
--idle-timeout SECS    Kill after N seconds of silence (default: 300)
--check-timeout SECS   Hard wall-clock limit per check (default: 0 = no limit).
                       Unlike --idle-timeout, kills even actively-running checks.
--max-memory-mb MB     Kill a check if its child process tree exceeds this RSS
                       (default: 8192). Set to 0 to disable.
--dry-run              Preview without running
--no-resume            Ignore any existing checkpoint and start fresh
--verbose, -v          Show operational events, timing, and memory info
--debug                Show all details including raw subprocess output
--pause SECS           Pause between checks (default: 2)
--changed-only [REF]   Only check files that changed vs a base ref.
                       With no argument, auto-detects main/master.
                       Pass a branch or SHA to compare against.
--dangerously-skip-permissions
                       Pass --dangerously-skip-permissions to Claude Code
                       (bypasses all permission checks)
--convergence-threshold PCT
                       Stop cycling early when less than PCT% of total lines
                       changed in a cycle (default: 0.1). Requires a git repo.
                       Set to 0 to disable convergence detection.
--cleanup-ai-slop      Add the cleanup-ai-slop check to the selected tier.
                       Removes AI-generated noise: redundant docstrings,
                       unnecessary logging, misleading error handling, etc.
```

## How It Works

`checkloop` is a modular Python CLI that orchestrates Claude Code as a subprocess. Here is the high-level flow:

1. **Argument resolution** — Parses CLI flags, resolves the check tier (or manual check selection), and validates the target directory.
2. **Pre-run warning** — Displays a 5-second countdown so the user can abort. Warns if `--dangerously-skip-permissions` is (or isn't) set.
3. **Check execution** — For each check, builds a focused prompt (with commit-message rules appended) and invokes `claude -p <prompt> --output-format stream-json --verbose` as a subprocess.
4. **Real-time streaming** — Streams JSONL output from the subprocess, displaying tool-use events (file reads, edits, shell commands) and assistant messages with elapsed-time prefixes.
5. **Idle timeout** — If Claude produces no output for N seconds (default 300), the process group is killed and the next check begins.
6. **Hard timeout & memory limit** — Optional hard wall-clock timeout (`--check-timeout`) kills checks regardless of output. Memory monitoring (`--max-memory-mb`, default 8192) samples child tree RSS every 10 seconds and kills the process group if it exceeds the limit.
7. **Checkpointing** — After each check, saves progress to `.checkloop-checkpoint.json`. If interrupted, the next run offers to resume from where it left off.
8. **Per-check change detection** — After each check, compares the git HEAD before/after to report how many lines changed. All checks run every cycle so that cascading improvements are never missed.
9. **Convergence detection** — After each full cycle, measures what percentage of total tracked lines were modified. If below the threshold, the loop exits early. Per-check commits are preserved individually for easier debugging.
10. **Process cleanup** — Each Claude subprocess runs in its own process group (`setsid`). On completion or timeout, the entire group is killed (SIGTERM, then SIGKILL) to prevent orphaned child processes from leaking memory. An atexit handler sweeps all tracked sessions on program exit.

Each check operates on the code left by the previous check, so improvements compound: a readability check renames variables, then the DRY check can spot the newly-visible duplication, and so on.

### Key internal functions

| Function | Role |
|----------|------|
| `main()` | CLI entry point — parses args, resolves checks, runs the suite |
| `run_claude()` | Public API to run a single Claude Code check |
| `_run_check_suite()` | Orchestrates all checks across all cycles |
| `_stream_process_output()` | Streams and parses JSONL from the Claude subprocess |
| `_check_cycle_convergence()` | Checks if the loop should stop based on change percentage |
| `_kill_process_group()` | Terminates a subprocess and all its children |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Required by Claude Code for authentication. Must be set before running `checkloop`. See the [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) for setup. |
| `CLAUDECODE` | Automatically stripped by `checkloop` when spawning subprocesses. This allows `checkloop` to be invoked from within a Claude Code session without conflict. You do not need to set this yourself. |

No other environment variables or config files are required. All configuration is done via CLI flags.

## Log File

Every run writes a DEBUG-level log to `.checkloop-run.log` in the target project directory. The log captures detailed operational data — prompt text, subprocess timing, memory measurements, and error traces — useful for post-run debugging. It is overwritten on each run and created with owner-only permissions (0600) since it may contain sensitive content. The file is excluded from git staging by default.

## Requirements

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Project Structure

```
src/checkloop/
├── __init__.py      # Public API exports (main, run_claude, CHECKS, TIERS, etc.)
├── check_runner.py   # Single-check execution: prompt assembly, invocation, change reporting
├── checkpoint.py     # Checkpoint save/load/clear for resume-after-interrupt
├── checks.py         # Check definitions, tier configuration, dangerous-prompt guard
├── cli.py            # CLI entry point, logging setup, checkpoint resume, signal handling
├── cli_args.py       # Argument parsing, validation, resolution, and pre-run display
├── commit_message.py # Commit message generation via Claude Code (plain-text, no streaming)
├── git.py            # Git operations: commits, diffs, line counting, branch detection
├── monitoring.py     # Memory/process monitoring, orphan detection, session cleanup
├── process.py        # Claude Code subprocess spawning, streaming, and cleanup
├── streaming.py      # JSONL stream parsing and real-time event display
├── suite.py          # Multi-cycle suite orchestration and convergence detection
└── terminal.py       # ANSI colours, banners, status messages, duration formatting
```

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/alexander-marquardt/checkloop.git
cd checkloop
uv sync --dev

# Run the test suite
uv run pytest

# Type checking
uv run mypy src/checkloop/

# Run checkloop on itself (dogfooding)
uv run checkloop --dir . --dangerously-skip-permissions
```

The project has no runtime dependencies — only `pytest` and `mypy` in the dev group.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `claude` not found | Install Claude Code: `npm install -g @anthropic-ai/claude-code` |
| Checks hang waiting for permission prompts | You must use `--dangerously-skip-permissions` — checkloop cannot relay interactive prompts |
| "CLAUDECODE" conflict when running inside a Claude session | checkloop automatically strips this variable; no action needed |
| Convergence detection not working | Ensure the project directory is a git repo (`git init` if needed) |
| High memory usage over many checks | checkloop kills orphaned child processes between checks and enforces an 8GB RSS limit by default. Adjust with `--max-memory-mb` or use `--verbose` to monitor RSS |
| Idle timeout kills a check too early | Increase with `--idle-timeout 600` (or higher) |
| A check runs too long | Use `--check-timeout 3600` for a hard 1-hour wall-clock limit per check |
| Want to start fresh after an interrupted run | Use `--no-resume` to skip the checkpoint prompt |

## Contributing

1. Fork the repo and create a feature branch.
2. Install dev dependencies: `uv sync --dev`
3. Make your changes in the relevant module under `src/checkloop/`.
4. Run the full check suite:
   ```bash
   uv run pytest
   uv run mypy src/checkloop/
   ```
5. Ensure all tests pass.
6. Open a pull request with a clear description of your changes.

Commit messages should be 2–3 sentences, describe *what* changed and *why*, and avoid mentioning specific tools used to make the changes.

## License

MIT
