# claudeloop

**Autonomous multi-pass code review using Claude Code.**

**Writeup:** [Autonomous Multi-Pass AI Code Review](https://alexmarquardt.com/ai-tools/claudeloop-autonomous-code-review/)

Single-pass AI code review misses things. `claudeloop` runs dimension-specific review passes — readability, DRY, tests, security, performance, error handling, and more — in sequence, then optionally cycles through the full suite again. Each pass creates a cleaner baseline that makes the next category of issues more visible.

## Install

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`).

```bash
git clone https://github.com/alexander-marquardt/claudeloop.git
cd claudeloop
uv sync
```

## Usage

Run with `uv run claudeloop` from the project directory:

```bash
# Review the current directory (basic tier: readability, dry, tests, docs)
uv run claudeloop

# Use the thorough tier for deeper review
uv run claudeloop --level thorough

# Exhaustive review — all 17 passes, repeat twice
uv run claudeloop --level exhaustive --cycles 2

# Pick specific passes manually (overrides tier)
uv run claudeloop --passes readability security tests

# Preview without running
uv run claudeloop --dry-run

# See what Claude is doing in detail
uv run claudeloop -v
```

To make `claudeloop` available globally (without `uv run`):

```bash
uv tool install git+https://github.com/alexander-marquardt/claudeloop.git
```

## Review Tiers

Choose a review depth with `--level`:

| Tier | Passes | Description |
|------|--------|-------------|
| **basic** (default) | 6 passes | Core code quality — readability, DRY, tests, docs (plus test-fix/test-validate bookends) |
| **thorough** | 10 passes | Adds security, performance, error handling, type safety |
| **exhaustive** | 17 passes | Everything — includes edge cases, complexity, deps, logging, concurrency, a11y, API design |

Every tier automatically includes the `test-fix` (first) and `test-validate` (last) bookend passes to ensure the test suite is green before and after the review.

Use `--passes` to pick individual passes, or `--all-passes` as a shortcut for `--level exhaustive`.

## Review Passes

| Pass | Tier | What it does |
|------|------|-------------|
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
| `test-validate` | bookend | Re-runs the full test suite after all passes. Fixes any regressions. Always runs last. |

## Why Multi-Pass Works

A single "review everything" prompt overwhelms the model. Dimension-specific passes let it focus deeply on one concern at a time. And cycling produces compounding improvements:

1. **Readability** pass renames a confusing variable and splits a long function
2. **DRY** pass can now see that two of those smaller functions are nearly identical
3. **Security** pass catches an injection vulnerability that was hidden inside the duplicated code
4. **Tests** pass writes tests for the cleaned-up API surface, which is now testable

Each pass builds on the work of the previous ones.

## Convergence Detection

When running multiple cycles (`--cycles N`), `claudeloop` can stop early once the codebase stabilises. After each cycle it commits the changes and measures what percentage of total tracked lines were modified. If that percentage falls below the `--converged-at-percentage` threshold (default 0.1%), the loop exits. This requires the project directory to be a git repo. Set to 0 to disable.

```bash
# Run up to 5 cycles, but stop early if changes drop below 0.5%
uv run claudeloop --cycles 5 --converged-at-percentage 0.5
```

## Options

```
--dir, -d DIR          Project directory (default: current directory)
--level, -l TIER       Review depth: basic, thorough, exhaustive (default: basic)
--passes PASS [...]    Manually select passes (overrides --level)
--all-passes           Run all 17 passes (same as --level exhaustive)
--cycles, -c N         Repeat the full suite N times (default: 1)
--idle-timeout SECS    Kill after N seconds of silence (default: 120)
--dry-run              Preview without running
--verbose, -v          Show operational events, timing, and memory info
--debug                Show all details including raw subprocess output
--pause SECS           Pause between passes (default: 2)
--dangerously-skip-permissions
                       Pass --dangerously-skip-permissions to Claude Code
                       (bypasses all permission checks)
--converged-at-percentage PCT
                       Stop cycling early when less than PCT% of total lines
                       changed in a cycle (default: 0.1). Requires a git repo.
                       Set to 0 to disable convergence detection.
```

## How It Works

`claudeloop` is a single-module Python CLI (`src/claudeloop/cli.py`) that:

1. Parses CLI arguments to determine the review tier (or manual pass selection) and how many cycles to run.
2. For each pass, builds a focused review prompt and invokes `claude -p <prompt> --output-format stream-json --verbose` as a subprocess.
3. Streams the JSONL output in real time, displaying tool-use events (file reads, edits, shell commands) and assistant messages with elapsed-time prefixes.
4. Applies an idle timeout — if Claude produces no output for N seconds (default 120), the process is killed and the next pass begins.
5. After all passes complete (across all cycles), prints a summary with total elapsed time.

Each pass operates on the code left by the previous pass, so improvements compound: a readability pass renames variables, then the DRY pass can spot the newly-visible duplication, and so on.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Required by Claude Code for authentication. Must be set before running `claudeloop`. See the [Claude Code docs](https://docs.anthropic.com/en/docs/claude-code) for setup. |
| `CLAUDECODE` | Automatically stripped by `claudeloop` when spawning subprocesses. This allows `claudeloop` to be invoked from within a Claude Code session without conflict. You do not need to set this yourself. |

No other environment variables or config files are required. All configuration is done via CLI flags.

## Requirements

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## Project Structure

```
src/claudeloop/
├── __init__.py   # Package docstring and public API summary
└── cli.py        # All CLI logic: argument parsing, review passes, Claude subprocess management
```

## Development

```bash
# Clone and install dev dependencies
git clone https://github.com/alexander-marquardt/claudeloop.git
cd claudeloop
uv sync --dev

# Run the test suite
uv run pytest

# Run with coverage
uv run pytest --cov=claudeloop --cov-report=term-missing

# Type checking
uv run mypy src/claudeloop/

# Run claudeloop on itself (dogfooding)
uv run claudeloop --dir . --dangerously-skip-permissions
```

The project has no runtime dependencies — only `pytest`, `pytest-cov`, and `mypy` in the dev group.

## License

MIT
