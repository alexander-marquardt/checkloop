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
- NEVER push anything to a remote. Commit locally only; the user handles all pushes themselves.
- NEVER push to `main` (or `master`) under any circumstance — not directly, not via force-push, not via PR merge. The user controls what lands on the default branch.
- This applies to both work on this checkloop repository and to any target project checkloop is run against.
