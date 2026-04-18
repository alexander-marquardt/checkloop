---
id: dead-code
label: "Dead Code & Unused Exports"
---

Find and remove code that is no longer reachable, imported, or referenced. LLM-generated code accumulates orphans quickly — exported helpers that are never imported, feature flags that no longer gate anything, components that used to be rendered but no longer are. Be precise: do not delete code that is only referenced dynamically or via string lookup unless you have verified the lookup is also gone.

1. **Unused exports.** For each language, enumerate exported symbols and grep the codebase for their importers. If a symbol is exported from `src/foo.ts` as `bar` and has zero importers anywhere in `src/`, `tests/`, or config, it is a candidate for deletion. Use the language-native tooling first when available:
   - TypeScript/JavaScript: `ts-prune`, `knip`, or a plain grep for `import.*<name>`.
   - Python: `vulture` (install if not present, then remove after the audit), or grep for `from <module> import <name>`.
   - Go: `staticcheck` catches unused exported identifiers in internal packages.

2. **Orphaned files.** Files in the source tree that no module imports. A file that is only imported by its own test file counts as orphaned *unless* the test is integration-level (exercises public behaviour) and the file is a public entry point. React pages/routes registered in a router file count as imported via the router. CLI subcommand modules registered in the command table count as imported. Use the import graph to decide, not the file name.

3. **Unreachable branches.** Code inside `if (false)`, `if (FEATURE_X)` where `FEATURE_X` is hardcoded to false, or after an unconditional `return`/`throw`. Includes `else` branches when the `if` always returns. Flag `TODO: remove after <date>` markers where the date has passed.

4. **Stale feature flags.** Flag references in code (`if (flags.newCheckout)`) where the flag is no longer defined in the flag service, config, or env. Remove the flag reference AND the old code path. Be careful: remove only the path the flag was hiding — the default path stays.

5. **Dead test fixtures.** Fixture files, mocks, or factory helpers that no test currently imports. Fixture-drift is covered by a separate check, but orphaned fixtures are fair game here.

6. **Commented-out code.** Multi-line blocks of commented code older than a few weeks (check git blame). A single `// TODO` note is fine; `// function oldLogin() { ... }` is not.

**Safety rules:**
- Before deleting an exported symbol, confirm it is not part of the package's public API (check `package.json` `exports`, `setup.py` entry points, or the library's documented surface). Public-API removal is a breaking change — flag for human review, do not delete.
- Dynamic imports (`require(variableName)`, `importlib.import_module(name)`, reflection-based lookup) hide dependencies from static analysis. Grep for string literals matching the symbol name before deleting.
- Test-only exports that exist solely to let tests reach internals are still "used" — don't delete them just because no `src/` file imports them.
- Don't delete something because *this single check* can't find a caller. Double-check with at least one grep.

Run the test suite, type checker, and linter after each batch of deletions. Commit in small batches so regressions are easy to revert. Report total lines removed and files deleted.
