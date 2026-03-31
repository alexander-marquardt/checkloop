# checkloop

**Autonomous multi-check code review using Claude Code.**

**Writeup:** [Autonomous Multi-Check AI Code Review](https://alexmarquardt.com/ai-tools/checkloop-autonomous-code-review/)

Asking an AI to "review everything" spreads it thin. `checkloop` runs focused, single-concern checks in sequence — readability, then DRY, then tests, then security, and so on — where each check builds on the previous one's cleanup. Splitting a long function reveals duplication; removing the duplication exposes a security gap that was hidden in the repeated code. Multi-cycle runs repeat the full suite on the improved codebase, catching issues that only become visible after the first round of fixes.


## Token Usage

Each check is a full Claude Code session — reading files, making edits, running tests. A basic plan (5 checks) on a medium-sized project typically uses 200K–500K tokens. Thorough or exhaustive runs with multiple cycles can reach several million tokens. Be careful!


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
# Check a project (basic plan — the default)
uv run checkloop --dir ~/my-project

# Use the thorough plan for deeper checks
uv run checkloop --dir ~/my-project --plan thorough

# Exhaustive — all 18 checks, repeat twice
uv run checkloop --dir ~/my-project --plan exhaustive --cycles 2

# Pick specific checks manually (overrides plan)
uv run checkloop --dir ~/my-project --checks readability security tests

# Use your own plan file
uv run checkloop --dir ~/my-project --plan ./my-plan.toml

# Preview without running
uv run checkloop --dir ~/my-project --dry-run

# Only check files changed on this branch (vs main/master)
uv run checkloop --dir ~/my-project --changed-only

# Only check files changed vs a specific branch
uv run checkloop --dir ~/my-project --changed-only develop

# See what Claude is doing in detail
uv run checkloop --dir ~/my-project -v

# Add a specific check on top of a plan
uv run checkloop --dir ~/my-project --plan thorough --checks cleanup-ai-slop
```

To make `checkloop` available globally (without `uv run`):

```bash
uv tool install git+https://github.com/alexander-marquardt/checkloop.git
```

## Execution Plans

Execution plans are TOML files that define which checks to run and which model to use for each check. They live in the `execution_plans/` directory at the project root. Three ship pre-populated — choose one with `--plan`:

| Plan | Checks | Description |
|------|--------|-------------|
| **basic** (default) | 5 checks | Core code quality — readability, DRY, tests (plus test-fix/test-validate bookends) |
| **thorough** | 13 checks | Adds docs, docs-accuracy, security, performance, error handling, type safety, derived-value consistency |
| **exhaustive** | 20 checks | Everything — includes edge cases, complexity, deps, logging, concurrency, a11y, API design, docs-accuracy, derived-value consistency, and code cleanup |

Every plan includes the `test-fix` (first) and `test-validate` (last) bookend checks to ensure the test suite is green before and after the review.

Use `--checks` to pick individual checks, or `--all-checks` as a shortcut for `--plan exhaustive`.

## Per-Check Model Selection

Each plan file specifies which Claude model to use for each check. The pre-populated plans assign models based on the cognitive demands of each task:

- **Sonnet** (faster, used for most checks) — pattern-matching tasks like readability, DRY, tests, docs, docs-accuracy, error handling, types, complexity, deps, logging, accessibility, API design, and code cleanup.
- **Opus** (deeper reasoning, used selectively) — multi-layer analysis tasks like security, concurrency, performance, and edge cases, where subtle issues span multiple code layers.

The `--model` flag overrides the per-check model for all checks:

```bash
# Use plan defaults (sonnet for most, opus for security/concurrency/perf/edge-cases)
uv run checkloop --dir ~/my-project --plan thorough

# Force all checks to opus (slower but deeper analysis everywhere)
uv run checkloop --dir ~/my-project --plan thorough --model opus

# Force all checks to sonnet (fastest, good for quick passes)
uv run checkloop --dir ~/my-project --plan thorough --model sonnet
```

## Available Checks

| Check | Plan | Model | What it does |
|-------|------|-------|-------------|
| `test-fix` | bookend | sonnet | Runs the existing test suite and fixes any failures in source code. Always runs first. |
| `readability` | basic | sonnet | Naming, function size, module/class docstrings for design strategy. Avoids rename churn. No behaviour changes. |
| `dry` | basic | sonnet | Finds repeated logic, extracts helpers, separates mixed concerns into focused modules. |
| `tests` | basic | sonnet | Behaviour-driven tests for happy paths, edge cases, complex logic correctness. Unit tests with mocks, integration tests separately. |
| `docs` | thorough | sonnet | README, config docs. Module-level docstrings for design strategy, class docstrings for intent. Function docstrings only where name+signature don't tell the full story. |
| `docs-accuracy` | thorough | sonnet | Cross-references CLI help, README examples, error messages, and API docs against actual code. Fixes factual inaccuracies — wrong defaults, renamed flags, stale file paths. Does not add documentation. |
| `security` | thorough | opus | Injection, hardcoded secrets, input validation. Won't change CORS/retry/auth config without a clear vuln. |
| `perf` | thorough | opus | N+1 queries, O(N²) algorithms, blocking I/O, unnecessary allocations. Selective caching for expensive repeated computations. |
| `errors` | thorough | sonnet | Centralized error handling for external services. Only where code can meaningfully respond. No wrapping code that can't fail. |
| `types` | thorough | sonnet | Type annotations, replace `Any`/untyped code, runtime validation at API boundaries (Annotated/Pydantic/Zod). |
| `derived-values` | thorough | opus | Finds frontend code that re-derives values the backend already computes and sends. Ensures derived values flow from one source instead of being independently calculated on both sides. |
| `edge-cases` | exhaustive | opus | Off-by-one, null/empty inputs, overflow, Unicode edge cases. |
| `complexity` | exhaustive | sonnet | Flatten nested conditionals, reduce cyclomatic complexity. |
| `deps` | exhaustive | sonnet | Remove verified-unused deps, flag vulnerable/outdated packages. |
| `logging` | exhaustive | sonnet | Structured logging at entry points. No debug logging on hot paths. |
| `concurrency` | exhaustive | opus | Race conditions, missing locks, async/await correctness. |
| `accessibility` | exhaustive | sonnet | Semantic HTML, ARIA, keyboard nav, colour contrast (WCAG AA). |
| `api-design` | exhaustive | sonnet | Consistent naming, HTTP methods, error formats, pagination. |
| `test-validate` | bookend | sonnet | Re-runs the full test suite after all checks. Fixes any regressions. Always runs last. |
| `cleanup-ai-slop` | exhaustive | sonnet | Removes unnecessary noise: redundant docstrings, unnecessary logging, misleading error handling, coverage-driven tests. Runs last (before test-validate) so it gets the final word on slop that earlier checks re-introduce. |

## Writing Your Own Plan Files

You can write your own plan files to define any combination of checks and model assignments. A plan file is a TOML file:

```toml
[tier]
name = "security-audit"
description = "Security-focused review with deep analysis"

[[checks]]
id = "test-fix"
model = "sonnet"

[[checks]]
id = "security"
model = "opus"

[[checks]]
id = "concurrency"
model = "opus"

[[checks]]
id = "edge-cases"
model = "opus"

[[checks]]
id = "test-validate"
model = "sonnet"
```

Point `--plan` at it:

```bash
uv run checkloop --dir ~/my-project --plan ./security-audit.toml
```

The pre-populated plans in `execution_plans/` use the same format — copy and modify them as a starting point.

## Customizing Checks

Each check is a Markdown file in `checks/` with YAML frontmatter (`id`, `label`) and a prompt body:

```markdown
---
id: readability
label: "Readability & Code Quality"
---

Improve naming (variables, functions, classes), but only where the current name
is genuinely confusing...
```

To customize a check, edit the `.md` file directly — no Python changes needed. To add a new check, create a new `.md` file in `checks/` and reference its `id` in a plan TOML or via `--checks`.

The `prompt_templates/` directory contains boilerplate injected into every check at runtime:
- `full_codebase_scope.md` — prepended to every check (unless `--changed-only` is used)
- `commit_message_instructions.md` — appended to every check

## Why Multi-Check Works

A single "review everything" prompt overwhelms the model. Dimension-specific checks let it focus deeply on one concern at a time. And cycling produces compounding improvements:

1. **Readability** check renames a confusing variable and splits a long function
2. **DRY** check can now see that two of those smaller functions are nearly identical
3. **Security** check catches an injection vulnerability that was hidden inside the duplicated code
4. **Tests** check writes tests for the cleaned-up API surface, which is now testable

Each check builds on the work of the previous ones.

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
--plan, -p PLAN        Plan name or path to a TOML plan file.
                       Pre-populated: basic, thorough, exhaustive (default: basic).
--checks CHECK [...]   Manually select checks (overrides --plan)
--all-checks           Run all 20 checks (same as --plan exhaustive)
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
--model, -m MODEL      Override the model for ALL checks. Accepts aliases
                       ('sonnet', 'opus') or full model IDs ('claude-sonnet-4-6').
                       When omitted, each check uses the model from the plan file.
```

## How It Works

`checkloop` is a modular Python CLI that orchestrates Claude Code as a subprocess. Here is the high-level flow:

1. **Argument resolution** — Parses CLI flags, loads the plan file (or resolves manual check selection), and validates the target directory.
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
checks/                   # Check definitions — one Markdown file per check
├── test-fix.md           # Each file has YAML frontmatter (id, label) and the prompt body
├── readability.md
├── dry.md
├── tests.md
├── docs.md
├── docs-accuracy.md
├── security.md
├── perf.md
├── errors.md
├── types.md
├── edge-cases.md
├── complexity.md
├── derived-values.md
├── deps.md
├── logging.md
├── concurrency.md
├── accessibility.md
├── api-design.md
├── cleanup-ai-slop.md
└── test-validate.md

execution_plans/          # Execution plans — which checks to run, which model for each
├── basic.toml
├── thorough.toml
└── exhaustive.toml

prompt_templates/         # Prompt fragments injected into every check at runtime
├── full_codebase_scope.md        # Prepended to every check (unless --changed-only)
└── commit_message_instructions.md # Appended to every check

src/checkloop/
├── __init__.py           # Public API exports
├── check_runner.py       # Single-check execution: prompt assembly, invocation, change reporting
├── checkpoint.py         # Checkpoint save/load/clear for resume-after-interrupt
├── checks.py             # Check loader (reads checks/), plan config, dangerous-prompt guard
├── cli.py                # CLI entry point, logging setup, checkpoint resume, signal handling
├── cli_args.py           # Argument parsing, validation, resolution, and pre-run display
├── commit_message.py     # Commit message generation via Claude Code (plain-text, no streaming)
├── git.py                # Git operations: commits, diffs, line counting, branch detection
├── monitoring.py         # Memory/process monitoring, orphan detection, session cleanup
├── process.py            # Claude Code subprocess spawning, streaming, and cleanup
├── streaming.py          # JSONL stream parsing and real-time event display
├── suite.py              # Multi-cycle suite orchestration and convergence detection
├── terminal.py           # ANSI colours, banners, status messages, duration formatting
└── tier_config.py        # TOML-based execution plan loading
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
