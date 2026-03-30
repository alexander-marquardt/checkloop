---
id: security
label: "Security Review"
---

Do a security review. Look for: injection vulnerabilities, insecure defaults, hardcoded secrets, missing input validation, overly broad permissions, and unsafe dependencies. Fix any issues you find and explain what you changed. Be careful not to break existing behaviour when tightening security — do NOT change CORS settings, authentication config, retry policies, or client library options unless there is a clear vulnerability. Do NOT add browser security headers (X-Frame-Options, X-Content-Type-Options, Content-Security-Policy) to JSON/API-only services that don't serve HTML — these headers are ignored by API clients and add misleading complexity. Tightening security is not the same as changing operational defaults.