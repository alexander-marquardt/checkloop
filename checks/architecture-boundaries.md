---
id: architecture-boundaries
label: "Architecture Layer Separation"
---

Identify the architectural layers in this project and enforce clean boundaries between them. Then fix any violations you find.

## Step 1: Discover the layers

Examine the directory structure, package names, import graph, and any documentation to determine the project's architectural layers. Common patterns include:

- **Frontend / Backend** — a UI layer that consumes a backend API
- **Standalone library / Application layer** — a core library or engine that should work independently, plus an application (demo app, CLI, web app) that wires it together and drives the frontend
- **API / Service / Data** — layered backend architectures where lower layers should not depend on higher ones

Not every project has multiple layers. If the project is a single-layer library, CLI tool, or script with no meaningful layer separation to enforce, report that and skip.

Write down the layers you identified, their intended dependency direction (which layers may depend on which), and the directories/packages that belong to each layer.

## Step 2: Find boundary violations

Check for these violation types:

1. **Upward imports** — A lower layer imports from a higher layer. For example, a standalone query-rewriter library importing from the demo app that wraps it, or a backend module importing frontend code. The dependency direction should always flow downward: application/frontend → API/backend → core library.

2. **Leaking internals** — A higher layer reaches into the internal modules of a lower layer instead of using its public API surface. For example, the frontend importing a backend helper function directly instead of calling the API, or an application layer importing a private module from the library.

3. **Shared state coupling** — Layers sharing global state, singletons, or module-level mutable variables instead of passing data through explicit interfaces (function arguments, API calls, dependency injection).

4. **Mixed-layer modules** — A single file containing code that belongs to different layers. For example, a file that defines both API route handlers and standalone business logic, or a file that mixes frontend rendering code with backend data processing.

5. **Circular dependencies between layers** — Layer A imports from layer B, and layer B imports from layer A, creating a cycle that makes it impossible to use either layer independently.

## Step 3: Fix the violations

For each violation, apply the appropriate fix:

- **Upward imports**: Move the shared code down to the lowest layer that needs it, or extract it into a shared utilities layer that both layers depend on. Update all import paths.

- **Leaking internals**: Replace the internal import with a call through the layer's public API. If no suitable public API exists, add one (a function, class, or API endpoint) and route through it.

- **Shared state coupling**: Replace the shared mutable state with explicit parameter passing, dependency injection, or configuration objects. Each layer should receive its dependencies, not reach out and grab them.

- **Mixed-layer modules**: Split the file into separate modules, one per layer. Move each piece to the directory/package where it belongs. Update all imports.

- **Circular dependencies**: Break the cycle by extracting the shared dependency into a lower layer, or by introducing an interface/protocol that allows the lower layer to remain independent.

## Constraints

- Do NOT restructure code where the current organization is clean and intentional. Only fix actual boundary violations.
- Do NOT create new layers or abstractions that don't already exist in the project's architecture. Work within the existing structure.
- Do NOT break functionality. After moving code, ensure all imports are updated and the code still works.
- Do NOT flag trivial cases — a small utility function used by two layers is fine if it lives in the lower layer or a shared module.
- Prefer the smallest move that fixes the violation. Don't reorganize the entire project when moving one function suffices.

If the project has no meaningful layer separation to enforce (single-layer project, flat script, etc.), report that and skip.
