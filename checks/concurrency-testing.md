---
id: concurrency-testing
label: "Concurrency Test Coverage"
---

First, determine whether this project serves multiple concurrent users. Multi-user projects include: web applications, REST/GraphQL APIs, e-commerce platforms, SaaS products, booking/reservation systems, collaborative tools, payment processors, marketplaces, and any system where two or more users can read/write shared state simultaneously. Single-user projects include: CLI utilities, desktop apps designed for one person, static site generators, build tools, personal scripts, and libraries that don't manage shared state. If the project is clearly single-user, report that concurrency testing is not applicable and stop — do not force tests where they don't belong.

For multi-user projects, audit the test suite for tests that simulate concurrent access — multiple users or requests hitting shared state at the same time. These are distinct from unit tests that run sequentially and from thread-safety checks on internal code. What you're looking for are tests that prove the system behaves correctly when, for example, two users try to buy the last item in stock, two requests update the same record, or multiple sessions write to the same resource simultaneously.

Specifically look for:
- Tests that use threading, multiprocessing, asyncio.gather, or a load-testing library (locust, k6, artillery, wrk, ab, pytest-asyncio with concurrent tasks) to send simultaneous requests or operations against shared resources.
- Tests that verify atomicity of critical operations: inventory decrement, balance transfers, seat/slot reservations, counter increments, order placement.
- Tests that verify database-level protections: optimistic locking (version columns, ETags), pessimistic locking (SELECT FOR UPDATE), unique constraints under race, and transaction isolation holding under concurrent writes.
- Tests that confirm idempotency of critical endpoints — replaying the same request doesn't double-charge, double-book, or double-create.

If these tests are missing or clearly insufficient for the project's domain, write them. Use the project's existing test framework and database/service setup. The tests should:
- Spawn a realistic number of concurrent workers (10-50 threads or async tasks is usually enough to surface races — don't go to thousands, this is a correctness test, not a load test).
- Target the actual code paths that handle shared mutable state (database writes, cache updates, queue operations), not just read-only endpoints.
- Assert on correctness invariants: final inventory count matches (initial - number of successful purchases), account balances sum to zero, no duplicate bookings for the same slot.
- Be deterministic and CI-friendly — no external service dependencies that aren't already in the test setup, no hardcoded ports, skip gracefully if required services aren't available.
- Be clearly named and documented so their purpose is obvious (e.g., test_concurrent_checkout_does_not_oversell, test_parallel_balance_transfers_preserve_total).

Do NOT write concurrency tests for read-only endpoints, static pages, or operations that don't touch shared mutable state. Do NOT add load-testing infrastructure (locust configs, k6 scripts) unless the project already uses them — the goal here is correctness under concurrency, not performance benchmarking. Do NOT refactor application code in this check — only add or improve tests. If you find a race condition while writing these tests, add a failing test that demonstrates it and leave a clear TODO comment explaining the bug, but do not fix application code.

Run the test suite after adding tests and fix any test-level issues (imports, fixtures, setup). If a new concurrency test fails because it found a real race condition, that is expected — leave it failing with a clear assertion message.
