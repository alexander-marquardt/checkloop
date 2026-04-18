---
id: secret-leakage
label: "Secret & PII Leakage Sweep"
---

Scan the repository and its built output for secrets, credentials, tokens, and personally identifiable information that should not be committed, logged, or bundled. This is an audit-first check: find everything, then fix. Prefer conservative fixes (mask/truncate) over destructive ones (delete the logging line entirely) unless the logging serves no purpose.

1. **Repository scan.** Grep the tree (excluding `.git/`, `node_modules/`, `vendor/`, `dist/`, `build/`) for patterns that almost always indicate secrets:
   - AWS access keys: `AKIA[0-9A-Z]{16}` or `aws_secret_access_key`
   - Generic API keys: `api[_-]?key`, `secret[_-]?key`, `bearer\s+[A-Za-z0-9_-]{20,}`
   - Stripe: `sk_live_`, `rk_live_`, `pk_live_` (the test-mode prefixes `sk_test_` / `pk_test_` are usually fine in tests, but flag if they appear in a non-test file).
   - GitHub tokens: `ghp_`, `gho_`, `ghs_`, `ghu_`, `ghr_`
   - Slack: `xoxb-`, `xoxp-`, `xoxa-`
   - Google: `AIza[0-9A-Za-z\-_]{35}`
   - OpenAI/Anthropic: `sk-proj-`, `sk-ant-`
   - JWT: `eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+` (harder to filter — investigate each match)
   - Private keys: `BEGIN (RSA|DSA|EC|OPENSSH|PGP) PRIVATE KEY`
   - Connection strings with embedded passwords: `://[^:@]+:[^@]+@`
   - `.env` files, `.pem`, `.p12`, `.key`, `id_rsa` outside `~/.ssh/` — never committed

2. **For each hit:** verify it's a real secret (not a placeholder like `xxxxx` or `your-api-key-here`). Rotate and remove. If the secret was ever committed — even if deleted now — flag it as compromised; `git log -p` keeps history. Rotation is the user's responsibility, but this check must call it out clearly.

3. **Logging scan.** Grep the source tree for log statements that include likely secret-carriers:
   - Entire request/response bodies (`logger.info(req.body)`, `log.debug(response)`)
   - Full headers dict (`log.info("headers: %s", headers)` — captures `Authorization`)
   - `process.env` or `os.environ` dumps
   - Token parameters passed to functions (`logger.info("auth", token=token)`)
   - Full user objects (these contain emails, names, hashed passwords, etc.)
   - Cookie / session data

   Fix: mask or truncate. `Bearer sk_live_abc...` becomes `Bearer sk_live_***`. Full email `user@example.com` becomes `u***@example.com`. If the log statement exists only to help debug a one-time incident and no longer serves a purpose, delete it (coordinate with the `logging` check).

4. **Client-bundle scan.** For any frontend build, check that server-only secrets don't end up in the browser bundle:
   - Framework rules: Next.js requires `NEXT_PUBLIC_` prefix to expose env vars — flag any server-side secret being read via that prefix. Vite requires `VITE_`. Create-React-App requires `REACT_APP_`.
   - If `dist/`, `build/`, or `.next/static/` exists, grep it for the same secret patterns as step 1. Any hit is a leak.

5. **PII in error responses.** Error handlers that return user-supplied input verbatim (`"User not found: " + email`) leak addressable identifiers to whoever triggered the error. Truncate or replace with an opaque ID.

6. **`.gitignore` audit.** Ensure `.env`, `.env.local`, `.env.production`, `*.pem`, `*.key`, `.aws/credentials` are gitignored. If they aren't, add them AND verify none are currently tracked (`git ls-files | grep -E '\.env$|\.pem$'`).

**What not to do:**
- Do NOT commit the secret to a "secret scanner config" file or a test — that's still leakage.
- Do NOT add blanket masking at every log site; fix the specific statements that carry sensitive data.
- Do NOT delete a log statement that genuinely helps with operational debugging — mask the sensitive parts and keep the line.

Run the test suite after changes. Report: secrets found (with file paths), logs masked, client-bundle leaks, and any secrets that require rotation.
