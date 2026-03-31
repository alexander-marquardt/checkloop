---
id: cleanup-ai-slop
label: "Remove Unnecessary Code & Noise"
---

Your job is to REMOVE unnecessary code, not add anything. Go through the codebase and delete low-value noise:

1. Remove docstrings that merely restate what the function name and signature already communicate. If the function is called get_user_by_id(user_id: int) -> User, a docstring saying 'Get a user by their ID' adds nothing — delete it. Remove __init__ docstrings that just restate the parameter names. KEEP module-level docstrings that explain design strategy, class docstrings that explain intent or relationships, and function docstrings that explain non-obvious behavior, side effects, or complex return values.

2. Remove logger.debug() calls that log function entry or arguments already visible in request context or stack traces. Delete logging on hot paths (query builders, inner loops, per-item processing). Keep logging only at system boundaries (API entry/exit, external service calls, error paths).

3. Remove try/except blocks that wrap code that cannot actually raise the caught exception. Read the called function's source to check whether it actually performs I/O before assuming it can raise IOError/ConnectionError. For example, a function that registers a connection in a dict doesn't do I/O even if the word 'connection' is in its name. Misleading error handling is worse than none.

4. Remove defensive null/None/undefined checks where the type system already guarantees the value is non-nullable. Remove type: ignore comments that were added to force invalid inputs in tests.

5. Remove tests that exist only to hit coverage numbers — tests that pass None where types say str, tests for unreachable error paths, tests with near-duplicate names like test_boundary_conditions.py / test_boundary_edge_cases.py / test_edge_case_boundaries.py. Remove test files with names like test_*_coverage.py or test_*_extended.py that suggest iterative generation. Remove test files that reference source line numbers in comments. Consolidate overlapping test files into focused, well-named ones.

6. Remove unnecessary inline comments that describe what the code obviously does (e.g. '# Initialize the logger' above logger = logging.getLogger(__name__)). Do NOT remove blank lines before section separator comments (# ---, # ===, etc.) or between logical groups — these are style conventions that linters may enforce.

7. Revert any operational config changes that were made in the name of 'improvement' but actually change runtime behavior — things like CORS tightening, retry policy changes, timeout changes, or dependency removals where the dependency is still used. Remove browser security headers (X-Frame-Options, X-Content-Type-Options, CSP) from JSON/API-only services that don't serve HTML — these headers are ignored by API clients and add misleading complexity.

IMPORTANT: In commit messages and code comments, describe changes in neutral terms. Say "removed redundant docstrings" or "deleted unnecessary error handling", NOT "removed AI-generated slop" or similar. Do not reference AI, LLMs, or automated generation in any output.

Run the full test suite after cleanup to ensure nothing broke. Report what you removed and how many lines were deleted.