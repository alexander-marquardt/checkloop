---
id: contributing-conformance
label: "CONTRIBUTING Conformance Audit"
---

This is an ADVISORY check. It MUST NOT modify source code, MUST NOT add, remove, or rewrite tests, and MUST NOT run `git revert`, `git reset`, `git rebase`, or any history-rewriting command. Its single job is to read the target project's own contributor rules **in full** and audit the changes this checkloop run produced against them, recording any violations for the human (and the downstream review agent) to evaluate before the work is adopted.

The motivation: a project's `CONTRIBUTING.md` encodes hard-won, project-specific rules — the failure modes that have actually shipped as bugs there — that the generic check suite cannot know. Those files are injected into other checks' prompts only up to a size cap, so the rules deep in a long `CONTRIBUTING.md` reach no other check. This check reads them whole and is the dedicated conformance pass.

## Step 1: Read the house rules in full

Read every contributor-standards file present at the repository root, **in their entirety** (do not stop at a size cap): `CONTRIBUTING.md` first, then `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, and any similar root policy file (`STYLEGUIDE.md`, `ENGINEERING.md`, `CODE_OF_CONDUCT.md` is not in scope). If a `CONTRIBUTING.md` references other policy docs (e.g. `docs/` rules), read the ones it treats as binding.

If none of these files exist, report "no contributor-standards files found — nothing to audit" and stop without writing a report.

## Step 2: Extract the auditable rules

From what you read, build the list of rules that are **checkable against a code change**. Keep only rules that a diff can violate:

- Code/architecture invariants (layer boundaries, "X is the source of truth", required call patterns, banned shapes).
- Testing rules (test required for every behaviour change, regression test for every bug fix, required coverage tiers).
- Hygiene rules (no AI/tool attribution, no net-neutral churn, no silenced failures, no backwards-compat shims, no committed generated artifacts, no proprietary data).
- Documentation/rationale rules that apply to changed code.
- Data/migration rules (version-don't-overwrite, expand-and-contract, etc.) when the diff touches those areas.

**Explicitly ignore process and workflow guidance** — these are not diff violations and must never be reported: CI check lists, merge-queue / branch-protection procedure, "wait for N checks", worktree policy, how to open a PR, review-approval mechanics, release cadence. Auditing those produces noise. You are checking the *code change*, not how it will be merged.

If the project already enforces a rule mechanically (e.g. a committed audit-grep script or CI lint), do not re-run or duplicate it — focus on the judgment rules a grep cannot decide, which is exactly what the human is expected to read the diff for.

## Step 3: Scope to this run's changes

Identify the scratch branch and its base commit (the same way `tests-for-diff` and `commit-audit` do) and audit `git diff <base>..HEAD` plus the run's commit messages. If there is no scratch branch or no commits, report this and stop without writing a report. You are auditing **what this run changed**, not pre-existing violations elsewhere in the repo (a pre-existing violation in untouched code is out of scope unless the diff newly depends on it).

## Step 4: Judge conservatively

For each candidate violation, confirm it against the verbatim rule text before recording it. Be conservative: if a change plausibly complies, do not flag it. A false positive that sends the reviewer chasing a non-issue costs more than it saves. Prefer precision over recall here — the boundary and other dimension checks already cover breadth.

## Step 5: Write the report (only if there are violations)

If you found **one or more** violations, write `.checkloop-contributing-audit.md` at the repository root with this layout:

    # CONTRIBUTING Conformance Audit
    Scratch branch: <branch>   Base: <short-sha>
    Sources audited: <comma-separated filenames>
    Violations found: <N>

    ## Violation 1 — <short title>
    Where: <path:line>, or commit <short-sha> for a commit-message/provenance rule
    Rule: "<verbatim quote of the violated rule>" (<source file> §<section or nearest heading>)
    Severity: high | medium | low
    What: <one or two sentences — what in the diff violates the rule>
    Remedy: <the smallest change that brings it into compliance, or "drop this change">

Repeat one `## Violation N` block per violation, highest severity first. Use neutral language throughout; the report MUST NOT mention AI, LLMs, Claude, or checkloop.

If you found **zero** violations, do NOT write the file. Print a single line to the terminal: "CONTRIBUTING conformance: no violations found against <sources>." The absence of the report file is the signal that the diff is clean.

## Step 6: Surface it

Print the same violation list to the terminal so the reviewer sees it without opening the file, with each `high`-severity item visually distinct (prefix the line with `[FLAG]`). End with the total count.

Do NOT fix the violations yourself — this is advisory. The downstream review step evaluates each flagged violation when deciding whether to adopt the corresponding change; your job is an accurate, conservative record, not enforcement.
