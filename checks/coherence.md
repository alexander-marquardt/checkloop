---
id: coherence
label: "Cross-Check Coherence Review"
---

Review the codebase as a whole and fix cases where earlier checks worked against each other or where their cumulative effect introduced problems that no single check would catch.

Look for these patterns:

1. **Conflicting changes** — One check added code that another check partially removed or rewrote, leaving behind inconsistent fragments. For example, error handling was added in one pass and stripped as unnecessary in another, but only partially — leaving a try block with no meaningful recovery, or a caught exception that is silently swallowed. Fix: pick the right approach (keep or remove) and apply it consistently.

2. **Cumulative over-engineering** — Each check individually added a small, defensible improvement — an abstraction, a validation layer, an extra parameter, a wrapper function. In isolation each one was fine, but together they made the code harder to follow than the original. If the total complexity grew disproportionately to the value added, simplify back. Three similar lines is better than an abstraction that only two callers use.

3. **Style drift** — The accumulated changes shifted naming conventions, code organization patterns, or idioms away from the project's existing style. New code should match the conventions of the surrounding codebase, not introduce a different style because multiple checks each nudged it in slightly different directions. Fix: align new code with the project's established patterns.

4. **Redundant layering** — Multiple checks independently addressed the same concern from different angles, resulting in belt-and-suspenders duplication. For example, input validation added by the security check that duplicates type constraints added by the types check, or error handling that duplicates what a framework already guarantees. Remove the redundant layer.

5. **Broken call chains** — A check refactored a function signature, return type, or module structure, and a later check built on the old interface or duplicated work that the refactor already handled. Fix: ensure callers and callees are consistent after all changes.

6. **Load-bearing deletions in `cleanup-ai-slop`** — Re-read the diff `cleanup-ai-slop` produced in this plan (`git show <sha>` against its commit). For every docstring, comment, log line, error handler, or piece of defensive code it deleted, ask explicitly: "did this deletion remove information that a future reader would have needed?" A docstring that explained intent ("so that X holds"), a comment that pinned an invariant ("caller must hold the lock"), a log line on an error path, a defensive check at a boundary the type system cannot cover — these are load-bearing. If a deletion was wrong, RESTORE the deleted content using `git show <commit-before-deletion>:<path>` to recover the prior version and re-apply it as your own commit with a clear message. Be especially suspicious of `cleanup-ai-slop` commits that deleted three or more docstrings in a single file with no compensating change to function names or signatures — that pattern almost always means real explanations were stripped.

Do NOT undo intentional improvements. If a check correctly extracted a helper, improved a name, tightened a type, or added necessary validation, leave it alone — even if it changed a lot of code. The goal is to catch cases where checks interfered with each other, not to revert good work.

Do NOT add new features, abstractions, or documentation. This check only fixes incoherence between prior changes.

Before flagging any cross-layer inconsistency, read the project's `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, and any `ARCHITECTURE.md` or `docs/architecture/` file. Several patterns that *look* like incoherence are in fact deliberate architectural parities, and the project docs say so explicitly. Do NOT flag any of these when they are documented or otherwise visibly intentional:

- **Frontend recompute / cross-check** — the frontend independently computes a value the backend also computes, specifically to compare and surface divergence. The duplication is the consistency check; collapsing one side onto the other destroys the property. See the same carve-out in `derived-values`.
- **Adapter / proxy parity** — a pair of modules (commonly `*_adapter` + `*_proxy`, `local_*` + `remote_*`, sync + async variants) that expose the same interface across an in-process vs network boundary. The repeated signatures are the contract that lets callers swap transports without changing call sites. See the same carve-out in `dry`.
- **Library / HTTP parity** — a library API and an HTTP endpoint that exposes the same operations. The two surfaces look redundant in isolation, but the parity is what lets one codebase serve both in-process callers and remote callers.
- **Defense-in-depth validation** — input validated at the framework layer AND again at a service boundary, by design. This is not redundant layering when the inner boundary is a trust seam (e.g. a worker that may receive jobs from sources other than the API).

When you see one of these patterns, do not "fix" it. Note it in the report as a recognised parity and move on. If the project docs do not mention the pattern explicitly but the code makes the intent obvious (matching method-by-method signatures across two files, a `crossCheck` helper, an `*_adapter` + `*_proxy` pair imported via a transport flag), treat the structural signal as documentation.

Run the test suite after making any fixes to ensure nothing broke.