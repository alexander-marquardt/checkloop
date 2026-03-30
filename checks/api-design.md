---
id: api-design
label: "API Design & Consistency"
---

Review public APIs (REST endpoints, library interfaces, CLI commands, exported functions) for consistency and usability. Check for: consistent naming conventions, predictable parameter ordering, appropriate HTTP methods and status codes, consistent error response formats, proper use of pagination, versioning where needed, and idempotency of mutating operations. Do NOT rename endpoints, change HTTP methods, or alter response shapes — these are breaking changes. Focus on parameter validation and error response consistency. Fix inconsistencies and document any breaking changes.