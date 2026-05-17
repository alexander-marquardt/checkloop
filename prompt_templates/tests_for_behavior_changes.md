

IMPORTANT — every behavior change needs a test in the same commit.

If your fix changes what code does — what it returns, what it logs, what errors it raises, what it commits, what response it sends, what state it leaves behind — you MUST add or update a test that pins the new behavior, and commit that test together with the code change. The test must demonstrate the new behavior: a test that would have failed against the old code and passes against your fix, or, for a refactor whose intent is to preserve behavior, a test that locks the behavior in so the next change cannot quietly regress it.

This is not a stylistic preference. CI does not catch "no test for new behavior" — only a human reviewer does, and human reviewers miss it under load. Without a regression test, the same bug is allowed to come back the next time someone edits this code. Treat the test as part of the fix, not as follow-up work.

How to apply:
1. Before changing code, look for the existing test that covers the current behavior. If none exists, write one against today's behavior so the gap is visible in the diff.
2. Make the code change.
3. Update or add tests so they pass against the new behavior, and would have failed against the old one. Use the project's existing test framework — do not introduce a new one.
4. Run the test suite locally and confirm the new test passes and no other tests broke.
5. Stage and commit code and tests together. Do not split them into separate commits, and do not leave the test for a follow-up commit.

If the change has genuinely no observable runtime behavior — documentation-only, comment-only, formatting-only, pure rename of already-tested code, type-annotation-only on already-tested code — the test requirement does not apply, but you MUST say so explicitly in the commit message body, in one short sentence (e.g. "no test added: rename only — existing coverage in tests/foo.test.ts", "no test added: docstring fix"). Without that note the change looks like a behavior change that quietly skipped its test, which is the exact problem this rule prevents.

If a real behavior change cannot be tested in this project's stack (no framework, no fixture for the involved subsystem, behavior only observable through a UI that has no end-to-end rig), state that in the commit message body in one short sentence and surface it in your final summary so the human reviewer knows there is a coverage gap they have to close manually. Do not silently skip the test.

Related: do not create cosmetic churn. If a candidate change is behavior-neutral (a rename for marginal clarity, a reordering that does not change semantics, an abstraction nobody asked for) and there is nothing for a test to pin, the change is probably not worth making in this pass at all. Skip it and move on to a concrete improvement.
