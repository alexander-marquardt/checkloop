# Contributing to checkloop

Thanks for your interest in contributing!

## Filing Issues

- **Bugs:** Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). Include the command you ran, what you expected, and what actually happened.
- **Feature requests:** Use the [feature request template](.github/ISSUE_TEMPLATE/feature_request.md).

## Development Setup

```bash
# Clone the repo
git clone https://github.com/alexander-marquardt/checkloop.git
cd checkloop

# Install with uv (recommended)
uv sync

# Run locally
uv run checkloop --help

# Or install in editable mode
uv pip install -e .
```

## Submitting PRs

1. Fork the repo and create a branch from `main`.
2. Make your changes. Keep them focused — one PR per feature or fix.
3. Test your changes: run `checkloop --dry-run` at minimum.
4. Submit a PR with a clear description of what changed and why.

## Code Style

- Follow the existing patterns in the codebase.
- Use type hints for function signatures.
- Keep it simple — this is a single-module CLI tool, not a framework.

## Adding Checks

New checks are welcome! Add them to the `CHECKS` list in `src/checkloop/cli.py`. Each check needs:
- `id`: short lowercase key (used in `--checks` flag)
- `label`: human-readable name for the banner
- `prompt`: the instruction sent to Claude
