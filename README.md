# checkloop

**Autonomous multi-check code review using Claude Code.**

**Writeup:** [Autonomous Multi-Check AI Code Review](https://alexmarquardt.com/ai-tools/checkloop-autonomous-code-review/)

Single-check AI code review misses things. `checkloop` runs dimension-specific checks — readability, DRY, tests, security, performance, error handling, and more — in sequence, then optionally cycles through the full suite again. Each check creates a cleaner baseline that makes the next category of issues more visible.

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
| `readability` | basic | Naming, function size, comments, formatting. No behaviour changes. |
| `dry` | basic | Finds repeated logic, extracts helpers, consolidates constants. |
| `tests` | basic | Targets >=90% coverage. Writes tests, runs them, fixes failures. |
| `docs` | basic | README, docstrings, config documentation. |
| `security` | thorough | Injection, hardcoded secrets, input validation, unsafe dependencies. |
| `perf` | thorough | N+1 queries, blocking I/O, unnecessary allocations. |
| `errors` | thorough | try/except coverage, meaningful error messages, logging. |
| `types` | thorough | Type annotations, replace `Any`/untyped code, run type checker. |
| `edge-cases` | exhaustive | Off-by-one, null/empty inputs, overflow, Unicode edge cases. |
| `complexity` | exhaustive | Flatten nested conditionals, reduce cyclomatic complexity. |
| `deps` | exhaustive | Remove unused deps, flag vulnerable/outdated packages. |
| `logging` | exhaustive | Structured logging, request context, observability gaps. |
| `concurrency` | exhaustive | Race conditions, missing locks, async/await correctness. |
| `accessibility` | exhaustive | Semantic HTML, ARIA, keyboard nav, colour contrast (WCAG AA). |
| `api-design` | exhaustive | Consistent naming, HTTP methods, error formats, pagination. |
| `test-validate` | bookend | Re-runs the full test suite after all checks. Fixes any regressions. Always runs last. |

## Why Multi-Check Works

A single "review everything" prompt overwhelms the model. Dimension-specific checks let it focus deeply on one concern at a time. And cycling produces compounding improvements:

1. **Readability** check renames a confusing variable and splits a long function
2. **DRY** check can now see that two of those smaller functions are nearly identical
3. **Security** check catches an injection vulnerability that was hidden inside the duplicated code
4. **Tests** check writes tests for the cleaned-up API surface, which is now testable

Each check builds on the work of the previous ones.

## Convergence Detection

When running multiple cycles (`--cycles N`), `checkloop` can stop early once the codebase stabilises. After each cycle it commits the changes and measures what percentage of total tracked lines were modified. If that percentage falls below the `--converged-at-percentage` threshold (default 0.1%), the loop exits. This requires the project directory to be a git repo. Set to 0 to disable.

```bash
# Run up to 5 cycles, but stop early if changes drop below 0.5%
uv run checkloop --cycles 5 --converged-at-percentage 0.5
```

## Options

```
--dir, -d DIR          Project directory to check (required)
--level, -l TIER       Check depth: basic, thorough, exhaustive (default: basic)
--checks CHECK [...]   Manually select checks (overrides --level)
--all-checks           Run all 17 checks (same as --level exhaustive)
--cycles, -c N         Repeat the full suite N times (default: 1)
--idle-timeout SECS    Kill after N seconds of silence (default: 120)
--dry-run              Preview without running
--verbose, -v          Show operational events, timing, and memory info
--debug                Show all details including raw subprocess output
--pause SECS           Pause between checks (default: 2)
--changed-only [REF]   Only check files that changed vs a base ref.
                       With no argument, auto-detects main/master.
                       Pass a branch or SHA to compare against.
--dangerously-skip-permissions
                       Pass --dangerously-skip-permissions to Claude Code
                       (bypasses all permission checks)
--converged-at-percentage PCT
                       Stop cycling early when less than PCT% of total lines
                       changed in a cycle (default: 0.1). Requires a git repo.
                       Set to 0 to disable convergence detection.
```

## How It Works

`checkloop` is a single-module Python CLI (`src/checkloop/cli.py`) that orchestrates Claude Code as a subprocess. Here is the high-level flow:

1. **Argument resolution** — Parses CLI flags, resolves the check tier (or manual check selection), and validates the target directory.
2. **Pre-run warning** — Displays a 5-second countdown so the user can abort. Warns if `--dangerously-skip-permissions` is (or isn't) set.
3. **Check execution** — For each check, builds a focused prompt (with commit-message rules appended) and invokes `claude -p <prompt> --output-format stream-json --verbose` as a subprocess.
4. **Real-time streaming** — Streams JSONL output from the subprocess, displaying tool-use events (file reads, edits, shell commands) and assistant messages with elapsed-time prefixes.
5. **Idle timeout** — If Claude produces no output for N seconds (default 120), the process group is killed and the next check begins.
6. **Per-check change detection** — After each check, compares the git HEAD before/after to report how many lines changed. Checks that produced no changes are skipped on subsequent cycles.
7. **Convergence detection** — After each full cycle, commits all changes and measures what percentage of total tracked lines were modified. If below the threshold, the loop exits early.
8. **Process cleanup** — Each Claude subprocess runs in its own process group (`setsid`). On completion or timeout, the entire group is killed (SIGTERM, then SIGKILL) to prevent orphaned child processes from leaking memory.

Each check operates on the code left by the previous check, so improvements compound: a readability check renames variables, then the DRY check can spot the newly-visible duplication, and so on.

### Key internal functions

| Function | Role |
|----------|------|
| `main()` | CLI entry point — parses args, resolves checks, runs the suite |
| `run_claude()` | Public API to run a single Claude Code check |
| `_run_check_suite()` | Orchestrates all checks across all cycles |
| `_stream_process_output()` | Streams and parses JSONL from the Claude subprocess |
| `_check_cycle_convergence()` | Commits changes and checks if the loop should stop |
| `_kill_process_group()` | Terminates a subprocess and all its children |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Required by Claude Code for authentication. Must be set before running `checkloop`. See the [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) for setup. |
| `CLAUDECODE` | Automatically stripped by `checkloop` when spawning subprocesses. This allows `checkloop` to be invoked from within a Claude Code session without conflict. You do not need to set this yourself. |

No other environment variables or config files are required. All configuration is done via CLI flags.

## Requirements

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Project Structure

```
src/checkloop/
├── __init__.py   # Package docstring and public API summary
└── cli.py        # All CLI logic: argument parsing, checks, Claude subprocess management
```

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/alexander-marquardt/checkloop.git
cd checkloop
uv sync --dev

# Run the test suite
uv run pytest

# Run with coverage
uv run pytest --cov=checkloop --cov-report=term-missing

# Type checking
uv run mypy src/checkloop/

# Run checkloop on itself (dogfooding)
uv run checkloop --dir . --dangerously-skip-permissions
```

The project has no runtime dependencies — only `pytest`, `pytest-cov`, and `mypy` in the dev group.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `claude` not found | Install Claude Code: `npm install -g @anthropic-ai/claude-code` |
| Checks hang waiting for permission prompts | You must use `--dangerously-skip-permissions` — checkloop cannot relay interactive prompts |
| "CLAUDECODE" conflict when running inside a Claude session | checkloop automatically strips this variable; no action needed |
| Convergence detection not working | Ensure the project directory is a git repo (`git init` if needed) |
| High memory usage over many checks | checkloop kills orphaned child processes between checks; use `--verbose` to monitor RSS |
| Idle timeout kills a check too early | Increase with `--idle-timeout 300` (or higher) |

## Contributing

1. Fork the repo and create a feature branch.
2. Install dev dependencies: `uv sync --dev`
3. Make your changes in `src/checkloop/cli.py`.
4. Run the full check suite:
   ```bash
   uv run pytest --cov=checkloop --cov-report=term-missing
   uv run mypy src/checkloop/
   ```
5. Ensure all tests pass and coverage stays above 90%.
6. Open a pull request with a clear description of your changes.

Commit messages should be concise (5–10 lines max), describe *what* changed and *why*, and avoid mentioning specific tools used to make the changes.

## License

MIT
