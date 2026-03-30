---
id: dry
label: "DRY / Eliminate Repetition"
---

Find repeated or near-repeated logic. Extract shared helpers, base classes, or utility modules to eliminate duplication. Consolidate config values or magic numbers into constants. Where a module mixes multiple concerns (e.g. data models, API serialization, and validation in one file), consider extracting each concern into a focused module — but only when the separation makes each piece independently testable or reusable. Ensure each concept has a single canonical home in the code. Do NOT extract helpers for code that is only duplicated 2-3 lines or used in only 2 places — three similar lines is better than a premature abstraction. Do NOT change observable behaviour — only reduce repetition.