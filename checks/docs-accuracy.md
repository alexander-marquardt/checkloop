---
id: docs-accuracy
label: "Documentation Accuracy"
---

Cross-reference all user-facing documentation against the actual code to find factual inaccuracies. This is NOT about adding documentation — it is about verifying that existing documentation is correct.

Check these sources for accuracy:

1. **CLI help text** — Read the argument parser (argparse, click, typer, yargs, cobra, etc.) and compare every flag name, default value, description, and example against the actual code behavior. If `--help` says a flag defaults to 10 but the code defaults to 20, fix it. If `--help` lists a flag that was removed, fix it.

2. **README and docs** — Compare CLI usage examples, configuration examples, environment variable names, file paths, and feature descriptions against the actual codebase. If the README says `config.yaml` but the code reads `settings.toml`, fix it. If the README claims a feature exists that was removed, fix it.

3. **Error messages** — Check that error messages reference correct flag names, file paths, and valid values. If an error says "use --verbose" but the flag is actually called "--debug", fix it.

4. **API documentation** — If the project has OpenAPI/Swagger specs, API docs, or endpoint documentation, compare request/response schemas, HTTP methods, URL paths, and status codes against the actual route handlers.

5. **In-app help text** — Check UI tooltips, placeholder text, onboarding copy, and inline help against actual behavior. If a tooltip says "maximum 100 characters" but the validation allows 200, fix it.

6. **Code comments referencing behavior** — Check that comments describing behavior (not implementation) are still accurate. If a comment says "retries 3 times" but the retry count was changed to 5, fix it.

For each inaccuracy found, fix the documentation to match the code (not the other way around — the code is the source of truth). Only fix the docs side unless the code is clearly buggy.

Do NOT add new documentation. Do NOT improve wording or style. Only fix factual inaccuracies where documentation contradicts code.