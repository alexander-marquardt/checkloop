---
id: cleanup-ai-slop
label: "Remove Unnecessary Code & Noise"
---

Your job is to REMOVE unnecessary code, not add anything. Go through the codebase and delete low-value noise:

1. Remove a docstring ONLY when it is a literal restatement of the function's name and signature, with nothing else of substance. The default is to KEEP — a surviving useful docstring is far less costly than a deleted load-bearing one.

   Concrete bar. Delete a docstring only when it contains NONE of the following:
   - A "because", "so that", "in order to", "this is needed because", or similar rationale clause that explains intent rather than action.
   - A reference to invariants, ordering requirements, side effects, atomicity, idempotency, or thread-safety guarantees ("must be called before X", "leaves Y in state Z", "callers must hold the lock", "safe to call multiple times", "not re-entrant").
   - Notes about non-obvious return semantics, error conditions, or behavior on edge cases ("returns None when ...", "raises X if ...", "empty list means no match, not error", "swallows ValueError and falls back to ...").
   - Examples, sample inputs, sample outputs, or doctest blocks.
   - References to a bug, incident, PR, ticket, or another file ("see X.md for the rationale", "fix for #1234", "matches the schema in foo.py").

   If ANY of those appear, the docstring is doing real work — KEEP IT. When in doubt, KEEP. The bar is not "could this be inferred from the code"; it is "is the docstring literally just the name in prose".

   Contrastive examples — internalize these before deleting anything:

   DELETE — pure restatement, no rationale or invariant:
       def get_user_by_id(user_id: int) -> User:
           """Get a user by their ID."""

   DELETE — __init__ just restating parameter names:
       def __init__(self, host: str, port: int) -> None:
           """Initialize with host and port."""

   KEEP — explains WHY (the "so that" / intent clause is load-bearing):
       def _operator_conflict_category(op: Operator) -> str:
           """Bucket operators by the conflict surface they share so the
           validator can report 'X and Y disagree on Z' rather than just
           'X is invalid'."""

   KEEP — pins an invariant the type signature cannot express:
       def wrap_function_score(query: Query) -> FunctionScoreQuery:
           """Wrap a leaf query so downstream boost composition can multiply
           into the score without rebuilding the parent. Caller must not
           re-wrap an already-wrapped query — that would double the boost."""

   KEEP — non-obvious return semantics:
       def _build_operator_info(op: str) -> OperatorInfo | None:
           """Returns None for synthetic operators ('ANY', 'ALL') that have
           no schema entry; callers must handle None explicitly instead of
           treating it as 'unknown operator'."""

   Remove `__init__` docstrings that ONLY restate parameter names. KEEP module-level docstrings that explain design strategy, KEEP class docstrings that explain intent or relationships, and KEEP any function docstring matching the KEEP rules above. If you are about to delete three or more docstrings in a single file, stop and re-read each one — that pattern is usually a sign you are stripping intent, not noise.

2. Remove logger.debug() calls that log function entry or arguments already visible in request context or stack traces. Keep logging at system boundaries (API entry/exit, external service calls, error paths). EXCEPTION: if the project's CLAUDE.md, AGENTS.md, or similar policy file explicitly mandates specific debug logging (for example "log generated ES queries at debug level", "trace external API calls", or any rule that names the code pattern that must exist), those log calls MUST be preserved. If the motivation for removing such a log is performance — typically because an argument is eagerly serialized (e.g. `query.to_dict()`, `json.dumps(payload)`, f-string formatting of a large object) before the logging framework checks the level — DO NOT delete the log. Instead, guard it lazily: wrap the call in `if logger.isEnabledFor(logging.DEBUG):`, move the expensive conversion inside that block, or pass a callable / `%s` + lazy object that only formats when the log is actually emitted. Deleting a mandated log in the name of performance is a policy violation; lazy evaluation is the correct fix.

3. Remove try/except blocks that wrap code that cannot actually raise the caught exception. Read the called function's source to check whether it actually performs I/O before assuming it can raise IOError/ConnectionError. For example, a function that registers a connection in a dict doesn't do I/O even if the word 'connection' is in its name. Misleading error handling is worse than none.

4. Remove defensive null/None/undefined checks where the type system already guarantees the value is non-nullable. Remove type: ignore comments that were added to force invalid inputs in tests.

5. Remove tests that exist only to hit coverage numbers — tests that pass None where types say str, tests for unreachable error paths, tests with near-duplicate names like test_boundary_conditions.py / test_boundary_edge_cases.py / test_edge_case_boundaries.py. Remove test files with names like test_*_coverage.py or test_*_extended.py that suggest iterative generation. Remove test files that reference source line numbers in comments. Consolidate overlapping test files into focused, well-named ones.

6. Remove unnecessary inline comments that describe what the code obviously does (e.g. '# Initialize the logger' above logger = logging.getLogger(__name__)). Do NOT remove blank lines before section separator comments (# ---, # ===, etc.) or between logical groups — these are style conventions that linters may enforce.

7. Revert any operational config changes that were made in the name of 'improvement' but actually change runtime behavior — things like CORS tightening, retry policy changes, timeout changes, or dependency removals where the dependency is still used. Remove browser security headers (X-Frame-Options, X-Content-Type-Options, CSP) from JSON/API-only services that don't serve HTML — these headers are ignored by API clients and add misleading complexity.

8. **Remove AI-attribution leakage from existing artefacts.** If the project's `CLAUDE.md`, `AGENTS.md`, `CONTRIBUTING.md`, or README states a no-AI-mentions rule (typical phrasings: "no mention of Claude / AI / LLMs", "no `Co-Authored-By` trailers attributing AI tools", "the work product stands on its own"), then any AI-attribution string that has slipped into the codebase is the kind of noise this check exists to clean up. Search code comments, docstrings, README, in-repo docs (`docs/`, `*.md`), error messages, log strings, CHANGELOG entries, and any user-visible text for:

   - `Co-Authored-By: Claude` / `Co-Authored-By: <AI tool>` trailers in committed text (commit *messages* are immutable history — leave those; we are only fixing what lives in tracked files).
   - "Generated by Claude", "Generated with Claude Code", "AI-generated", "AI-assisted", "with help from <AI tool>", "🤖 Generated with …" or equivalent attributions.
   - References to Claude, ChatGPT, GPT-4, Copilot, Cursor, or any LLM/tool name in a context that attributes authorship or assistance rather than describing the tool as a subject of the code itself. (A module that integrates with Anthropic's API legitimately mentions Claude in its source — the test is whether the mention is *attribution* or *content*.)
   - TODO/FIXME comments that name an AI as the responsible party ("Claude added this — please review").

   Delete these strings (and the lines that contained nothing else) without ceremony. Do not preserve them behind a comment, do not rewrite them as "this was machine-generated" — just remove. If a Markdown badge or footer chunk only exists to attribute generation, remove the whole block. Where the rule lives in project docs, this is policy enforcement, not aesthetic preference — treat it as mandatory.

   If the project does NOT have a no-AI-mentions rule, leave existing attribution alone. The trigger is the project policy, not a generic preference.

IMPORTANT: In commit messages and code comments produced by this check, describe changes in neutral terms. Say "removed redundant docstrings" or "deleted unnecessary error handling", NOT "removed AI-generated slop" or similar. Do not reference AI, LLMs, or automated generation in any output you write — this rule applies to the check's own output regardless of whether the target project has a no-AI-mentions policy.

Run the full test suite after cleanup to ensure nothing broke. Report what you removed and how many lines were deleted.