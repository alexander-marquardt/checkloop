---
id: logging
label: "Logging & Observability"
---

Review for logging and observability gaps. Ensure entry points (API routes, CLI commands, queue consumers) log request/response summaries. Add structured logging with context (request IDs, user IDs, operation names) where missing. Ensure errors are logged with stack traces. Remove or downgrade noisy debug logs that would clutter production. Do NOT add logger.debug() to every function entry point — avoid logging arguments that are already visible in request context or stack traces. Do NOT add logging on hot paths (query builders, inner loops, per-item processing) where it adds overhead for minimal diagnostic value. Add metrics or timing instrumentation to performance-critical paths if appropriate.