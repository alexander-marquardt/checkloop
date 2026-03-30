---
id: concurrency
label: "Concurrency & Thread Safety"
---

Review for concurrency issues. Look for: race conditions, shared mutable state without synchronisation, deadlock potential, missing locks around critical sections, non-atomic read-modify-write sequences, and unsafe use of globals. Check async code for missing awaits, unawaited coroutines, and blocking calls in async contexts. Fix any issues you find.