# Changelog

## 0.1.0 (2026-03-07)

Initial release.

- Seven review passes: readability, DRY, tests, docs, security, performance, error handling
- Multi-cycle support (`--cycles`) for compounding improvements
- Live progress streaming via Claude's `stream-json` output
- Idle-based timeout (no hard limit — runs as long as Claude is producing output)
- Dry-run mode for previewing what would execute
- Dangerous prompt detection as a safety guard
