---
id: commit-audit
label: "Audit This Run's Commits"
---

This is an ADVISORY check. It MUST NOT modify source code, MUST NOT add or remove tests, and MUST NOT run `git revert`, `git reset`, `git rebase`, or any other history-rewriting command. Its single job is to read every commit this checkloop run produced and classify it so the human reviewer can drop or fold commits that earned no value.

1. **List this run's commits.** Identify the scratch branch and its base commit (the same way `tests-for-diff` does). Use `git log --oneline <base>..HEAD` to list them. If there is no scratch branch or there are no commits, report this and stop without writing anything.

   When you need to know which check produced a given commit (for the report's bucketing), look at two signals on each commit in this order: (a) a `Check: <check-id>` trailer in the body — the format the commit-message instructions now mandate, format-independent of subject convention — and (b) a `[<check-id>] ` prefix on the subject line, which older runs and runs against repos with a bracket-prefix convention still use. Either signal is authoritative when present; commits with neither are ungrouped and should be flagged in the report as missing provenance.

2. **Classify each commit as exactly one of these categories:**

   - **A — Behavior change + test.** The commit changes runtime behavior and the same commit adds or updates a test that pins the new behavior.
   - **B — Behavior change, NO test.** Same as A but without a test. Flag prominently. Check the commit body: if it explicitly says "no test added: <reason>" the human has acknowledged the gap; otherwise the gap is silent and the rule was skipped.
   - **C — Bug fix + regression test.** The commit fixes a bug and includes a test that would have failed before the fix.
   - **D — Bug fix, NO regression test.** Same as C without the test. Flag prominently. Apply the same "no test added" check as B.
   - **E — Readability or maintainability win.** A meaningful clarity improvement: a rename whose old name was actually confusing, a flattened deep nesting, a helper extracted with at least two real callers, a removed dead branch. No new test needed if existing coverage still exercises the behavior.
   - **F — Documentation, comment, formatting, or type-annotation-only.** No observable runtime change. No test needed. The commit body must say so explicitly; if it does not, note that omission in your report.
   - **G — NET-NEUTRAL CHURN.** The commit makes the code different without making it better: a rename for marginal clarity, a reordering with no semantic effect, an abstraction with one caller, a deleted docstring or comment whose deletion did not improve readability. Zero behavior change, zero readability win for a future reader. The reviewer should drop or fold this commit. Apply the rename-revert test: if reverting every identifier rename in the commit leaves zero substantive content, classify G regardless of how much clearer the new names read — identifier-rename-only commits are the most common pattern reviewers reject.

3. **Write a report file `.checkloop-commit-audit.md` at the repo root.** Layout:

   - A short header naming the scratch branch and the base commit.
   - A "Flagged for your attention" section listing every commit classified B, D, or G with a one-line rationale and the recommended command to act on it.
   - A "Full classification" section with one entry per commit, in commit order, of the form:

         ## <short-sha> — <commit subject>
         Classification: <A/B/C/D/E/F/G>
         Rationale: <one or two sentences>
         Recommended action: <"keep" | "keep and write a follow-up test for X" | "drop with `git revert <sha>`" | "fold into <other-sha> with an interactive rebase">

   The file content uses neutral language and never mentions AI, LLMs, or checkloop.

4. **Print the same classification table to the terminal**, in the same order, so the reviewer sees it without opening the file. Make B, D, and G visually distinct (e.g. prefix the line with `[FLAG]`).

5. **Do NOT execute any drop, revert, fold, or rebase**, even if every signal points that way. The closest you may come to action is providing the exact command in "Recommended action". Auto-revert risks losing real work to a misclassification — the human must look at the report and decide.

6. **Be conservative.** If a commit could be E (readability win) or G (net-neutral), prefer E. If it could be A (behavior + test) or C (bug fix + regression test), pick the more accurate one. Reserve G for commits where you can affirmatively say "removing this commit would not lose anything a future reader or user would notice."

Report at the end: the totals per category and the count of commits flagged (B, D, G combined). The `.checkloop-commit-audit.md` file is the canonical record — the terminal print is for immediate visibility.
