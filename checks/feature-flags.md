---
id: feature-flags
label: "Feature Flag Hygiene"
---

Feature flags accumulate. The flag that gated "new checkout" a year ago is still in the code long after the feature shipped, and nobody is sure whether the "off" branch still works. This check finds stale flags, conflicting gates, and mismatches between the code and the flag service.

Skip this check entirely if the project has no flag library or config. Look for one of: LaunchDarkly, Unleash, Flagsmith, Split, ConfigCat, Statsig, PostHog, Vercel Flags, a homegrown `flags.json` / `features.yaml`, or env-var feature flags.

1. **Enumerate flag references in code.** Find every site that checks a flag. Typical patterns:
   - `flags.<name>`, `useFlag('<name>')`, `client.variation('<name>', ...)`, `client.isEnabled('<name>')`
   - Env-var flags: `if (process.env.ENABLE_X === 'true')`, `if os.getenv("FEATURE_Y")`
   - Build-time flags: `#ifdef`, Webpack `DefinePlugin` substitutions

2. **Enumerate flag definitions.** Where the flag is declared — the flag service dashboard (unreachable from this check, so skip), a config file, a schema, or documentation. In most setups this is a `flags.ts` / `features.yaml` / `feature_flags.py` file.

3. **Cross-reference and flag the gaps:**
   - **Ghost flags:** referenced in code but no definition anywhere. Either the flag was deleted in the service but not in the code (dead code path), or there's a typo. Fix: remove the reference and the code path, OR fix the typo.
   - **Orphan flags:** defined but not referenced in code. Fix: remove the definition. If the flag might be used via dynamic key construction (`flags[\`experiment_\${id}\`]`), verify via grep of the prefix before removing.
   - **Always-on / always-off flags:** the flag exists but every caller has concluded the same branch. If the rollout is 100% and has been for >30 days (check git blame on the flag creation), remove the flag and the dormant branch.
   - **Conflicting flags:** two flags that gate the same code path with different names, or nested flag checks where the outer guarantees the inner. Collapse to one.
   - **Flag + env-var drift:** the code checks `flags.newCheckout` but the env-var override `ENABLE_NEW_CHECKOUT` still exists and takes precedence. Pick one.

4. **Check flag usage at the right layer.** A flag checked deep inside a library function means the library can't be reasoned about without knowing the flag's value. Prefer flags at the composition root (entry points, router, feature boundaries). Flag this pattern but don't aggressively refactor unless the fix is small.

5. **Check flag cleanup when removing features.** If a flag has been rolled out 100% for a long time AND the "off" branch does something different than the "on" branch, the "off" branch is dead code but may be load-bearing for rollback. When removing, leave a comment explaining the rollout date and link to the removal PR, so the git blame tells the story.

6. **Per-env flag safety.** Env-var flags are often deployed inconsistently. `process.env.BETA_MODE` set to `'true'` in staging but unset in prod is a common drift source. Flag any env-var-based feature flag that doesn't have a fallback in code (`process.env.BETA ?? 'false'`).

**What to fix:**
- Remove ghost flags (reference without definition).
- Remove orphan flags (definition without reference).
- Remove fully-rolled-out flags and their dormant branches.
- Resolve conflicting / redundant flags.

**What not to do:**
- Do NOT remove a flag that's in an active A/B test or gradual rollout — check recent commits and the flag's metadata before deleting.
- Do NOT remove a "kill switch" flag (a flag that can disable a feature in an emergency) even if it's currently on — these exist deliberately. Look for comments or naming (`kill_`, `disable_`, `emergency_`).
- Do NOT add new flags in this check — that's a product decision.

Run the test suite after changes. Report: flags found, ghost/orphan/dormant/conflicting counts, and what was removed.
