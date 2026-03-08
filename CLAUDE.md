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
