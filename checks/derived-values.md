---
id: derived-values
label: "Frontend/Backend Derived Value Consistency"
---

Find places where the frontend re-derives, recalculates, or re-implements values that the backend already computes. Derived values should have a single authoritative source of truth — computed once on the backend and consumed by the frontend. If the backend computes a value but does not yet include it in an API response the frontend already uses, the fix is to add it to that existing response — not to create new API calls, and not to independently recompute it on the frontend.

Look for these patterns:

1. **Re-derived calculations** — The backend computes a value (total price, discount amount, tax, score, percentage, count, average). The frontend recalculates the same value from raw data using its own logic instead of consuming the backend's result. Fix: if the backend already includes it in the API response, use it; if not, expose it via the API and consume it.

2. **Re-implemented business rules** — The backend determines a status, permission, eligibility, or validation result. The frontend re-implements the same logic (checking the same conditions, applying the same rules) instead of consuming the backend's determination. Fix: expose the backend's decision via the API if not already present, and consume it.

3. **Re-derived display values** — The backend formats a label, description, summary string, date display, or human-readable representation. The frontend formats its own version from raw fields instead of using the backend's. Fix: use the backend's formatted value, or if the frontend needs different formatting, document why.

4. **Re-derived state** — The backend computes state (is_active, is_expired, is_valid, can_edit). The frontend re-derives the state by checking timestamps, flags, or other raw fields independently instead of consuming the backend's state. Fix: if the backend includes these fields in the API response, use them; if not, expose them and consume them.

5. **Duplicated enums/constants** — The frontend defines its own copy of enums, status codes, category lists, or configuration constants that the backend already defines and could serve via an API endpoint or shared schema. Fix: consume from the API or a shared definition.

6. **Inconsistent validation** — The frontend validates input using different rules than the backend (different max lengths, different regex patterns, different required fields). The frontend validation should be a subset of or match the backend validation, not contradict it. Fix: align the rules, or have the frontend fetch validation constraints from the backend.

For each issue found:
- Identify the backend computation and where it appears (or should appear) in an existing API response
- Identify the frontend re-derivation and how it differs (or could drift)
- Fix the frontend to consume the backend value instead of re-deriving it
- If the backend computes the value but doesn't currently include it in a response the frontend already fetches, add it to that existing response — do NOT introduce new API calls just to avoid frontend re-computation

Do NOT flag cases where the frontend intentionally computes something different from the backend (e.g. optimistic UI updates, client-side filtering for UX responsiveness, or display-only transformations that are purely cosmetic). Only flag cases where the frontend is trying to arrive at the SAME value the backend already computes.

Also do NOT flag trivially deterministic computations where divergence is essentially impossible — for example, `items.length`, `firstName + " " + lastName`, or `list.length > 0`. The litmus test: could a reasonable change to business logic cause the frontend and backend versions to produce different results? If no, independent computation on both sides is acceptable.

If the project has no frontend, or the frontend and backend are in separate repositories, report that and skip.