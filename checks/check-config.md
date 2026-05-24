---
id: check-config
label: "Project Infrastructure Audit"
---

Audit whether the project's test, lint, type-check, and CI infrastructure actually matches the stack it claims to use. This is a structural check: the goal is that the *tools* exist and are wired up, before other checks assume they do. Do not write application code — only configuration, CI files, and scaffolding.

1. **Detect the stack.** Read `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `Gemfile`, etc. Identify the primary languages, whether there is a frontend (React/Vue/Svelte/Next/etc.), a backend service, a CLI, and/or a library. Note which test runners, linters, type checkers, and formatters are already present.

2. **Check for surface-appropriate tools.** For each surface that exists, verify the appropriate tool is installed AND runnable:
   - **Frontend (any web UI):** a unit-test runner (Vitest/Jest/RTL), an E2E runner (Playwright/Cypress), and a linter (ESLint). If no E2E framework is installed, scaffold Playwright with a single smoke test against the app's root route.
   - **TypeScript:** `tsconfig.json` with `strict: true`. If strict is off, turn it on and fix only the config, not the type errors (that's the `types` check's job).
   - **Python:** pytest configured, `mypy` or `pyright` available, `ruff` or equivalent linter.
   - **Go:** `go test` works, `golangci-lint` configured.
   - **Any HTTP service:** at least one smoke test that starts the server and hits `/health` (or `/`) — if absent, add one.
   - **Any CLI:** at least one test invoking the CLI with `--help` and asserting exit code 0.

3. **Check for a coverage gate.** If the project has >200 source lines and no coverage reporting, wire up the language-native coverage tool (`coverage.py`, `jest --coverage`, `go test -cover`) and a baseline threshold. Measure the current coverage first, then set the threshold using this rule:

   - If current coverage is **≥ 80%** (typical for library, plugin, and CLI code where the public surface is small and well-tested), set the threshold to `current - 3`, rounded down to the nearest 5. A project sitting at 92% should be gated at 85%, not at 55%. An artificially low floor on already-well-tested code is worse than no gate at all — it accepts large regressions silently, and reviewers stop trusting the number.
   - If current coverage is **between 50% and 80%**, set the threshold to `current - 3` rounded down to the nearest 5. This catches regressions without blocking work that has a known coverage gap.
   - If current coverage is **below 50%**, set the threshold at the current level (no buffer) and add a note in the README or CI config that the floor is provisional and should rise as coverage improves. Do not set it lower than the current level "to be safe" — that bakes in room to regress.

   When a project already has a coverage gate, do not lower it. If the existing gate is well below the current coverage (e.g. gate at 55%, actual at 92%), raise it per the rule above and note the change in the commit message. This is the one place this check is allowed to tighten an existing threshold; everywhere else, only add missing thresholds.

4. **Check CI.** If `.github/workflows/`, `.circleci/`, or `.gitlab-ci.yml` exists, verify it runs:
   - the unit tests,
   - the linter,
   - the type checker (if the project has one),
   - the E2E tests (if they exist and can run in CI).
   If any of these exist locally but aren't wired into CI, add them. If CI doesn't exist at all and the project has a remote on GitHub, scaffold a minimal `.github/workflows/ci.yml` that runs the unit tests on push/PR.

5. **Check pre-commit / pre-push hooks.** If a `.pre-commit-config.yaml`, `husky`, or `lefthook` config exists, verify the hooks actually fire on commit. If a project has strong test culture but no pre-commit blocker for lint/type errors, add one.

Report what was missing and what you added. Do NOT add tools the project doesn't need: a pure backend service doesn't need Playwright; a single-file script doesn't need a coverage gate; a library without a CLI doesn't need CLI smoke tests. Err on the side of *not* adding infrastructure when the signal is ambiguous — this check is for clear gaps, not preferences.

Do NOT modify application code. Do NOT change existing test thresholds downward — only add missing ones, or raise a coverage gate per step 3 when the existing floor is well below current coverage. Do NOT replace an existing tool with a different one (if the project uses Jest, don't migrate to Vitest). Run any tool you added/configured to confirm it works before finishing.
