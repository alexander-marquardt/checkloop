# Project Rules

## Commit Messages
- Every commit message MUST be exactly 2-3 sentences.
- Describe what was changed and why, in plain professional English.
- Do NOT mention Claude, AI, checkloop, or any AI tools in commit messages.
- Do NOT add Co-Authored-By or Signed-off-by trailers.
- Do NOT use generic messages like "test-fix", "cleanup", or single-word summaries.
- Do NOT write walls of text — keep it to 2-3 concise, readable sentences.

## Testing
- Run tests with: `uv run python -m pytest tests/ -x -q`

## Git Push Policy
- Do NOT push to a remote by default. Commit locally and stop; the user normally handles pushes themselves.
- When the user explicitly asks you to push (e.g. "push" or "push and open a PR"), a plain feature-branch push is fine — no need to re-prompt.
- Merging or pushing directly to `main` / `master` (including `gh pr merge` onto main and any force-push to main) still warrants one confirmation even when a push was authorized, since unreviewed changes on the default branch are the class of action this rule exists to prevent.
- Force-push to any branch: mention it explicitly before running.
- When checkloop is driving commits in a target project, the tool itself must never push — preserve that in any future change to its commit flow.
