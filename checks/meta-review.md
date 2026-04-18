---
id: meta-review
label: "Meta-Review: Recommend Additional Checks"
---

**THIS CHECK MAKES NO CODE CHANGES.** It produces a recommendations report only. Do not edit any source files, tests, configs, or docs. Do not commit anything. Your only output is the file `.checkloop-recommendations.md` at the root of the project being reviewed.

Your job is to step back and ask: given what you now know about this codebase, what should the user *also* be testing, validating, or checking — that the checks already defined in checkloop don't cover, or that this specific project needs beyond the defaults? This is informational and advisory — the user will read the output and decide what to act on.

### What you already know

The following checkloop checks have definitions and are available to be run on this project:

- **test-fix** — runs the existing test suite and fixes failures
- **readability** — naming, function size, module docstrings
- **dry** — duplication removal, shared helpers
- **tests** — behaviour-driven tests, coverage-audit preamble (source vs tests, safety-sensitive first)
- **docs** — README, config, design-level docstrings
- **docs-accuracy** — cross-references docs/CLI-help against real code
- **security** — injection, secrets, input validation
- **perf** — N+1, O(N²), blocking I/O, caching
- **errors** — centralised error handling at boundaries
- **types** — type annotations, replace `Any`, runtime validation
- **derived-values** — frontend re-deriving backend-computed values
- **architecture-boundaries** — layer violations, circular deps
- **edge-cases** — off-by-one, null/empty, overflow, unicode
- **complexity** — flatten nested conditionals
- **deps** — unused/outdated/vulnerable dependencies
- **logging** — structured logging at entry points, not hot paths
- **concurrency** — race conditions, locks, async correctness
- **concurrency-testing** — tests for shared-state contention
- **accessibility** — semantic HTML, ARIA, keyboard, contrast
- **api-design** — naming, HTTP methods, errors, pagination
- **cleanup-ai-slop** — remove LLM-generated noise
- **coherence** — cross-check conflicts, over-engineering
- **test-validate** — re-runs tests after the suite
- **check-config** — audits project's test/lint/CI infrastructure
- **dead-code** — unused exports, orphaned files, dead branches
- **observability** — logs/metrics/alerts on critical paths
- **schema-validation** — validators at every external boundary
- **secret-leakage** — PII/secret sweep in code, logs, bundles
- **migration-safety** — DB migration safety (locking, backfills, rollback)
- **feature-flags** — ghost/orphan/stale-flag hygiene
- **fixture-drift** — mocks/fixtures that no longer match real code

### What to do

1. **Read the codebase broadly.** Don't do a deep audit — skim the structure. Note the languages, frameworks, primary surfaces (web UI, API, CLI, library), data stores, external integrations, and build/deploy pipeline. Look at `README.md`, the top-level directories, `package.json` / `pyproject.toml`, any CI config, and a handful of representative source files.

2. **Identify domain-specific or stack-specific risk areas that the generic checks don't cover.** Examples of the kind of thing to look for:
   - An e-commerce project might need specific checks for tax calculation, refund flows, inventory drift, currency rounding.
   - An ML-serving project might need checks for model-version pinning, feature-drift monitoring, prompt-injection guards in LLM pipelines, deterministic-seed hygiene in tests.
   - A multi-tenant SaaS might need row-level security audits, tenant-isolation tests, noisy-neighbour limits.
   - A mobile/desktop client might need offline-mode tests, sync-conflict resolution, permission-prompt coverage.
   - A search-heavy product might need ranking-regression tests, relevance-baseline snapshots, query-latency SLOs.
   - A data-pipeline project might need schema-compatibility tests, replay/idempotency, dead-letter handling.
   - A project with i18n might need localisation-coverage audits.

   Be specific. "Add security tests" is not useful. "Add a test that verifies `/api/orders/:id` 403s when called with a session for a different tenant" is useful.

3. **Note gaps in the *existing* checks specifically for this project.** For example: if the project has a React frontend but most existing checks read as backend-biased, say so and suggest what the frontend-specific variant should cover. If `security` doesn't address a project-specific attack surface (e.g. webhook-replay attacks on a payments service), call that out.

4. **Note tests the project itself should have** regardless of whether a checkloop check would catch them — things that require product knowledge (business invariants, domain rules) and can't be reasonably automated through a generic LLM check. Frame these as "tests the maintainer should add," not "checks to add to checkloop."

5. **Prioritise.** Rank your recommendations into three tiers: **high** (would likely catch a production bug within 6 months), **medium** (protective, hardening), **low** (polish). Be willing to have a short list. Five well-chosen recommendations beats twenty generic ones.

### Output

Write the report to `.checkloop-recommendations.md` at the root of the project being reviewed. Use this structure:

```markdown
# Checkloop Meta-Review

Generated: <ISO date>

## Project snapshot
- Stack: <one-line>
- Primary surfaces: <web / API / CLI / library / mobile / ...>
- Notable integrations: <databases, external APIs, queues, ML, ...>

## High-priority recommendations
### 1. <Short title>
**What:** <one-sentence description>
**Why:** <specific risk in this project, with file paths or flow names>
**Who acts:** checkloop maintainer (add a new check) | project maintainer (add a test) | both

### 2. ...

## Medium-priority recommendations
### ...

## Low-priority recommendations
### ...

## Gaps in existing checkloop checks for this project
- `<check-id>`: <specific gap as applied to this project>
- ...

## Checks you ran but might want to tune
- `<check-id>`: <observation>
- ...
```

If the project has nothing interesting to recommend (tiny repo, already exhaustively checked), say so briefly — don't pad.

**Reminder: this check must not edit any source files, tests, configs, or docs.** Only write `.checkloop-recommendations.md`. Do not git-commit anything. Do not run the test suite (it's not relevant — the previous `test-validate` check already did). Just analyse and report.
