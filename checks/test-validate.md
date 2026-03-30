---
id: test-validate
label: "Validate All Tests Pass"
---

Run the FULL test suite (including any tests written or modified during earlier checks). Run ALL test categories including integration tests — do not skip any. If some tests require external services that are unavailable, report this explicitly rather than treating skipped tests as passing. If any tests fail, diagnose whether the failure is due to a bug in the source code or a bad test. Fix the root cause — prefer fixing source code over weakening tests. Re-run until all tests pass. If this is a Python project and mypy is available, run it on the source tree after the tests pass. If the project has a mypy config (mypy.ini, pyproject.toml [tool.mypy], setup.cfg), use it; otherwise run `mypy --strict`. Fix any type errors mypy reports. If mypy is not available, skip this step. Report the final test count, results, and mypy outcome (or note that mypy was skipped).