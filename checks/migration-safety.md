---
id: migration-safety
label: "Database Migration Safety"
---

Review database migrations (and any other schema/state mutations that run on deploy) for safety under production conditions: concurrent writes, large tables, partial failure, and rollback. Skip this check entirely if the project has no migrations directory.

1. **Locate migrations.** Common paths: `migrations/`, `db/migrate/`, `alembic/versions/`, `prisma/migrations/`, `supabase/migrations/`, `knex/migrations/`, `flyway/`, `db/schema.sql`. Also consider one-off "data fix" scripts in `scripts/` that mutate production state.

2. **For each migration, check these failure modes:**

   **Locking:** A `NOT NULL` column added to a large table without a default locks the table for the full scan. Postgres ≥ 11 handles `ADD COLUMN ... DEFAULT` without a rewrite for constant defaults, but earlier versions or volatile defaults (e.g. `now()`) still lock. Same for changing column types and adding `UNIQUE`/`CHECK` constraints. Flag migrations that could block a busy table for more than a few seconds.

   **Index creation:** Creating an index on a large table with `CREATE INDEX` (no `CONCURRENTLY`) blocks writes. Postgres offers `CREATE INDEX CONCURRENTLY` — prefer it for any table expected to have significant row counts in production. Same rule applies to `DROP INDEX CONCURRENTLY`.

   **Destructive changes:** `DROP COLUMN`, `DROP TABLE`, renames, and type narrowings are destructive. Best practice is a multi-step rollout:
   - Step 1: Add new column / table (old still in use).
   - Step 2: Backfill data and dual-write from application code.
   - Step 3: Cut over reads.
   - Step 4: In a *separate* release, drop the old column/table.

   A single migration that does all four in one step can't be rolled back if the deploy fails mid-step. Flag these for splitting.

   **Backfills:** A migration that runs `UPDATE large_table SET ...` in a single statement can lock the table, fill the WAL, or time out. Backfills over >10k rows should be chunked (batched loops with explicit transaction commits, or a separate backfill job). Flag and rewrite.

   **Non-idempotency:** A migration that would fail or do the wrong thing if re-run (because, say, a partial earlier run left the table half-migrated) is dangerous. Check that the migration either uses `IF NOT EXISTS` / `IF EXISTS` guards, or that the framework provides idempotency itself (Alembic, Flyway, Prisma all track applied migrations — so this is usually fine within the framework, but *application-code migrations* often aren't).

   **Rollback:** Does a `down` / `reverse` exist? If the migration is one-way (data loss), the `down` should be explicit about that, not silently absent. Prefer expand-and-contract over irreversible changes.

   **Transaction boundaries:** Some migrations must run outside a transaction (`CREATE INDEX CONCURRENTLY`, `ALTER TYPE ... ADD VALUE` in older Postgres). Most frameworks wrap every migration in a transaction by default — flag migrations that need an explicit "no transaction" directive.

3. **Check for foreign-key deadlock risk.** Adding a foreign key with `VALIDATE` on a large table locks both sides. Two-step it: `ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT` in a separate migration.

4. **Check startup migrations.** Some apps run pending migrations automatically on boot. This is convenient in dev but dangerous in production (one slow migration stalls deploys; concurrent replicas race to migrate). Prefer migrations run as a separate step in the deploy pipeline. Flag if the app migrates at boot without a lock.

5. **Check data scripts.** One-off scripts in `scripts/` that mutate production data should be checked into the repo (not run from laptops), log what they did, be idempotent, and ideally run as a migration under the same framework as the schema changes.

**What to fix:**
- Split multi-step destructive changes into separate migrations.
- Add `CONCURRENTLY` where the project's database supports it.
- Chunk large backfills.
- Add explicit `down` migrations where missing.
- Flag high-risk migrations clearly in the report, even if you can't safely rewrite them without more context — this is the kind of check where some findings require human review before action.

**What not to do:**
- Do NOT rewrite a migration that has already been applied to any environment — that creates drift between environments. If a bad migration was already applied, the fix is a *new* follow-up migration, not editing the old one.
- Do NOT reorder migrations — the ordering is part of the history.

Run the test suite after changes. Report: migrations reviewed, high-risk findings (with file:line), and what you split/rewrote.
