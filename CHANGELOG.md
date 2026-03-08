# Changelog

## 0.1.0 (2026-03-07)

Initial release.

### Features
- **Multi-pass review** — 17 review passes including 2 bookend passes and 15 dimension-specific passes (readability, DRY, tests, docs, security, performance, error handling, type safety, edge cases, complexity, deps, logging, concurrency, accessibility, API design)
- **Bookend passes** — `test-fix` runs first to ensure the existing test suite passes; `test-validate` runs last to catch regressions
- **Review tiers** — `basic` (6 passes), `thorough` (10 passes), `exhaustive` (17 passes) via `--level`
- **Multi-cycle support** — repeat the full suite with `--cycles N` for compounding improvements
- **Convergence detection** — automatically stop cycling when changes fall below a threshold (`--converged-at-percentage`, default 0.1%), using git diff stats
- **Live progress streaming** — real-time display of tool-use events and assistant messages via Claude's `stream-json` output
- **Idle-based timeout** — no hard time limit; processes are killed only after N seconds of silence (`--idle-timeout`, default 300s)
- **Process group management** — spawns Claude in a dedicated process group; cleans up orphaned child processes after each pass
- **Memory monitoring** — logs RSS and child process count after each pass
- **Dangerous-prompt guard** — skips passes whose prompts contain destructive keywords (rm -rf, drop database, etc.)
- **Dry-run mode** — preview the full pass sequence without invoking Claude
- **Custom pass selection** — override tiers with `--passes` to pick individual passes
- **Pre-run safety warning** — 5-second countdown with Ctrl+C abort, with specific guidance when `--dangerously-skip-permissions` is or isn't set
