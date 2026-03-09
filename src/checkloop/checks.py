"""Check definitions, tier configuration, and dangerous-prompt safety guard."""

from __future__ import annotations

import re
from typing import TypedDict


class CheckDef(TypedDict):
    """A single check definition with its identifier, display label, and prompt.

    Attributes:
        id: Short identifier used on the CLI (e.g. ``"readability"``, ``"dry"``).
        label: Human-readable name shown in banners and summaries.
        prompt: The review prompt sent to Claude Code for this check.
    """

    id: str
    label: str
    prompt: str


# --- Check definitions --------------------------------------------------------
#
# Ordering matters: bookend checks (test-fix, test-validate) are first and
# last; the remaining checks are grouped by tier (basic -> thorough -> exhaustive).

CHECKS: list[CheckDef] = [
    # --- Bookend: run first ---
    {
        "id": "test-fix",
        "label": "Run Existing Tests & Fix Failures",
        "prompt": (
            "Find and run the existing test suite for this project. "
            "Use whatever test runner is already configured (pytest, jest, go test, cargo test, etc.). "
            "If tests fail, diagnose and fix the root cause in the SOURCE code — not by weakening or "
            "deleting the tests. Re-run until all existing tests pass. "
            "Do NOT write new tests in this step — only fix failures in the existing suite. "
            "Report what you found and fixed."
        ),
    },
    # --- On-demand only (not in any tier) ---
    # Positioned after test-fix so that when combined with other checks,
    # cleanup runs first and later checks can catch any regressions it introduces.
    {
        "id": "cleanup-ai-slop",
        "label": "Remove AI-Generated Code Slop",
        "prompt": (
            "Your job is to REMOVE unnecessary code, not add anything. "
            "Go through the codebase and delete AI-generated slop:\n\n"
            "1. Remove docstrings that merely restate what the function name and signature already "
            "communicate. If the function is called get_user_by_id(user_id: int) -> User, a docstring "
            "saying 'Get a user by their ID' adds nothing — delete it. KEEP module-level docstrings "
            "that explain design strategy, class docstrings that explain intent or relationships, "
            "and function docstrings that explain non-obvious behavior, side effects, or complex "
            "return values.\n\n"
            "2. Remove logger.debug() calls that log function entry or arguments already visible in "
            "request context or stack traces. Delete logging on hot paths (query builders, inner loops, "
            "per-item processing). Keep logging only at system boundaries (API entry/exit, external "
            "service calls, error paths).\n\n"
            "3. Remove try/except blocks that wrap code that cannot actually raise the caught exception "
            "(e.g. wrapping a pure data structure construction in try/except, or catching ConnectionError "
            "around code that doesn't do I/O). Misleading error handling is worse than none.\n\n"
            "4. Remove defensive null/None/undefined checks where the type system already guarantees "
            "the value is non-nullable. Remove type: ignore comments that were added to force invalid "
            "inputs in tests.\n\n"
            "5. Remove tests that exist only to hit coverage numbers — tests that pass None where types "
            "say str, tests for unreachable error paths, tests with near-duplicate names like "
            "test_boundary_conditions.py / test_boundary_edge_cases.py / test_edge_case_boundaries.py. "
            "Consolidate overlapping test files into focused, well-named ones.\n\n"
            "6. Remove unnecessary inline comments that describe what the code obviously does "
            "(e.g. '# Initialize the logger' above logger = logging.getLogger(__name__)).\n\n"
            "7. Revert any operational config changes that were made in the name of 'improvement' but "
            "actually change runtime behavior — things like CORS tightening, retry policy changes, "
            "timeout changes, or dependency removals where the dependency is still used.\n\n"
            "Run the full test suite after cleanup to ensure nothing broke. "
            "Report what you removed and how many lines were deleted."
        ),
    },
    # --- Basic tier (default) ---
    {
        "id": "readability",
        "label": "Readability & Code Quality",
        "prompt": (
            "Improve naming (variables, functions, classes), but only where the current name "
            "is genuinely confusing — do NOT rename for marginal gains or personal preference, "
            "as rename churn creates large diffs through hot paths for little value. "
            "Break up any function that does more than one logical thing, "
            "or that requires scrolling to read in full. "
            "Prefer small, named functions where the name removes the need for a comment. "
            "If any source file is longer than roughly 500 lines, split it into "
            "smaller, well-named modules with clear responsibilities — group "
            "related functions together and use imports to reconnect them. "
            "Apply the same standard to test files: split large test files "
            "so each module has a corresponding focused test file. "
            "Add module-level docstrings that explain the module's purpose and design strategy. "
            "Add class docstrings that explain intent, relationships, or non-obvious behaviour. "
            "Do NOT add docstrings to functions whose purpose is already clear from their "
            "name and signature — a function called get_user_by_id(user_id: int) -> User "
            "does not need a docstring saying 'Get a user by their ID'. "
            "Add inline comments where logic is non-obvious, but not to restate what "
            "the code already says. "
            "Ensure consistent formatting. "
            "Do NOT change any behaviour — only improve clarity."
        ),
    },
    {
        "id": "dry",
        "label": "DRY / Eliminate Repetition",
        "prompt": (
            "Find repeated or near-repeated logic. "
            "Extract shared helpers, base classes, or utility modules to eliminate "
            "duplication. Consolidate config values or magic numbers into constants. "
            "Where a module mixes multiple concerns (e.g. data models, API serialization, "
            "and validation in one file), consider extracting each concern into a focused "
            "module — but only when the separation makes each piece independently testable "
            "or reusable. "
            "Ensure each concept has a single canonical home in the code. "
            "Do NOT extract helpers for code that is only duplicated 2-3 lines or used in "
            "only 2 places — three similar lines is better than a premature abstraction. "
            "Do NOT change observable behaviour — only reduce repetition."
        ),
    },
    {
        "id": "tests",
        "label": "Write / Improve Tests",
        "prompt": (
            "Write behaviour-driven tests that verify what the code does, not how it's implemented. "
            "Cover: happy paths, meaningful edge cases, and real error conditions. "
            "Test correctness of complex logic — regex patterns, parsing, serialization, "
            "validation rules — not just that code runs without error. "
            "Write unit tests that can run without external services (databases, APIs) by "
            "using mocks or fixtures. Write integration tests separately for end-to-end flows. "
            "Do NOT write tests for defensive paths that can't actually happen "
            "(e.g. passing None where the type says str, or catching exceptions from code "
            "that can't raise them). Do NOT use # type: ignore to force invalid inputs. "
            "Avoid overlapping test files with near-identical names — each test file should have "
            "a clear, distinct purpose. "
            "Use the testing framework already in the project (or pytest/jest if none). "
            "Do NOT remove existing coverage gates or test configuration. "
            "Run the test suite and fix any failures before finishing."
        ),
    },
    {
        "id": "docs",
        "label": "Documentation",
        "prompt": (
            "Add or improve documentation: "
            "update (or create) a README section describing what was built, "
            "and document any non-obvious environment variables or config. "
            "Add module-level docstrings that explain the module's role, design strategy, "
            "and how it fits into the larger system. "
            "Add class docstrings that explain intent, usage patterns, or non-obvious behaviour. "
            "Add function/method docstrings only where the name and signature don't tell the full "
            "story — e.g. complex return values, side effects, important preconditions, or "
            "non-obvious parameter semantics. "
            "Do NOT add docstrings that merely restate the function name "
            "(e.g. 'Get a user by their ID' on get_user_by_id). "
            "Prefer comments that explain WHY and design rationale, not WHAT the code does."
        ),
    },
    # --- Thorough tier ---
    {
        "id": "security",
        "label": "Security Review",
        "prompt": (
            "Do a security review. "
            "Look for: injection vulnerabilities, insecure defaults, "
            "hardcoded secrets, missing input validation, "
            "overly broad permissions, and unsafe dependencies. "
            "Fix any issues you find and explain what you changed. "
            "Be careful not to break existing behaviour when tightening security — "
            "do NOT change CORS settings, authentication config, retry policies, or "
            "client library options unless there is a clear vulnerability. "
            "Tightening security is not the same as changing operational defaults."
        ),
    },
    {
        "id": "perf",
        "label": "Performance",
        "prompt": (
            "Review for obvious performance issues: "
            "N+1 queries, O(N²) algorithms that could be O(N) or O(N log N), "
            "missing indexes, unnecessary re-renders, "
            "blocking I/O that could be async, large allocations in loops. "
            "Add caching (@cache, @lru_cache, memoization) for expensive computations "
            "that are called repeatedly with the same inputs — especially compiled regexes, "
            "schema introspection, and config loading. Only cache where the inputs are "
            "stable and the cache won't grow unbounded. "
            "Fix anything significant and add a comment explaining the optimisation."
        ),
    },
    {
        "id": "errors",
        "label": "Error Handling",
        "prompt": (
            "Audit error handling. "
            "Ensure I/O operations, network calls, and parsing steps "
            "have proper try/except (or try/catch) with meaningful error messages. "
            "Where multiple call sites handle the same external service errors (e.g. database, "
            "API clients, message queues), consider centralizing error handling into a shared "
            "helper that logs context and raises a consistent application-level error. "
            "Only add error handling where the code can MEANINGFULLY respond to the error — "
            "do NOT wrap code in try/except when the wrapped call cannot actually raise "
            "(e.g. a function that just builds a data structure, or a connection registration "
            "that doesn't perform I/O). Misleading error handling is worse than none. "
            "Add logging only where it would help diagnose production issues."
        ),
    },
    {
        "id": "types",
        "label": "Type Safety",
        "prompt": (
            "Review for type safety issues. "
            "Add or fix type annotations (Python type hints, TypeScript types, JSDoc @param/@returns). "
            "Replace uses of Any, Object, or untyped collections with precise types. "
            "Ensure function signatures, return types, and class attributes are all typed. "
            "Where the framework supports it, use types for runtime validation at API boundaries "
            "(e.g. Annotated types with FastAPI/Pydantic constraints, Zod schemas, or "
            "class-validator decorators) — this makes the type system enforce input validation. "
            "Run the type checker (mypy, tsc, etc.) if available and fix any errors. "
            "Do NOT add complex generic types or multi-line type aliases that hurt readability — "
            "a simple Any is better than a 3-line generic constraint that is harder to understand. "
            "Do NOT change runtime behaviour beyond adding input validation at system boundaries."
        ),
    },
    # --- Exhaustive tier ---
    {
        "id": "edge-cases",
        "label": "Edge Cases & Boundary Conditions",
        "prompt": (
            "Look for unhandled edge cases and boundary conditions: "
            "off-by-one errors, empty/null/undefined inputs, integer overflow, "
            "empty collections, zero-length strings, negative numbers where unsigned expected, "
            "concurrent modification, and Unicode/encoding edge cases. "
            "Only fix edge cases that can realistically occur in production usage. "
            "Do NOT add defensive handling for inputs that the type system already prevents "
            "(e.g. null checks where the type is non-nullable, bounds checks on validated input). "
            "Fix any issues and add tests for the edge cases you find."
        ),
    },
    {
        "id": "complexity",
        "label": "Reduce Complexity",
        "prompt": (
            "Review for excessive complexity. "
            "Simplify deeply nested conditionals (flatten with early returns or guard clauses). "
            "Break apart functions with high cyclomatic complexity. "
            "Replace complex boolean expressions with named variables or helper functions. "
            "Simplify state machines, reduce the number of code paths where possible. "
            "Do NOT change observable behaviour — only reduce complexity."
        ),
    },
    {
        "id": "deps",
        "label": "Dependency Hygiene",
        "prompt": (
            "Audit the project's dependencies for issues. "
            "Identify unused dependencies and remove them, but ONLY if they are truly unused — "
            "verify that no source file imports the package before removing it. "
            "Also verify the package is not used as a CLI tool, plugin, or runtime server. "
            "Do NOT remove a dependency if any code still imports or references it. "
            "Check for outdated packages with known vulnerabilities. "
            "Flag dependencies that are unmaintained or have better alternatives. "
            "Ensure lock files are consistent with declared dependencies. "
            "Check that dependency version constraints are neither too loose nor too tight."
        ),
    },
    {
        "id": "logging",
        "label": "Logging & Observability",
        "prompt": (
            "Review for logging and observability gaps. "
            "Ensure entry points (API routes, CLI commands, queue consumers) log "
            "request/response summaries. Add structured logging with context (request IDs, "
            "user IDs, operation names) where missing. Ensure errors are logged with stack traces. "
            "Remove or downgrade noisy debug logs that would clutter production. "
            "Do NOT add logger.debug() to every function entry point — avoid logging arguments "
            "that are already visible in request context or stack traces. "
            "Do NOT add logging on hot paths (query builders, inner loops, per-item processing) "
            "where it adds overhead for minimal diagnostic value. "
            "Add metrics or timing instrumentation to performance-critical paths if appropriate."
        ),
    },
    {
        "id": "concurrency",
        "label": "Concurrency & Thread Safety",
        "prompt": (
            "Review for concurrency issues. "
            "Look for: race conditions, shared mutable state without synchronisation, "
            "deadlock potential, missing locks around critical sections, "
            "non-atomic read-modify-write sequences, and unsafe use of globals. "
            "Check async code for missing awaits, unawaited coroutines, and blocking calls "
            "in async contexts. Fix any issues you find."
        ),
    },
    {
        "id": "accessibility",
        "label": "Accessibility (a11y)",
        "prompt": (
            "Review UI code (HTML, JSX, templates, components) for accessibility issues. "
            "Ensure: semantic HTML elements are used instead of generic divs/spans, "
            "images have meaningful alt text, form inputs have associated labels, "
            "ARIA attributes are used correctly, keyboard navigation works, "
            "colour contrast meets WCAG AA standards, and focus management is correct. "
            "If the project has no UI code, report that and skip."
        ),
    },
    {
        "id": "api-design",
        "label": "API Design & Consistency",
        "prompt": (
            "Review public APIs (REST endpoints, library interfaces, CLI commands, "
            "exported functions) for consistency and usability. "
            "Check for: consistent naming conventions, predictable parameter ordering, "
            "appropriate HTTP methods and status codes, consistent error response formats, "
            "proper use of pagination, versioning where needed, and idempotency of mutating operations. "
            "Do NOT rename endpoints, change HTTP methods, or alter response shapes — "
            "these are breaking changes. Focus on parameter validation and error response consistency. "
            "Fix inconsistencies and document any breaking changes."
        ),
    },
    # --- Bookend: run last ---
    {
        "id": "test-validate",
        "label": "Validate All Tests Pass",
        "prompt": (
            "Run the FULL test suite (including any tests written or modified during earlier checks). "
            "If any tests fail, diagnose whether the failure is due to a bug in the source code "
            "or a bad test. Fix the root cause — prefer fixing source code over weakening tests. "
            "Re-run until all tests pass. "
            "Report the final test count and results."
        ),
    },
]

# All valid check IDs, derived from CHECKS to stay in sync.
CHECK_IDS: list[str] = [check["id"] for check in CHECKS]


# --- Check tiers --------------------------------------------------------------
# Tiers control which checks run at each review depth.  Each tier is a list of
# check IDs that includes the bookend checks (test-fix first, test-validate last)
# plus a progressively larger set of checks.

_BOOKEND_FIRST_CHECKS: list[str] = ["test-fix"]
_BOOKEND_LAST_CHECKS: list[str] = ["test-validate"]
_CORE_BASIC: list[str] = ["readability", "dry", "tests", "docs"]
_CORE_THOROUGH: list[str] = ["security", "perf", "errors", "types"]
_CORE_EXHAUSTIVE: list[str] = ["edge-cases", "complexity", "deps", "logging", "concurrency", "accessibility", "api-design"]

# Checks that are only run when explicitly requested via --checks, never included in tiers.
_ON_DEMAND_ONLY: set[str] = {"cleanup-ai-slop"}

# Public tier lists — used by --level and exposed for programmatic access.
TIER_BASIC: list[str] = _BOOKEND_FIRST_CHECKS + _CORE_BASIC + _BOOKEND_LAST_CHECKS
TIER_THOROUGH: list[str] = _BOOKEND_FIRST_CHECKS + _CORE_BASIC + _CORE_THOROUGH + _BOOKEND_LAST_CHECKS
TIER_EXHAUSTIVE: list[str] = [
    cid for cid in CHECK_IDS if cid not in _ON_DEMAND_ONLY
]

# Maps tier name (used by --level) to the list of check IDs for that tier.
TIERS: dict[str, list[str]] = {
    "basic": TIER_BASIC,
    "thorough": TIER_THOROUGH,
    "exhaustive": TIER_EXHAUSTIVE,
}
DEFAULT_TIER: str = "basic"


# --- Prompt constants ---------------------------------------------------------

FULL_CODEBASE_SCOPE: str = (
    "Review ALL code in this project (not just recently written code). "
    "IMPORTANT: Respect the existing codebase style. Do NOT make changes that create "
    "large diffs for marginal improvement. Avoid blanket additions (docstrings on every "
    "function, logger.debug in every method, try/except around code that can't fail). "
    "Comments and docstrings that explain non-obvious design decisions, module-level "
    "strategies, or complex interactions ARE valuable — the goal is to avoid restating "
    "what is already obvious from the code, not to avoid all documentation. "
    "Every change should be clearly justified — if in doubt, leave the existing code alone. "
)
"""Default scope prefix prepended to every check when --changed-only is not used."""

COMMIT_MESSAGE_INSTRUCTIONS: str = (
    "\n\nIf you make any git commits, follow these commit message rules:\n"
    "- Write a 2-3 sentence description of what was changed and why\n"
    "- Do NOT mention Claude, AI, checkloop, or any AI tools anywhere in the message\n"
    "- Do NOT add Co-Authored-By or Signed-off-by trailers\n"
    "- Do NOT use generic messages like 'test-fix', 'cleanup', or single-word summaries\n"
    "- Use clear, professional commit message style"
)
"""Instructions appended to every check prompt to enforce clean commit messages."""


# --- Dangerous-prompt guard ---------------------------------------------------
# Safety net: reject check prompts that contain destructive keywords.
# These are checked with word-boundary-aware regexes (see _compile_danger_patterns).

_DANGEROUS_PROMPT_KEYWORDS: list[str] = [
    "rm -rf /",
    "format c:",
    "format /dev",
    "mkfs",
    "wipe disk",
    "wipe drive",
    "wipe partition",
    "delete all files",
    "drop database",
    "drop table",
    "truncate table",
    ":(){:|:&};:",
    "sudo rm",
    "chmod 777 /",
    "/etc/passwd",
    "dd if=/dev/zero",
    "dd of=/dev",
]


def _compile_danger_patterns() -> list[re.Pattern[str]]:
    """Pre-compile regex patterns for all danger keywords.

    Adds word-boundary anchors (\\b) only at alphanumeric edges, so
    "reformat" won't match "format" but "/etc/passwd" still matches.
    """
    patterns: list[re.Pattern[str]] = []
    for keyword in _DANGEROUS_PROMPT_KEYWORDS:
        if not keyword:
            continue
        escaped = re.escape(keyword)
        leading_boundary = r"\b" if keyword[0].isalnum() else ""
        trailing_boundary = r"\b" if keyword[-1].isalnum() else ""
        patterns.append(re.compile(leading_boundary + escaped + trailing_boundary, re.IGNORECASE))
    return patterns


# Perf: compile once at import time instead of rebuilding on every call.
_DANGEROUS_PROMPT_PATTERNS: list[re.Pattern[str]] = _compile_danger_patterns()


def looks_dangerous(text: str) -> bool:
    """Check if a prompt contains any destructive keyword.

    Uses word-boundary anchors (\\b) around alphanumeric edges so e.g.
    "reformat" does not match "format", while keywords containing special
    characters like "rm -rf /" or "/etc/passwd" are still detected.
    """
    return any(pattern.search(text) for pattern in _DANGEROUS_PROMPT_PATTERNS)
