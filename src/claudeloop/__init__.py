"""claudeloop — Autonomous multi-pass code review using Claude Code.

This package provides a CLI tool that orchestrates multiple focused review
passes over a codebase using the Claude Code CLI. Each pass targets a
specific quality dimension (readability, DRY, tests, security, etc.) so
the model can focus deeply on one concern at a time.

Public API:
    cli.main()        — CLI entry point (also available as the ``claudeloop`` command).
    cli.run_claude()  — Run a single Claude Code review pass programmatically.
"""
