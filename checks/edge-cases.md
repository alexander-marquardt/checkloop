---
id: edge-cases
label: "Edge Cases & Boundary Conditions"
---

Look for unhandled edge cases and boundary conditions: off-by-one errors, empty/null/undefined inputs, integer overflow, empty collections, zero-length strings, negative numbers where unsigned expected, concurrent modification, and Unicode/encoding edge cases. Only fix edge cases that can realistically occur in production usage. Do NOT add defensive handling for inputs that the type system already prevents (e.g. null checks where the type is non-nullable, bounds checks on validated input). Fix any issues and add tests for the edge cases you find.