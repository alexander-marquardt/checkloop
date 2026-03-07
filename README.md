# claudeloop

**Autonomous multi-pass code review using Claude Code.**

**Writeup:** [Autonomous Multi-Pass AI Code Review](https://alexmarquardt.com/ai-tools/claudeloop-autonomous-code-review/)

Single-pass AI code review misses things. `claudeloop` runs dimension-specific review passes — readability, DRY, tests, security, performance, error handling — in sequence, then optionally cycles through the full suite again. Each pass creates a cleaner baseline that makes the next category of issues more visible.

## Install

Requires [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`npm install -g @anthropic-ai/claude-code`).

```bash
# Clone and install
git clone https://github.com/alexander-marquardt/claudeloop.git
cd claudeloop
uv sync

# Or install directly
uv tool install git+https://github.com/alexander-marquardt/claudeloop.git
```

## Usage

```bash
# Review the current directory (default passes: readability, dry, tests, docs)
claudeloop

# Review a specific project
claudeloop --dir ~/my-project

# Run all 7 passes, repeat twice
claudeloop --all-passes --cycles 2

# Pick specific passes
claudeloop --passes readability security tests

# Preview without running
claudeloop --dry-run

# See what Claude is doing in detail
claudeloop -v
```

## Review Passes

| Pass | What it does |
|------|-------------|
| `readability` | Naming, function size, comments, formatting. No behaviour changes. |
| `dry` | Finds repeated logic, extracts helpers, consolidates constants. |
| `tests` | Targets >=90% coverage. Writes tests, runs them, fixes failures. |
| `docs` | README, docstrings, config documentation. |
| `security` | Injection, hardcoded secrets, input validation, unsafe dependencies. |
| `perf` | N+1 queries, blocking I/O, unnecessary allocations. |
| `errors` | try/except coverage, meaningful error messages, logging. |

Default passes: `readability`, `dry`, `tests`, `docs`. Use `--all-passes` for all seven.

## Why Multi-Pass Works

A single "review everything" prompt overwhelms the model. Dimension-specific passes let it focus deeply on one concern at a time. And cycling produces compounding improvements:

1. **Readability** pass renames a confusing variable and splits a long function
2. **DRY** pass can now see that two of those smaller functions are nearly identical
3. **Security** pass catches an injection vulnerability that was hidden inside the duplicated code
4. **Tests** pass writes tests for the cleaned-up API surface, which is now testable

Each pass builds on the work of the previous ones.

## Options

```
--dir, -d DIR          Project directory (default: .)
--passes PASS [...]    Which passes to run
--all-passes           Run all 7 passes
--cycles, -c N         Repeat the full suite N times (default: 1)
--idle-timeout SECS    Kill after N seconds of silence (default: 120)
--dry-run              Preview without running
--verbose, -v          Show raw streaming output
--pause SECS           Pause between passes (default: 2)
```

## Requirements

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI installed and authenticated
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

## License

MIT
