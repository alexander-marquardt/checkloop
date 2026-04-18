---
id: schema-validation
label: "Schema Validation at Boundaries"
---

Every byte crossing into the system from the outside — HTTP request bodies, query/path params, webhook payloads, message queue bodies, file uploads, external API responses — must be parsed and validated against a schema before being used. This is not "does a function named `validate*` exist"; it's "can an untyped blob of JSON reach business logic".

1. **Enumerate boundaries.** Find every place the application ingests external data:
   - HTTP endpoint handlers (Express routes, FastAPI path operations, Django views, Rails controllers, Go `http.HandleFunc`, etc.)
   - Webhook receivers (Stripe, GitHub, Slack, custom)
   - Message/queue consumers (SQS, Kafka, Redis streams, RabbitMQ)
   - File/upload processors
   - Responses from external APIs that the code deserializes and uses
   - Environment variable and config-file parsing at startup

2. **For each boundary, check for a schema.** Acceptable shapes:
   - **TypeScript/JavaScript:** Zod, Yup, io-ts, Joi, ArkType, Valibot. `req.body as MyType` is NOT validation — it's a type assertion.
   - **Python:** Pydantic models, `marshmallow`, `dataclass` + `cattrs`, FastAPI path-op types (which use Pydantic under the hood).
   - **Go:** `encoding/json` + explicit struct tags + a validator library (`go-playground/validator` or similar); `json.Unmarshal` alone doesn't validate, only shape-matches.
   - **Rust:** `serde` + explicit type with validation, or `validator` crate.

3. **Check failure modes.** A validator that throws a generic 500 on bad input is almost as bad as no validator. Each boundary should:
   - Return a structured 4xx with a useful error message (field path + reason)
   - Log the validation failure with enough context to debug (without logging the raw payload if it may contain secrets)
   - Not leak implementation details (don't return the raw Pydantic traceback to an external caller)

4. **Check external API responses.** When the code deserializes a response from Stripe/GitHub/etc. and uses fields from it, it should tolerate missing fields and schema drift. A `response.json()["amount"]` that crashes when the external API adds/removes fields is brittle. Parse external responses through a schema that accepts "extra" fields but fails loudly on missing required ones.

5. **Check env/config parsing.** App startup is a boundary too. Config should be parsed through a schema (Pydantic Settings, Zod `.parse(process.env)`, `envconfig` in Go) so missing/malformed env vars fail at boot, not at first use.

6. **Check webhook signatures.** Webhook receivers must verify the signature header before parsing the body. If a Stripe/GitHub/Slack webhook handler reads the body without a signature check, that's a high-severity gap — flag and fix.

**What to fix:**
- Add schema validation at every boundary that lacks it. Prefer the library already used in the project.
- Route validation failures to the structured 4xx path, not a generic 500.
- Do NOT over-validate trusted internal-only boundaries (service-to-service calls within a private VPC, for instance) if the project explicitly trusts them — flag, don't fix.
- Do NOT invent a new validation library when the project already has one in use elsewhere.

Safety-sensitive symbols (`isSafe*|validate*|sanitize*|escape*|auth*|permission*|encrypt*|decrypt*|verify*`) on boundary paths get extra scrutiny — they must have tests covering both the happy path and a representative set of invalid inputs.

Run the test suite after adding validators. Report boundaries found, which were unvalidated, and what was added.
