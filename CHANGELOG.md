# Changelog

## 0.1.0 (2026-03-07)

Initial release.

### Features
- **Multi-check review** — 17 checks including 2 bookend checks and 15 dimension-specific checks (readability, DRY, tests, docs, security, performance, error handling, type safety, edge cases, complexity, deps, logging, concurrency, accessibility, API design)
- **Bookend checks** — `test-fix` runs first to ensure the existing test suite passes; `test-validate` runs last to catch regressions
- **Review tiers** — `basic` (6 checks), `thorough` (10 checks), `exhaustive` (17 checks) via `--level`
- **Multi-cycle support** — repeat the full suite with `--cycles N` for compounding improvements
- **Convergence detection** — automatically stop cycling when changes fall below a threshold (`--convergence-threshold`, default 0.1%), using git diff stats
- **Live progress streaming** — real-time display of tool-use events and assistant messages via Claude's `stream-json` output
- **Idle-based timeout** — no hard time limit; processes are killed only after N seconds of silence (`--idle-timeout`, default 300s)
- **Hard wall-clock timeout** — optional per-check time limit (`--check-timeout`) that kills even actively-running checks
- **Memory limit** — kills a check if its child process tree exceeds a configurable RSS threshold (`--max-memory-mb`, default 8192)
- **Process group management** — spawns Claude in a dedicated process group; cleans up orphaned child processes after each check
- **Memory monitoring** — logs RSS and child process count after each check
- **Checkpoint & resume** — saves progress after each check; offers to resume from where it left off if interrupted
- **Dangerous-prompt guard** — skips checks whose prompts contain destructive keywords (rm -rf, drop database, etc.)
- **Dry-run mode** — preview the full check sequence without invoking Claude
- **Custom check selection** — override tiers with `--checks` to pick individual checks
- **Changed-only mode** — restrict review to files changed vs a base branch (`--changed-only`)
- **Pre-run safety warning** — 5-second countdown with Ctrl+C abort, with specific guidance when `--dangerously-skip-permissions` is or isn't set
