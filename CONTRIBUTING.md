# Contributing to checkloop

This document is the single source of truth for how work happens in this repository — engineering conventions, testing expectations, commit and PR mechanics, and the project-specific philosophies that aren't obvious from the code alone. Read it before opening a PR; if something here conflicts with what you find in a file, the file is wrong and a fix is welcome.

## What checkloop is

`checkloop` runs Claude Code as a sequence of focused, single-concern code reviews against a target repository. Instead of one omnibus "review everything" prompt, it loops — readability, then DRY, then tests, then security, and so on — and lets each pass build on what the previous pass cleaned up. A typical run produces a scratch branch in a disposable clone under `~/checkloop-runs/`; the user reviews and adopts the work in their own repo afterwards.

The tool itself is a Python CLI that orchestrates Claude Code subprocesses, watches their JSONL output, enforces idle and memory limits, and emits per-check logs and telemetry. It is *not* a Claude SDK wrapper or a framework — it is glue around the `claude` binary.

## Stack

- **Python ≥ 3.12** — runtime
- **uv** — package manager and runner
- **pytest** — test runner
- **mypy** — type checker
- **No frontend, no database, no service stack** — the CLI is the whole product

External runtime dependency: the `claude` CLI from `@anthropic-ai/claude-code`. Tests stub it; manual end-to-end runs require it on `PATH`.

## Setup

```bash
git clone https://github.com/alexander-marquardt/checkloop.git
cd checkloop
uv sync
uv run checkloop --help
```

To iterate against your own work without re-installing, use `uv run` — it picks up local source changes immediately.

## Repository layout

```
checkloop/
├── src/checkloop/
│   ├── cli.py, cli_args.py        # Entry point and argument parsing
│   ├── suite.py                   # Run orchestration and post-run review prompt
│   ├── check_runner.py            # Per-check execution: prompt build, dangerous-keyword guard, Claude invocation
│   ├── checks.py                  # Check registry, plan loading, dangerous-prompt regex
│   ├── process.py                 # Subprocess lifecycle, watchdog (idle/memory), descendant tracking
│   ├── streaming.py               # JSONL event parsing, StreamObserver, tool-use rendering
│   ├── monitoring.py              # Telemetry sampler (RSS, descendant snapshot)
│   ├── telemetry.py               # JSONL telemetry writer
│   ├── clone.py                   # Clone-mode setup; auto-memory import
│   ├── git.py                     # Git helpers (status, branch, commit count)
│   ├── commit_message.py          # Per-check commit message generation
│   ├── checkpoint.py              # Resume support
│   ├── project_map.py             # Cached layout summary, refreshed per check
│   ├── run_storage.py             # On-disk run directory layout
│   ├── tier_config.py             # Plan TOML schema
│   └── terminal.py                # ANSI colour, status-line rewriting
├── checks/                        # One markdown file per check (id in frontmatter)
├── execution_plans/               # Pre-populated TOML plans (basic, thorough, exhaustive, super-exhaustive)
├── prompt_templates/              # Fragments injected into every check prompt
├── tests/                         # pytest suite
├── CLAUDE.md                      # Agent-facing pointer to this file
└── CONTRIBUTING.md                # You are here
```

## How a run works

1. **Clone-mode setup** (default): `clone.py` makes a fresh clone of the target repo under `~/checkloop-runs/<project>-<timestamp>/`, checks out the review branch, and creates a scratch branch off it. The user's actual working tree is never touched. The user's Claude memory directory for the target project is read-only-imported into the clone's slug so the agent inherits prior context — writes during the run land in the clone's slug and are orphaned with the clone.
2. **Plan resolution**: `checks.py` loads the requested plan TOML, expands check IDs into prompts, and builds a flat ordered list. Bookend checks (`test-fix` first, `test-validate` last) are always present.
3. **Per-check execution**: `check_runner.py` builds the prompt, runs `looks_dangerous()` over it as a safety net, snapshots git state, then `process.py` spawns the Claude subprocess and `streaming.py` parses its stdout JSONL.
4. **Watchdog**: while the subprocess runs, three signals decide whether it's still doing real work — descendant processes alive, parent tree CPU activity, and recent JSONL output. The watchdog suppresses idle-kill while *any* signal is positive, while a `system: status=compacting` event is in flight, and tier-3 (no signal at all) is now advisory and bounded only by `--check-timeout`. The historical context for that policy is in PR #20.
5. **Post-run**: `suite.py` prints a per-check summary, surfaces `.checkloop-recommendations.md` from the clone (if `meta-review` ran), and emits a copy-paste prompt that drives the original-repo Claude through reviewing the scratch branch and re-applying the work as new commits.

A more detailed user-facing walkthrough lives in `README.md` — this section exists so contributors don't have to reverse-engineer it from the code.

## Day-to-day commands

```bash
# Run the test suite (the canonical command)
uv run python -m pytest tests/ -x -q

# Type-check
uv run mypy src/checkloop/

# Single test file
uv run python -m pytest tests/test_process.py -x -q

# Single test
uv run python -m pytest tests/test_streaming.py::TestStreamObserver -x -q

# Run checkloop against itself in dry-run (no Claude calls)
uv run checkloop --dir . --review-branch main --dry-run

# Run checkloop against a real target (requires the claude binary)
uv run checkloop --dir ~/some-project --review-branch main --plan basic
```

## Branching, rebasing, merging

Work happens on feature branches off `main`. Sync with `git pull origin main --rebase` rather than merge commits — the history stays linear and bisectable. PRs target `main` directly; there is no staging branch.

Before a PR is mergeable, CI must be green. Don't merge with checks pending or failing. If a check is flaky, fix the flake, don't retry until it passes.

## Push policy

A few rules that keep automated agents (and tired humans) from doing things that are hard to undo:

- **No unsolicited pushes.** Commit locally and stop. The user normally handles pushes. When the user explicitly says "push" or "push and open a PR", a feature-branch push is fine without re-prompting.
- **Direct merges or pushes to `main` (including `gh pr merge` onto main) require one explicit confirmation**, even when an earlier "push" was authorised. Unreviewed changes on the default branch are exactly the class of action this rule exists to prevent.
- **Force-push to any branch must be announced before running.** It is not the agent's place to silently rewrite history.
- **Never skip git hooks** (`--no-verify`, `--no-gpg-sign`, etc.) unless the user has asked for it explicitly. If a hook fails, fix the underlying issue.
- **When checkloop is driving commits inside a *target* project, the tool itself never pushes.** The user owns the decision to publish. Preserve this invariant in any future change to the commit flow.

## Commit messages

The minimum bar is **two or three sentences** of plain professional English describing what changed and why — never a single-word message or filler (`cleanup`, `wip`, `update readme`). For commits where every change was mechanical and self-evident from the diff (a typo fix, a dependency bump, a rename of already-tested code with no behaviour change), the minimum is also the maximum — keep it tight, the diff carries the rest.

For any commit where a non-trivial call was made — anywhere the spec was ambiguous, the implementation diverged from the obvious shape, an alternative was rejected, or an open question remains — expand the body to capture the **audit trail**. The aim is that a maintainer (or your future self) reading the commit alone, six months from now, can reconstruct what you were thinking *at the moment of the commit*. PR descriptions tell the cross-commit story; the commit body has to survive squash and rebase and still read coherently from `git log`.

The categories worth surfacing in the body, when they apply:

- **Design decisions** — where the spec or requirement was ambiguous and a call had to be made. Name the ambiguity, name the call, name the constraint that pointed to it. *Example: "The spec didn't say whether unknown rule types should fail loudly or be silently ignored; chose fail-loudly so SI deployments catch typo'd rule names in CI rather than in production."*
- **Derivations** — where the implementation diverged from the obvious or spec'd shape and why. Don't make a future reader hunt through unrelated documents to discover the divergence. *Example: "Diverges from the schema in `docs/05-query-rewriting.md` by adding a `precedence` field on `FilterGroup`; the schema doesn't say how overlapping groups resolve, and precedence is the cheapest way to make the existing tests pass without a wider refactor."*
- **Tradeoffs** — what alternatives were considered and why this one was selected. Naming the rejected option is what makes this an audit trail rather than an advertisement. *Example: "Considered a Lua-script vs MULTI/EXEC for the atomic decrement. Picked the script for the smaller round-trip at the cost of one extra deploy step (`SCRIPT LOAD` on every Redis restart) which `lifespan.py` now handles."*
- **Open questions** — anything you'd want a maintainer to sign off on, revisit, or change later. Prefix them with `Open question:` on their own line so `git log --grep "Open question:"` finds them. Surfacing a known uncertainty in the commit is much cheaper than discovering it in production. *Example: "Open question: the 60-bucket retention may be too tight under traffic spikes that span the cleanup cron interval — revisit once we have a week of production data via `ratelimit_evictions_total`."*
- **Rationale** — why this approach is the right one, framed as the *why* the code itself can't express. The code shows the *how*; the rationale is the constraint, threat model, past incident, or trade-off that justifies the shape. If you can't write the rationale, the design isn't load-bearing yet and probably shouldn't ship.

Use the labels (`Design decisions:`, `Derivations:`, `Tradeoffs:`, `Open question:`, `Rationale:`) on their own line so the body stays greppable from `git log`. Not every label needs to appear in every non-trivial commit — only those that earned their keep on this particular change. Anything else useful for a future reader to understand the moment-of-implementation thinking belongs in the body too, even if it doesn't fit a named category. The pair rule of thumb: if you'd be annoyed to discover a teammate had made this decision without telling you, write it down.

### Hard rules (regardless of length)

- No mention of Claude, AI, LLMs, "AI-assisted", or any tool-attribution phrasing. The work product stands on its own.
- No `Co-Authored-By:` or `Signed-off-by:` trailers attributing AI tools.
- No single-word or filler messages (`cleanup`, `wip`, `fix`, `update readme`).
- No restating the diff. "I edited `foo.py` and added a helper to `bar.py`" is doing the diff's job; reviewers can read the diff. The body's job is to explain what the diff doesn't show.
- Set `git config user.email` to a real address. Commits authored by the default `user@hostname` account get rejected on review.

### Examples

**Trivial commit — the 2–3 sentence floor is also the ceiling:**

> Fix a regex backreference that misnumbered after the previous capture-group reorder. The CI failure surfaced as a flaky parse on Windows-style line endings; the fix is correct on both platforms.

**Non-trivial commit — full audit trail:**

> Replace the in-process token-bucket rate limiter with a Redis-backed sliding-window implementation. Host-level fairness across replicas is the only way the documented SI rate-limit contract holds; the previous in-process limiter let a misbehaving tenant exceed the documented limit by a factor of *N* replicas.
>
> Design decisions: 1-second bucket granularity with 60-bucket retention, not the more common fixed-window or leaky-bucket variants. Fixed window permits a 2× burst at the boundary; leaky bucket masks the burst pattern that triggers the auto-scaler. Both are wrong for our threat model.
>
> Tradeoffs: considered Lua-script atomicity vs MULTI/EXEC. Picked the script (smaller round-trip, atomic by Redis semantics) at the cost of one extra deploy step (`SCRIPT LOAD` on every Redis restart) which `lifespan.py` now handles.
>
> Open question: 60-bucket retention may be too tight under traffic spikes that span the cleanup cron interval. Want to revisit once we have a week of production data — instrumented via `ratelimit_evictions_total`.

## Pull requests

Open one PR per concern. A bug fix and a refactor that touches the same file are two PRs unless the refactor is genuinely required to land the fix.

The PR description should answer: what changed, why, and what to look at first. Test plan as a checklist. Link the issue if there is one. Don't restate the diff — the reviewer can see it.

Before requesting review:

```bash
uv run python -m pytest tests/ -x -q
uv run mypy src/checkloop/
```

Both must pass locally. CI will catch what your machine doesn't, but burning a CI cycle on something you could have caught in 8 seconds is a poor use of everyone's time.

## Reviewing & merging

CI being green is necessary but not sufficient. Before clicking merge, read the full diff and check each commit individually:

- **Every commit either ships a test or names why not.** A commit that changes runtime behaviour — what code returns, logs, raises, commits, sends — must include the test that pins the new behaviour in the *same* commit. A commit that is intentionally test-free (a rename of already-tested code, a docstring fix, a formatting pass, a type-annotation tightening, a genuinely untestable change in this stack) must say so explicitly in the commit body, in one short sentence that names the specific reason or missing piece: `no test added: rename only — existing coverage in tests/foo_test.py`, `no test added: docstring fix`, `no test added: no E2E rig for the upload flow`. Vague non-explanations like `no test added: hard to test` or `untestable here` are not the carve-out — they are the most common way the rule gets bypassed. Reject the commit and ask for either the test or a named gap.
- **Drop net-neutral commits.** A commit that renames for marginal clarity, reorders without changing semantics, deletes a docstring whose deletion did not improve the file, or introduces an abstraction with one caller earns nothing in exchange for review time. Ask the author to drop or fold it. The `commit-audit` check flags these when a PR was produced by a checkloop run; trust the flag and verify.
- **Reject AI-attribution leakage.** Commit messages, code comments, PR descriptions, README, docstrings — none of these may mention Claude, AI, LLMs, checkloop, or any tool attribution. If a `Co-Authored-By` or `Generated with X` trailer slipped through, ask for a rewrite before merging.
- **CI must be green with no checks pending.** A flaky test is a fix-the-flake situation, not a retry-until-green situation.
- **Recursive case.** Many PRs against this repo were produced by checkloop running against itself. When you review one, apply the same audit standards you would when reviewing checkloop's output against any other target: same test-or-named-gap rule, same net-neutral drop rule, same no-AI-attribution rule. The fact that the changes came out of checkloop does not buy them lower scrutiny — the failure modes are the same ones documented above.

## Engineering principles

These are the rules that aren't obvious from reading the code, and that have caused incidents when violated.

### Edit at the source

When you move or rename code, update every call site in the same change. Do not leave a `from .new_home import X  # noqa: F401 — kept for backward compat` line behind. Forwarding stubs hide the real owner of a symbol, accumulate as cruft, and signal that the split was incomplete.

In the same spirit:

- No `# moved to module_x` or `// see new_home.py` placeholder comments where deleted code used to live. Git history is the record. The file is not a changelog.
- No renaming a now-unused parameter to `_unused` to telegraph intent. If it's unused, delete it from the signature.
- No code parked behind dead conditionals "for future use". When the call site arrives, the code can arrive with it.

### Don't catch what you can't handle

Bare `except Exception:` in a request handler or a long-running loop is a bug-hider. Catch the specific exception you can do something useful about, and re-raise (or restructure) the rest.

- Repeated catch/log/raise patterns get factored into a helper so log format and status codes don't drift across handlers.
- Errors carry context. A traceback alone is not enough — include the operation name, relevant IDs, and the inputs that produced the failure. The on-call engineer should not need to reproduce the bug to understand it.
- Failures are loud. Catching and swallowing is a defect.
- Error messages tell the reader what went wrong *and* what to try next. "Connection refused" is not a usable error; "Could not reach the Claude CLI on PATH — install with `npm install -g @anthropic-ai/claude-code`" is.

### Optionality lives in configuration, not in `try/except`

If a feature can be disabled, that's a config flag. If a dependency is required when the feature is enabled and it's missing, that's a hard error — fail loudly with an actionable message. A `try/except ImportError: pass` that silently turns off behaviour is the wrong place to make that decision; it makes the system's actual behaviour depend on what's installed at runtime, not on what the operator chose.

### Don't silence the type checker

`# type: ignore` (and TypeScript's `as any`) is for genuine third-party type-stub gaps, not for shutting up an annotation that's flagging a real issue. If mypy is unhappy, the answer is to fix the type or fix the code, not to suppress the diagnostic. The same logic applies to `pytest.skip` and `xfail` — those are reserved for environmental gates (the `claude` binary not being on PATH, for instance), and the gate must be explicit with a documented install path.

### Don't add what isn't requested

Bug fixes don't grow into surrounding refactors. One-shot operations don't sprout helper functions. Three similar lines beat a premature abstraction. Half-finished implementations don't ship — if the call site for a new helper isn't part of the same change, the helper isn't ready to land.

### No net-neutral cosmetic churn

Renaming a variable for marginal clarity, reordering arguments without changing semantics, deleting a docstring whose deletion does not improve the file, introducing an abstraction with one caller — each of these makes the diff larger without making the codebase better. If a candidate change has no observable runtime effect *and* nothing for a test to pin *and* a fresh reader of the resulting file would not find it any clearer than before, drop the change. The "Don't add what isn't requested" rule covers additions; this one covers deletions and rewrites that earn nothing in exchange for the review and merge cost. When the cleanup-ai-slop check produces commits that fit this description (a stripped docstring that was explaining intent, a removed defensive check at a real boundary), the coherence check is supposed to catch and restore them — but the reviewer is the final backstop.

One carve-out: a rename that fixes a name that *lies* (its value contradicts the identifier) or is *overloaded* (one name reused for different concepts across contexts or layers, or one concept carried under several disagreeing names) is **not** net-neutral, even when the commit is rename-only. Such a name is read as truth straight into a bug, and disambiguating it removes a correctness hazard. Keep these renames — and require they be applied to every call site and layer in the same commit, not papered over with a compatibility alias on the internal name.

### Comments earn their place

Default to no comments. Names should do the work. Write a comment when the *why* is non-obvious and would surprise a future reader: a hidden constraint, a workaround for a specific bug, an invariant that isn't enforced by code. Don't write comments that narrate the *what* (the next line already does that), and don't reference the current task or PR ("added for issue #123") — that information lives in the commit message and rots in the file.

## Testing

The full suite is fast (~8 seconds, 979+ tests). Keep it that way.

- Tests run with `uv run python -m pytest tests/ -x -q`. The `-x` is intentional — fail fast.
- Every behaviour change comes with a test that pins the new behaviour. New code without a test is incomplete; bug fixes without a regression test are an invitation to repeat the bug. The only acceptable carve-out is a commit that genuinely has no observable runtime behaviour — a pure rename of already-tested code, a docstring or comment fix, a formatting pass, a type-annotation tightening on already-tested code — and even then the commit body must say so explicitly with a named reason: `no test added: rename only — existing coverage in tests/foo_test.py`, `no test added: docstring fix`. A vague `no test added: hard to test` is not the carve-out and does not satisfy this rule. For checks within checkloop itself, this is the same rule the agent operates under in target repos — see `prompt_templates/tests_for_behavior_changes.md`.
- Don't mock what you can run cheaply. The watchdog tests use real subprocesses. The streaming tests construct real JSONL events and parse them. Mocking is for the `claude` binary itself (because invoking it spends tokens) and for OS-level signals that don't reproduce reliably in CI.
- When you do mock, mock at the *boundary* — the subprocess, the filesystem, the clock — never the function under test. A mock that knows the internals of the code it's patching is a refactor blocker.
- Skipping or `xfail`-ing a test to silence a failure is forbidden. Find the root cause. The same rule that bans `# type: ignore` on real type errors bans skip-marks on real test failures: both hide bugs.

## Adding a check

Each check is a markdown file in `checks/` with a frontmatter `id` and `label`, plus a body that becomes the prompt sent to Claude. Pick an id (lowercased, hyphenated), drop the file in `checks/`, and add it to the plans where it belongs (`execution_plans/*.toml`) with a model and idle timeout. The check registry in `checks.py` reloads from disk — no Python edit is required for the check itself, only for plan membership if needed.

A few practical notes:

- The body is read verbatim. If it includes phrases that match the dangerous-keyword regex in `checks.py`, the check will be skipped at runtime. Rephrase or quote the trigger words. The list of patterns is small (`drop table`, `rm -rf /`, etc.) and the false positives are easy to spot — run `uv run python -c "from checkloop.checks import looks_dangerous; print(looks_dangerous(open('checks/your_check.md').read()))"`.
- Checks that should be available but excluded from default plans go in `_ON_DEMAND_ONLY` in `checks.py`. They're still callable via `--checks <id>` but aren't dragged in by `--plan super-exhaustive`. `migration-safety` is the worked example.
- The frontmatter `label` shows up in the run banner and the per-check log path. Keep it short and specific.

## Adding an execution plan

Drop a TOML file in `execution_plans/` with a `[tier]` block (name, description) and one `[[checks]]` block per check (id, model, idle_timeout). Plans don't need to be subsets of each other, but every check id must exist in the registry. The test in `tests/test_checks.py` will catch most wiring mistakes.

`super-exhaustive` is the canonical superset of every non-on-demand check. There's a test (`test_super_exhaustive_tier_includes_all_tier_checks`) that pins this — don't add a non-on-demand check without also adding it to super-exhaustive.

## Touching the watchdog

The idle-timeout and memory-kill logic in `process.py` has been the source of every "checkloop killed legitimate work" incident in the project's history. Two rules of thumb when changing it:

- **Don't reintroduce caps that this codebase already removed.** PRs #18, #19, #20 progressively removed the no-signal idle-kill cap because every kill it produced post-PRISM-2026-05-02 was a false positive. The watchdog now defers to `--check-timeout` for tier-3 (no descendants, no CPU, no JSONL). If a future change wants to re-cap, the bar is a documented incident showing genuine hangs that escape `--check-timeout`.
- **Watch the `find_all_descendant_pids` guard.** Walking from PID 0 or PID 1 returns every user process; signalling that result SIGKILLs the user's login session. The guard in `process.py` prevents this and has regressed twice already. Any change to descendant-walking must keep the guard and add a test that asserts the guard fires.

## The agent boundary

This project is built with Claude Code, runs Claude Code, and is reviewed by Claude Code. A few invariants matter:

- **No AI attribution anywhere user-visible.** Commit messages, code comments, docstrings, PR descriptions, README, error messages — none of these mention Claude, AI, LLMs, or "AI-assisted". The product stands on its own.
- **The dangerous-keyword filter is a backstop, not a policy.** It exists to refuse running prompts whose literal text would direct an LLM to do something destructive. False positives (a check prompt that mentions `DROP TABLE` while teaching Claude what to look for) get fixed by rephrasing the prompt — not by widening the filter into uselessness.
- **When checkloop drives commits in a target repo, it never pushes.** The user reviews the scratch branch and decides. This is non-negotiable — preserve it in any change to the commit flow.

## Where to read more

- `README.md` — user-facing usage, plan tiers, options
- `checks/*.md` — every check's exact prompt
- `execution_plans/*.toml` — what runs in each tier
- `prompt_templates/*.md` — fragments injected into every check
- `CLAUDE.md` — short pointer to this file for agents loading project context
