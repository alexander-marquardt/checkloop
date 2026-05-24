---
id: rationale
label: "Document the Why, Not Just the How"
---

For every non-trivial module, function, class, configuration knob, complex code block, and invariant-pinning test, ensure the rationale for *why* it exists is recoverable from the code itself — the threat model, business constraint, past incident, contract requirement, or design trade-off that justifies the code being shaped the way it is. Implementation details (the *how*) are recoverable by reading the code; the rationale (the *why*) is not, and is the part anyone modifying the code or integrating it for a downstream customer must understand before changing anything.

Place each explanation wherever fits best for the reader who will look for it: module / class / function docstring, code comment, configuration doc, or architecture doc. The goal is that an integrator or a new maintainer can answer "why is this here?" from the artefact at hand, without reverse-engineering it from git history every time.

## Scope — what needs a "why"

1. **Modules.** Every non-trivial module should state, in its module docstring, the role it plays in the wider system and the constraint that drove its shape. A reader landing on the file from a grep should learn *what threat / contract / requirement it serves*, not just a name-restating one-liner. Pure utility modules with self-evident contents (`constants.py`, `types.py`, simple re-exports) are exempt unless their composition is itself non-obvious.

2. **Configuration knobs.** Every env var, settings field, CLI flag, feature toggle, and YAML / TOML / JSON config key. Capture the operational situation that motivates the knob, the failure mode it addresses (or causes when set wrong), and the default trade-off. "What it does" is obvious from the variable name; "when an operator should change it and why" is the part that matters. The right home is usually either a docstring on the settings field / CLI argument, or a section in an existing configuration doc — pick whichever the project already uses and stay consistent.

3. **Non-obvious code patterns.** Any code where the structure is doing something the reader could not reconstruct from the function name alone: a workaround for a specific bug, a counterintuitive ordering that satisfies an invariant, a defensive check that protects against a real but non-obvious failure mode, a performance threshold tuned to a measured cost, or a contract requirement imposed by an external consumer. Add a brief comment (one or two lines is usually enough) naming the constraint — past incident reference, contract requirement, performance threshold, RFC clause, etc.

4. **Invariant-pinning tests.** Tests that pin a contract (cross-layer parity, API stability, schema-migration safety, security boundary, etc.) should carry a docstring naming the contract they enforce. "Asserts X equals Y" decays into noise when the requirement shifts; "pins the contract requirement that X equals Y so external consumers can reconstruct …" stays grep-able and load-bearing.

5. **Public APIs and exports.** Every public function / class on the package's stable surface should carry a brief docstring stating the use case it serves and any non-obvious calling constraints. The audience is integrators who may never read the implementation — they need the *why* of the surface, not just its signature.

## Process

For each gap you find:

1. **Investigate before writing.** When the rationale is not visible from the current file, search the file's history: `git log --follow -- <path>`, `git blame <path>`, and the PR / issue references in commit messages. The reason this code is shaped the way it is is usually in the commit that introduced it — recover that reason rather than guessing. Also check the project's `CLAUDE.md` / `AGENTS.md` / `CONTRIBUTING.md` / `docs/` / `ARCHITECTURE.md`, which often carry the larger-scale rationale that a specific module is locally satisfying.

2. **Capture what you found — terse and load-bearing.** One paragraph of *why* beats five paragraphs of *how*. Examples of the shape to aim for:

   - GOOD module docstring: "Token-bucket rate limiter, in-process. Defends `/search` against bursts from a single misbehaving SI client when running behind a shared L7 LB that won't enforce per-tenant limits. Configured per-tenant via `RATELIMIT_*` env vars; a tenant exceeding their bucket gets a 429 with `Retry-After`."

   - GOOD config-knob docstring: "`ENGINE_URL` — URL of a remote engine service. Unset (default) → run the engine in-process via ASGI transport, suitable for single-host demo deployments. Set → call the engine over TCP, used by SI deployments where the engine is scaled independently. A URL the process cannot reach causes startup to fail loudly at the health probe, not at first request."

   - GOOD code comment: "Wait 500 ms before retrying — the upstream LB queues for ~300 ms during failover; retrying sooner duplicates the request without giving the LB time to land on the survivor."

   - GOOD test docstring: "Pins the SI contract requirement that `RewriteSummary.synonyms[]` is sufficient to reconstruct the peer-bool expansion; an integrator must be able to rebuild the executed ES DSL from this field alone."

3. **When the rationale cannot be recovered, do NOT fabricate one.** Leave a `TODO(rationale): <what you tried, what you could not find>` comment at the relevant site and list the file in your report. A truthful gap is far better than a plausible-sounding lie that future readers will trust.

4. **When the rationale already exists elsewhere, prefer linking to it over duplicating.** A module docstring that says `See docs/14-deployment-scenarios.md § "ASGI in-process mode" for the why behind this transport choice.` is better than restating the same content in two places where they can drift. Duplication is invitation to inconsistency.

## Anti-patterns — do not produce these

- **Comments that restate the code.** `# increment counter` next to `counter += 1` is noise. This check is about rationale, not narration. If `cleanup-ai-slop` would delete the comment, do not add it in the first place.
- **Module docstrings that just restate the module name.** "Module for handling users." adds nothing over `users.py` as a filename.
- **Inflated docstrings.** One terse paragraph naming the constraint and the trade-off — not a five-paragraph essay or a bulleted recap of every function in the file.
- **Speculative or generic rationale.** "For maintainability" / "for performance" / "for testability" / "for flexibility" are not rationales — they are labels. Name the specific maintenance pain, the measured performance threshold, or the concrete test the structure unblocks.
- **Universal backfill.** This check is not "comment every file in the repo". Focus on files that genuinely need a why (the five categories above). If the file's name plus signatures already make its role obvious, leave it alone.
- **AI-attribution leakage.** Do not write "Generated by …", "AI-assisted", `Co-Authored-By: <AI tool>`, or any equivalent attribution into the docstrings, comments, or doc files you add. The work product stands on its own. If the target project's `CLAUDE.md` / `AGENTS.md` / `CONTRIBUTING.md` documents a no-AI-mentions rule, this is mandatory; otherwise it remains the default.

## Scope discipline

- **Documentation-only check.** Do NOT modify code behaviour. Do not rename, do not refactor, do not adjust signatures — capture the why for the code as it stands. Behaviour changes are the job of the behaviour-modifying checks that have already run.
- **Prefer the surface the reader will look at first.** A non-obvious code pattern gets a code comment; a config knob gets either a docstring on the settings field or a section in the configuration doc, whichever the project already uses; an invariant-pinning test gets a docstring on the test function; a module's overall role goes in the module docstring.
- **Do not duplicate the work of `docs` or `docs-accuracy`.** This check is about *rationale*; those are about README/API-doc completeness and factual accuracy respectively. If you find a factual inaccuracy in an existing doc or comment while looking for missing rationale, note it in your report and leave it for `docs-accuracy`.
- **Run after the behaviour-modifying checks.** The rationale you capture should describe the code as it is at the end of the run, not as it was at the start. Plans place this check late for that reason.

## Reporting

Report what you added by category — modules, config knobs, code patterns, invariant tests, public APIs — and list every `TODO(rationale)` gap you flagged but could not fill. The TODOs are the most important part of the report: they are the items that need a maintainer's domain knowledge before the next reader hits the file.
