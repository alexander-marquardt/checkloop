---
id: tests-for-diff
label: "Tests For This Run's Diff"
---

This check runs AFTER the behavior-modifying checks in the plan. Its single job is to make sure every behavior change introduced during this run has a regression test that pins the new behavior. Do not re-audit the rest of the codebase — focus only on what THIS run actually changed. The earlier `tests` check audited pre-existing coverage; you are closing the gap that opened between then and now.

1. **Find this run's diff.** Identify the scratch branch and its base commit. Useful starting points:
   - `git status` and `git branch --show-current` to confirm the branch name.
   - `git log --oneline -30` to see recent commits; the first commit produced by this checkloop run is usually preceded by an unrelated commit.
   - `git rev-list --max-parents=1 HEAD ^origin/main 2>/dev/null | tail -1` or look for the commit immediately before the run's first commit; treat its parent as `<base>`.
   - `git log --oneline <base>..HEAD` — the commits produced by earlier checks.
   - `git diff <base>..HEAD --stat` and `git diff <base>..HEAD` — the actual changes.
   If you cannot determine a sensible base or the diff is empty, report this and stop without writing anything.

2. **For each behavior-changing hunk, identify the unit of behavior that changed.** A unit is whatever the test framework can target: a function, a method, a class, an HTTP endpoint, a CLI subcommand, a configuration default, an emitted log line, a returned error, a database write. Skip hunks that are purely documentation, comments, formatting, type annotations on already-tested code, or whitespace — those do not need a test.

3. **For each behavior-changed unit, check whether a test pins the new behavior.** A test pins the new behavior if it both (a) exercises the changed code path with concrete inputs and (b) asserts the new output, return value, raised error, side effect, or emitted log. Look for the test in the obvious places: a `test_<module>.py` next to the source, a `<file>.test.<ext>` co-located with the source, an `e2e/`, `tests/integration/`, `cypress/`, `playwright/` spec for endpoint or UI changes. Re-use the locator strategy from the earlier `tests` check, but scope the search to files touched in step 1.

4. **For every changed unit that lacks a pinning test, write one.** Match the project's existing test framework, fixture conventions, and naming style — do NOT introduce a new test framework. Each test must:
   - demonstrate the new behavior with a concrete assertion,
   - be runnable in the project's existing CI configuration without new services or credentials,
   - fail against the old behavior (write it so that reverting the source change would break the test).
   For refactors whose intent was to preserve behavior, still pin the preserved behavior so the next change cannot quietly regress it.

5. **Do not modify the source code in this check.** This is a test-writing pass, not a re-fix pass. If a behavior change looks wrong on inspection, surface it in your final summary so the human reviewer sees it — but do not edit the source from this check. Coherence and the human review are the right place for that.

6. **If a real behavior change genuinely cannot be tested in this stack**, do NOT skip silently. Note the unit, the file, and the SPECIFIC MISSING PIECE — the framework, fixture, harness, or rig that does not exist (e.g. "no E2E rig for the upload flow", "subsystem has no mocking fixture for the auth provider"). Vague claims like "untestable here" or "hard to test" are NOT acceptable — they are the most common way the rule gets bypassed. Emit a `# TODO: regression test for <unit> — <named missing piece>` comment near the change so the gap is visible in the diff, and include the same named gap in your final summary so the reviewer can decide whether to add the missing piece or accept the hole.

7. **Run the test suite and confirm the new tests pass.** If a new test fails because the source change was incorrect or incomplete, leave the test asserting the intended behavior, mark it `xfail`/`skip` with a one-line reason if the framework supports it, and surface the failing assertion in your final summary so the human can decide. Do NOT weaken the test to make it pass, and do NOT delete it.

8. **Commit the new tests separately from any unrelated changes**, with a clear message naming the units now covered. Generic messages like "add tests" are not acceptable.

Report at the end: how many changed units you examined, how many already had pinning tests, how many tests you wrote, and any units left untested with the reason.
