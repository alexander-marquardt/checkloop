---
id: deps
label: "Dependency Hygiene"
---

Audit the project's dependencies for issues. Identify unused dependencies and remove them, but ONLY if they are truly unused — verify that no source file imports the package before removing it. Also verify the package is not used as a CLI tool, plugin, or runtime server. Do NOT remove a dependency if any code still imports or references it. Check for outdated packages with known vulnerabilities. Flag dependencies that are unmaintained or have better alternatives. Ensure lock files are consistent with declared dependencies. Check that dependency version constraints are neither too loose nor too tight.