---
id: fixture-drift
label: "Fixture & Mock Drift"
---

Tests often mock functions, classes, or HTTP responses that the real code has since changed. The tests still pass — because the mock passes — but they no longer verify anything about the real behaviour. This check finds and fixes that drift.

1. **Enumerate mocks.** Find every test that patches, mocks, or stubs something:
   - Python: `unittest.mock.patch`, `mocker.patch` (pytest-mock), `monkeypatch.setattr`, `mock.Mock(spec=...)`
   - JavaScript/TypeScript: `jest.mock`, `jest.spyOn`, `vi.mock`, `vi.spyOn`, `sinon.stub`, MSW handlers, `nock` interceptors, manual `__mocks__/` directories
   - Go: interface-based test doubles, `httptest.NewServer` recordings
   - Fixture files: `fixtures/`, `__fixtures__/`, JSON responses checked in for replay
   - Search/index DSL fixtures: Elasticsearch / OpenSearch query bodies, aggregation specs, index mappings, and stored response samples — these reference field paths (`user.profile.email`, `events.timestamp`, nested `properties` keys) that drift when the index mapping changes

2. **For each mock, verify the mocked target still exists:**
   - `patch("module.Class.method")` — does `method` still exist on `Class`? Did it get renamed?
   - `jest.mock("../api")` with `mockReturnValue({...})` — does the shape still match the real `api` export?
   - Fixture file `fixtures/stripe_charge.json` — does the field set still match what the real Stripe library returns for the current API version?
   - HTTP recording at `https://api.example.com/v1/users` — does that URL still exist, and is the response shape current?
   - Elasticsearch / OpenSearch fixtures — does every field path referenced in a `query`, `term`, `match`, `range`, `bool`, `aggs`, `sort`, `script`, or `_source` clause still exist in the current index mapping? Field renames (e.g. `user_email` → `user.email`), type changes (`keyword` → `text`), and nested-vs-flattened migrations break ES fixtures silently because the query still parses — it just returns zero hits. Cross-reference field paths against the project's mapping files (`mappings/*.json`, index-template definitions, or the live mapping if a snapshot is checked in) and any field-name constants in the source. Pay particular attention to multi-field names (`title.raw`, `title.english`), nested-path prefixes (`comments.author.id`), and runtime fields whose definitions live on the index, not in the query.

3. **Find silently passing mocks:** the worst kind of drift. A test like:
   ```python
   with patch("app.fetch_user") as fake:
       fake.return_value = {"id": 1, "name": "Alice"}
       result = get_profile(1)
       assert result.username == "Alice"
   ```
   If `fetch_user` now returns `{"user_id": ..., "display_name": ...}`, the real callers of `get_profile` break, but this test still passes because `fake` returns whatever it was told to. Fix: use `spec=fetch_user` / `autospec=True` / `jest.MockedClass` / strict TypeScript on the mock's shape, so mock signatures match real ones.

   The same failure mode hits Elasticsearch / OpenSearch tests harder than most: a query against a field that no longer exists in the mapping does not raise — it parses, runs, and returns an empty result set, which a test asserting `len(hits) == 0` will happily accept. When the mocked or fixture-backed ES client is involved, the query is never executed against a real mapping at all and the drift is invisible. Pin field paths against the current mapping (or a checked-in mapping snapshot) and add at least one assertion that fails when the field path is wrong — e.g. assert the response is non-empty for a fixture document you also indexed, or assert against the parsed query body that the field path equals an expected value sourced from a shared constants module.

4. **Check mock depth.** Tests that mock four layers deep (`patch("a.b.c.d")`) are brittle to any rename anywhere in the chain. Prefer mocking the outermost seam (the HTTP client or the public function under test's direct dependency), not an internal helper.

5. **Check fixture freshness.** If a fixture is a recording of an external API response:
   - Date-stamp the recording in a comment or sibling file.
   - If the recording is >12 months old and the external API has a changelog, check whether the response shape has changed.
   - Prefer VCR-style recordings that can be regenerated (`vcrpy`, `nock.back`, MSW + snapshots) over hand-maintained JSON files.

6. **Check mock assertions.** A mock that is set up but never asserted against is only a stub. `expect(mock).toHaveBeenCalledWith(...)` / `mock_fn.assert_called_with(...)` / `sinon.assert.calledWith(...)` — if the mock's only role is to return a value, fine; if it's verifying an interaction, the assertion must exist.

7. **Check mock leakage.** A mock set up in test A that leaks into test B (because `autouse` fixtures or module-level `jest.mock` or global patch). This creates order-dependent flakes. Ensure every mock has a matching teardown/unpatch, either via context manager, pytest fixture with `yield`, `afterEach`, or `sinon.restore`.

**What to fix:**
- Update mock signatures to match current real code (add `spec=`/`autospec=True`/types).
- Regenerate stale external-API recordings and note the regeneration date.
- Replace deep-chain mocks with mocks at the outermost reasonable seam.
- Add missing interaction assertions where the mock's purpose was to verify a call.
- Fix leaking mocks with explicit teardown.
- Update Elasticsearch / OpenSearch query fixtures whose field paths no longer exist in the current mapping; where the project keeps field-name constants in source, switch the test to reference those constants so a future rename trips the test instead of silently returning zero hits.

**What not to do:**
- Do NOT rewrite tests wholesale — fix the specific drift.
- Do NOT replace mocks with real calls to external services (the test suite should stay self-contained).
- Do NOT use `# type: ignore` / `@ts-ignore` to silence mock type mismatches — that's what caused the drift in the first place.

Run the test suite after changes — including with `-p no:randomly` / `--runInBand` to catch order-dependent flakes. Report: mocks audited, drift found, and what was fixed.
