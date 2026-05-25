

If you make any git commits, follow these commit message rules:

- **Pre-emit reader-value gate (run this BEFORE you stage anything for commit).** For each prospective commit, answer in one sentence: *what observable thing improves for a reader of this codebase, or what reader-confusion gets resolved?* Valid answers include a correctness fix, a perf improvement, new or better test coverage, an error message that now tells the operator what to do next, a previously-cryptic identifier renamed to something self-explanatory, an over-deep nesting flattened. A rewrite that swaps one idiomatic form for another with no behaviour change and no reader-confusion fix — e.g. `for (let i++)` → `Array.entries()` purely as style — is NOT a valid answer; the project's no-net-neutral-churn rule explicitly rejects these. If you cannot complete the sentence, drop the change. This gate is the same FIRST-CLASS RULE that opens `full_codebase_scope.md`, applied at commit time as a self-check.

- **Match the target repo's commit-subject convention; do not impose a foreign one.** Before writing your first commit subject in this run, read the last ~20 commit subjects on the branch you forked from (`git log --oneline <base>..HEAD` if you have commits, otherwise `git log --oneline -20`). Conform to what you see:
  - Sentence-case subjects with no prefix (e.g. `Refactor boost_config to drop the legacy adapter`) → write the same shape.
  - Conventional commits (e.g. `fix: handle empty user list`, `feat(api): add pagination`) → use the matching type and optional scope.
  - Bracketed prefixes already in use (e.g. `[ci] Update workflow`, `[deps] Bump httpx to 0.27`) → a `[<check-id>] ` prefix fits in naturally; use it.
  - No clear convention → default to sentence-case.

  Imposing a foreign convention forces the human reviewer to rewrite every subject on adoption — wasted work, and exactly the kind of friction this rule exists to remove.

- **Always include a `Check: <check-id>` trailer at the bottom of the commit body.** The check id is the value from the running check's frontmatter (e.g. `Check: readability`, `Check: security`). This is the canonical, format-independent way downstream consumers (`commit-audit`, the post-run review prompt, your own `git log --grep "Check: readability"`) group the run's commits by theme. The trailer goes on its own line at the very end of the body, after any `Depends-on:` lines. Always present even if your subject style already carries a bracketed prefix — the trailer is what every downstream consumer relies on regardless of subject convention.

- **Two-tier body.** For trivial commits — typo fixes, dep bumps, pure renames of already-tested code, mechanical reformats — 2-3 sentences describing what changed and why is both the floor and the ceiling. For any commit where a non-trivial call was made (the spec was ambiguous, the implementation diverged from the obvious shape, an alternative was rejected, or an open question remains), expand the body with labelled lines so the audit trail survives squash / rebase and is greppable from `git log`:
  - `Design decisions:` — spec ambiguity + the call you made + the constraint that pointed to it.
  - `Derivations:` — where the implementation diverged from the obvious / spec'd shape and why.
  - `Tradeoffs:` — alternatives considered + why this one won. Naming the rejected option is what makes this an audit trail rather than an advertisement.
  - `Open question:` — anything you'd want a maintainer to sign off on, revisit, or change later. Prefix on its own line so `git log --grep "Open question:"` finds them.
  - `Rationale:` — the *why* the code itself cannot express — constraint, threat model, past incident, trade-off.

  Not every label appears in every non-trivial commit; only those that earned their keep. The pair rule of thumb: if a future maintainer would be annoyed to discover you made this decision without telling them, write it down.

- **Note inter-commit dependencies.** If your change calls, extends, or otherwise depends on code that an earlier commit in this same checkloop run introduced or modified, add a `Depends-on: <short-sha> — <one-line reason>` trailer above the `Check:` trailer. Run `git log --oneline <first-checkloop-commit>^..HEAD` first to see what came before; if a later commit ports a fix or pattern to here, name it. Best-effort: it is fine to miss one, but a present trailer is strictly better than zero signal for the downstream reviewer who will re-apply this work. Use one `Depends-on:` line per dependency.

- **Run pre-commit BEFORE each commit, not just at the end of the run.** If the target project has a `.pre-commit-config.yaml` / `husky` / `lefthook` config, explicitly invoke it on the staged files before each `git commit` — for example `pre-commit run --files <files>` or `pre-commit run` for the staged set. If the project lacks a configured pre-commit toolchain but uses formatters (ruff, black, prettier, eslint, gofmt), run those equivalently. Auto-fixes that the run produces must be folded into the same commit that prompted them: re-stage and re-commit (or `--amend` before pushing). The failure mode this rule defends against is the chain `[checkA] do X` / `[checkB] do Y` / `[checkC] do Z` / `[coherence] apply pinned pre-commit formatters` — that fourth commit should not exist; each of the first three should have landed formatted-on-arrival.

- **Pre-commit auto-fixes ride with the substantive commit, never alone.** Same direction as the previous rule, applied to the unlikely-but-possible case where pre-commit modifies files that the current check did NOT otherwise touch (an F401 unused-import strip on an unrelated file because the project's hook is configured to fix all files): drop those changes (`git checkout -- <file>`) rather than carving them into a standalone "apply pre-commit auto-fixes" commit. Standalone auto-fix commits are zero-signal noise that lands naturally next time anyone touches the affected file. The only exception is when pre-commit is fixing something in direct response to a real source change you made; then the fix stays with that commit, by construction.

- Do NOT use generic messages like 'test-fix', 'cleanup', or single-word summaries.
- Use clear, professional commit message style.
- Do NOT run 'git push' — commits must stay local for the human to review and push.