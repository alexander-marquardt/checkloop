# Contributing to claudeloop

Thanks for your interest in contributing!

## Filing Issues

- **Bugs:** Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include the command you ran, what you expected, and what actually happened.
- **Feature requests:** Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).

## Development Setup

```bash
# Clone the repo
git clone https://github.com/alexander-marquardt/claudeloop.git
cd claudeloop

# Install with uv (recommended)
uv sync

# Run locally
uv run claudeloop --help

# Or install in editable mode
uv pip install -e .
```

## Submitting PRs

1. Fork the repo and create a branch from `main`.
2. Make your changes. Keep them focused — one PR per feature or fix.
3. Test your changes: run `claudeloop --dry-run` at minimum.
4. Submit a PR with a clear description of what changed and why.

## Code Style

- Follow the existing patterns in the codebase.
- Use type hints for function signatures.
- Keep it simple — this is a single-module CLI tool, not a framework.

## Adding Review Passes

New review passes are welcome! Add them to the `REVIEW_PASSES` list in `src/claudeloop/cli.py`. Each pass needs:
- `id`: short lowercase key (used in `--passes` flag)
- `label`: human-readable name for the banner
- `prompt`: the instruction sent to Claude
