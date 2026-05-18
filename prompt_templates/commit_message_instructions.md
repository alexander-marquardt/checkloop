

If you make any git commits, follow these commit message rules:
- **Prefix the subject line with `[<check-id>] `** — the bracketed check id of the check you are currently running, lowercased, exactly as it appears in the check's frontmatter (e.g. `[security] Validate query inputs at /api/posts`, `[readability] Rename d to user_document`, `[tests-for-diff] Add regression tests for new MCP gate`). This lets downstream consumers bucket the run's commits by theme without rereading every diff. The prefix is part of the subject, not a separate header line.
- Write a 2-3 sentence description of what was changed and why, in the commit body following the subject.
- **Note inter-commit dependencies.** If your change calls, extends, or otherwise depends on code that an earlier commit in this same checkloop run introduced or modified, add a `Depends-on: <short-sha> — <one-line reason>` trailer at the bottom of the commit body. Run `git log --oneline <first-checkloop-commit>^..HEAD` first to see what came before; if a later commit ports a fix or pattern to here, name it. Best-effort: it is fine to miss one, but a present trailer is strictly better than zero signal for the downstream reviewer who will re-apply this work. Use one `Depends-on:` line per dependency.
- Do NOT use generic messages like 'test-fix', 'cleanup', or single-word summaries
- Use clear, professional commit message style
- Do NOT run 'git push' — commits must stay local for the human to review and push