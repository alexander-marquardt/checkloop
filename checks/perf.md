---
id: perf
label: "Performance"
---

Review for obvious performance issues: N+1 queries, O(N²) algorithms that could be O(N) or O(N log N), missing indexes, unnecessary re-renders, blocking I/O that could be async, large allocations in loops. Add caching (@cache, @lru_cache, memoization) for expensive computations that are called repeatedly with the same inputs — especially compiled regexes, schema introspection, and config loading. Only cache where the inputs are stable and the cache won't grow unbounded. Fix anything significant. Add a brief comment only if the optimisation would be surprising to a reader — do not comment obvious improvements.