---
id: derived-values
label: "Frontend/Backend Derived Value Consistency"
---

Find places where the frontend re-derives, recalculates, or re-implements values that the backend already computes and sends in API responses. Derived values should be computed once (backend) and consumed (frontend), not independently re-derived on both sides.

Look for these patterns:

1. **Re-derived calculations** — The backend computes a value (total price, discount amount, tax, score, percentage, count, average) and includes it in the API response. The frontend ignores the response field and recalculates the same value from raw data using its own logic. Fix: use the backend's computed value from the response.

2. **Re-implemented business rules** — The backend determines a status, permission, eligibility, or validation result. The frontend re-implements the same logic (checking the same conditions, applying the same rules) instead of reading the backend's determination from the API response. Fix: consume the backend's decision.

3. **Re-derived display values** — The backend formats a label, description, summary string, date display, or human-readable representation. The frontend ignores it and formats its own version from raw fields. Fix: use the backend's formatted value, or if the frontend needs different formatting, document why.

4. **Re-derived state** — The backend returns an object with a computed state (is_active, is_expired, is_valid, can_edit). The frontend ignores these fields and re-derives the state by checking timestamps, flags, or other raw fields independently. Fix: use the backend's state fields.

5. **Duplicated enums/constants** — The frontend defines its own copy of enums, status codes, category lists, or configuration constants that the backend already defines and could serve via an API endpoint or shared schema. Fix: consume from the API or a shared definition.

6. **Inconsistent validation** — The frontend validates input using different rules than the backend (different max lengths, different regex patterns, different required fields). The frontend validation should be a subset of or match the backend validation, not contradict it. Fix: align the rules, or have the frontend fetch validation constraints from the backend.

For each issue found:
- Identify the backend computation and where it appears in the API response
- Identify the frontend re-derivation and how it differs (or could drift)
- Fix the frontend to consume the backend value instead of re-deriving it
- If the backend doesn't currently include the value in the response but should, add it to the backend response and consume it on the frontend

Do NOT flag cases where the frontend intentionally computes something different from the backend (e.g. optimistic UI updates, client-side filtering for UX responsiveness, or display-only transformations that are purely cosmetic). Only flag cases where the frontend is trying to arrive at the SAME value the backend already computed.

If the project has no frontend, or the frontend and backend are in separate repositories, report that and skip.