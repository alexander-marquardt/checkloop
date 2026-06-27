---
id: recurring-issues
label: "Recurring-Issue History Audit"
---

**THIS CHECK MAKES NO CODE CHANGES.** It produces a single advisory report and nothing else. Do not edit source files, tests, configs, or docs. Do not write test files. Do not commit anything. Do not run any history-rewriting command (`git revert`, `git reset`, `git rebase`). Your only output is the file `.checkloop-recurring-issues.md` at the root of the project being reviewed, plus a short terminal summary.

The motivation: every other check in checkloop is hermetic — it reads the working tree, the diff, and git history, and nothing else. None of them can see *which problems keep coming back*. A bug class that has been reported, fixed, and reported again is the single strongest signal that a regression test is missing or that an architectural shape is fighting the maintainers. That signal lives in the GitHub issue and pull-request history, not in the code. This check mines that history, clusters the recurring problems, and — for the highest-confidence clusters — specifies the regression test that would have caught them. It is informational and advisory: the human (and the downstream review step) reads the report and decides what to act on.

## Step 1: Confirm the data source is reachable, or self-skip cleanly

This check depends on the GitHub CLI (`gh`) and a GitHub-hosted origin. Before doing anything else, confirm all three of the following:

1. `gh` is on `PATH` (`gh --version`).
2. `gh` is authenticated (`gh auth status`).
3. The repository has a GitHub remote (`gh repo view --json nameWithOwner` resolves to an `owner/name`).

If any of these is missing, this check does **not** apply. Print one line to the terminal stating which precondition failed — for example `recurring-issues: gh CLI not authenticated — skipping (run 'gh auth login' to enable this check)` or `recurring-issues: no GitHub remote found — skipping` — write **no** report file, and stop. The absence of the report file is the signal that the audit did not run. Self-skipping silently-applicable-but-unreachable is the correct behaviour here, exactly as the migration-safety check skips a project with no migrations directory.

## Step 2: Pull a bounded slice of issue and PR history

The history can be large; bound it so this check stays fast and does not exhaust API rate limits. Retrieve the **most recent 200 closed issues and 200 merged/closed pull requests, or everything from the last 18 months, whichever is smaller.** State the exact window you used at the top of the report — never silently truncate and present the result as if it covered everything.

Useful starting commands (adapt as needed):

- `gh issue list --state closed --limit 200 --json number,title,labels,closedAt,body`
- `gh pr list --state merged --limit 200 --json number,title,labels,mergedAt,body,closingIssuesReferences`
- `gh issue list --state all --search "reopened" --json number,title,reactionGroups` to find issues that were closed and reopened — a reopen is a direct admission that the first fix did not hold.
- For a handful of strong candidates, pull the discussion: `gh issue view <n> --json title,body,comments,labels` and the linked PR diffs.

Prefer labels the project already uses (`bug`, `regression`, `incident`, `flaky`, `revert`) as a first filter, but do not rely on labels alone — many real recurrences are unlabelled and only visible in the title/body text.

## Step 3: Cluster the recurring problems

Group items that describe the **same underlying defect class surfacing more than once**. The patterns worth clustering:

- An issue that was **closed and later reopened**, or a fix PR followed weeks later by a "this broke again" issue.
- **Multiple distinct issues with the same root cause** — three separate bug reports that all trace to the same unvalidated boundary, the same race, the same off-by-one in the same module.
- **Revert chains** — a PR that reverts an earlier PR, especially if a later PR re-lands and re-breaks it.
- **Repeated regressions in one area** — a file, subsystem, or flow that shows up in bug reports far more often than its size would predict.
- **Recurring operational failures** — the same flaky test, the same deploy-time breakage, the same integration that keeps drifting.

Be disciplined about evidence. **Every cluster must cite the specific issue/PR numbers (and their dates) that compose it.** A cluster with one supporting item is not a recurrence — drop it. If the tracker is thin, noisy, or mostly feature requests with few genuine defect recurrences, say so plainly and report few or no clusters rather than manufacturing patterns. A confident-sounding but wrong recommendation costs the maintainer more than a short honest report.

## Step 4: For each confirmed cluster, recommend a fix

For each cluster, produce:

- **Root-cause hypothesis** — the single underlying defect the recurrences share, grounded in what the issues/PRs actually say. If you can only guess, mark it a guess.
- **Recommended regression test** — this is the headline output and the most actionable part. Specify it concretely enough that someone could implement it without re-reading the issues: which test file it belongs in, the scenario it exercises, the input that triggered the bug, and the assertion that would now fail if the bug returned. "Add a test for the auth flow" is useless; "in `tests/test_webhook.py`, add a test that POSTs a webhook whose signature was computed with a rotated-out secret and asserts a 401, covering the replay reported in #214 and #287" is useful.
- **Architectural note (optional, advisory, lower-confidence)** — only when the recurrences point to a structural cause that a regression test alone won't fix (a value derived in two places that keep drifting, a missing validation boundary, a layering inversion). Keep this clearly marked as advisory and lower-confidence than the test recommendation — a fundamental architectural change suggested from issue history is a hypothesis for a human to weigh, never a directive, and it must never be auto-applied. When a regression test fully addresses the cluster, say so and omit the architectural note.
- **Confidence** — high / medium / low, with one line of reasoning. High means the recurrence is unambiguous (an explicit reopen or revert chain) and the fix is clear. Lower it when you are inferring the link between items or the tracker is sparse.

## Step 5: Prioritise and write the report

Rank clusters by confidence and by how likely the recurrence is to bite again. A short, well-evidenced list beats a long speculative one — five real recurrences are worth more than twenty maybes.

Write the report to `.checkloop-recurring-issues.md` at the repository root, using this structure:

```markdown
# Recurring-Issue History Audit

Generated: <ISO date>
Repository: <owner/name>
History window: <e.g. "most recent 200 closed issues + 200 merged PRs (oldest: 2025-01-14)">

## Summary
<one or two sentences: how many recurring clusters were found, and the headline takeaway>

## Recurring clusters

### 1. <short title> — confidence: high | medium | low
**Evidence:** #<n> (<date>), #<n> (<date>), … <one phrase on what links them, e.g. "issue #112 reopened twice", "PR #340 reverted PR #298">
**Root cause:** <one or two sentences>
**Recommended regression test:** <file + scenario + trigger input + assertion>
**Architectural note:** <only if a test won't fully address it; clearly advisory — otherwise omit this line>

### 2. ...

## Notes
<anything the maintainer should know about the limits of this audit — sparse tracker, label noise, window truncation, items that looked related but couldn't be confirmed>
```

If you found no genuine recurrences, write a short report that says so explicitly and names the window you searched — do not pad it with weak clusters.

## Step 6: Surface it

Print a compact version of the cluster list to the terminal (title + confidence + evidence issue numbers for each) so the reviewer sees the headline without opening the file, followed by the total cluster count. Use neutral language throughout — the report and terminal output MUST NOT mention AI, LLMs, Claude, or checkloop.

Do NOT implement any of the recommendations yourself — this is advisory. The downstream review step evaluates each recommended test and architectural note when deciding what to adopt; your job is an accurate, conservative, well-evidenced record, not enforcement.
