---
id: complexity
label: "Reduce Complexity"
---

Review for excessive complexity. Simplify deeply nested conditionals (flatten with early returns or guard clauses). Break apart functions with high cyclomatic complexity. Replace complex boolean expressions with named variables or helper functions. Simplify state machines, reduce the number of code paths where possible. Do NOT change observable behaviour — only reduce complexity.

## Before extracting from a file that has moved since the review base

When you decide to extract a helper from a file, first check how stable that file is on the upstream branch. In a checkloop run, the *review base* is the branch the run is reviewing (typically `origin/main`); the *scratch branch* is where commits land. Before committing an extraction, run:

```bash
git log --oneline <review-base>..origin/<upstream-branch> -- <file>
```

If `<file>` has moved substantially on upstream since the review base — particularly if it has been renamed, restructured, or had nearby code re-organised — the extraction you are about to land will likely need manual re-application by the human reviewer against current upstream HEAD. The work product is still useful (the extraction is a real readability win) but the divergence risk needs to be visible.

Do NOT skip the extraction in this case — make it. But add an `Open question:` line to the commit body naming the divergence:

> Open question: this extraction was made against base `<short-sha>`, which is behind `origin/main` by N commits on `<file>` (rename, restructure, etc.). The human reviewer may need to re-apply the extraction against current HEAD; the patch as written will not apply cleanly.

The `Open question:` prefix is the format documented in the commit-message rules — it lands the warning where `git log --grep "Open question:"` and the post-run reviewer will both find it.