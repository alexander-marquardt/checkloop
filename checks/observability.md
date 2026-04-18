---
id: observability
label: "Observability on Critical Paths"
---

Verify that critical code paths — authentication, payments, data mutations, external API calls, background jobs — are observable in production. The bar is: if this call fails at 3am, does someone know? If it silently returns the wrong result, can an engineer reconstruct what happened? This is distinct from the `logging` check (which focuses on log quality/placement); this one asks whether the *outcomes* are watched.

1. **Identify critical paths.** Find handlers and services that touch:
   - Auth/session creation, password resets, token issuance
   - Payment, billing, refunds, credit application
   - Any write to durable storage that mutates user-owned data
   - External API calls (payment processors, email providers, third-party auth)
   - Background jobs, scheduled tasks, queue consumers
   - Data export/import, migrations run at runtime

2. **Check each critical path for three layers of observability:**
   - **Structured logs at entry, success, and failure.** Not a single `console.log` — a log line that includes correlation ID, user ID (when safe), and enough context to reconstruct the call. Failures must log the exception or error code, not swallow it.
   - **A metric or counter.** At minimum: one success counter and one failure counter per critical operation. Latency histograms for external calls. If no metrics library is configured (Prometheus client, statsd, OpenTelemetry), skip this unless the project already has one — don't introduce a new observability stack.
   - **An alert or error report.** Errors on critical paths must reach an on-call channel: Sentry, Rollbar, Datadog error tracking, a PagerDuty integration, or at minimum a logger that the team actually reads. Silent `catch` blocks on payment or auth paths are failures.

3. **Check correlation.** A single user action (e.g. "submit order") may touch 3–4 services. Logs from those services should share a request/trace ID so they can be joined. If the project has a tracing library (OpenTelemetry, X-Ray, Datadog APM), verify critical paths propagate the trace context across service boundaries. If no tracing is configured, generate-and-propagate a request ID header at the API entry point — a poor-man's trace.

4. **Check for PII in logs.** While reviewing log statements on critical paths, flag any that log full email addresses, tokens, passwords, PII, payment card numbers, or auth headers. Mask/truncate them (`u***@example.com`, `sk_***1234`). This overlaps with the `secret-leakage` check but fix obvious cases here too.

5. **Check background jobs.** Queue consumers and scheduled tasks silently disappearing is a classic production failure. Verify each job logs start and end, records duration, and alerts on repeated failure. If the queue library supports dead-letter queues or retry-exhausted hooks, ensure they are wired to the alerting path.

**What not to do:**
- Do NOT add logging/metrics to non-critical paths. Request-scoped getters, internal helpers, pure functions do not need instrumentation.
- Do NOT add a new observability dependency. Use what's already installed. If the project uses `winston`/`structlog`/`zerolog`, add to that. If none exists, use stdlib logging with JSON formatting.
- Do NOT log request/response bodies wholesale on auth or payment paths — that's how credentials end up in ElasticSearch.
- Do NOT add metrics without a way to read them. If there's no Prometheus scrape or statsd receiver, you're adding code that does nothing.

Report critical paths found, which were missing which layer of observability, and what you added. Run the test suite after changes.
