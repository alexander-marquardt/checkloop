---
id: concurrency
label: "Concurrency & Thread Safety"
---

Review for concurrency issues. Look for: race conditions, shared mutable state without synchronisation, deadlock potential, missing locks around critical sections, non-atomic read-modify-write sequences, and unsafe use of globals. Check async code for missing awaits, unawaited coroutines, and blocking calls in async contexts. Fix any issues you find.

When you fix a concurrency issue, ship an **isolating** test that pins the concurrency-specific behavior the fix introduced — that the blocking call now runs off the event loop (e.g. assert the loop is not blocked, or that the work ran in a worker thread), that the coroutine is actually awaited, that the critical section stays correct under contention (spawn concurrent workers and assert the invariant holds). A pre-existing parity or value-equality test does NOT discharge this: those pass with or without the fix, so they prove nothing about the concurrency behavior — the isolating test should fail against the pre-fix code. If an isolating test genuinely cannot be written in this stack, do not skip silently — add the explicit `no test added: <named missing piece>` line naming the specific harness or fixture that does not exist.