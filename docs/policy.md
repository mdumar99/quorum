# Policy versions

The return policy is data plus code (ADR-0003, Decision 2: `docs/adr/0003-decision-pipeline-and-llm.md`). The parameters live in a versioned file. The rule logic, R1 to R3, is code keyed by rule ID. `evaluate(policy, case, as_of)` is pure: it reads no clock and no database, and `as_of` is the database time that the caller passes in. R4 (a duplicate return) is not a policy rule: the database enforces it and the returns API records it.

## Where things are
- This page: `docs/policy.md`.
- The current version: `apps/api/app/policy/versions/v1.toml`.
- The loader (file validation, content hash, pinned loading): `apps/api/app/policy/loader.py`.
- The types: `apps/api/app/policy/models.py`. The rules and `evaluate`: `apps/api/app/policy/rules.py`.
- The current version is loaded once at startup in `apps/api/app/main.py`. An invalid file stops the boot.
- The tests: `apps/api/tests/policy/test_policy_file.py` pins the hash of every released version.

## The version string
A return is pinned with a `policy_version` written `v1:<64 hex characters>`: the version name, a colon, and the SHA-256 of a canonical serialization of the validated parameters (sorted keys, the separators "," and ":", ASCII only, excluded categories sorted). It is not a hash of the file bytes, so comments, whitespace, CRLF and key order cannot change it. Loading a pinned version whose file no longer has the pinned hash fails, and nothing is returned.

The hash covers the parameters, not the rule code. If the behaviour of a rule changes while the parameters stay the same, the version string does not change, and old and new decisions can no longer be compared by it. A change in rule behaviour therefore needs a new version file.

## Add a version
1. Copy `apps/api/app/policy/versions/v1.toml` to `apps/api/app/policy/versions/v<N>.toml` and set `version` to the new name. The name must match the file name.
2. Change the parameters. Every key is required, unknown keys are rejected, and amounts are integers in minor units (a float fails).
3. In the same commit, change `CURRENT_POLICY_NAME` in `apps/api/app/policy/loader.py` to the new name.
4. Pin the new hash in `apps/api/tests/policy/test_policy_file.py`, next to the pin of v1.
5. The old file stays where it is, so a return pinned with the old `policy_version` can still be loaded.

## A released version is never edited
Returns and decisions store the `policy_version` they were made with. Editing the parameters of a released file changes its hash, so every return pinned to that version then fails to load (a hash mismatch). The hash ignores comments and whitespace, but the rule is still that a released version is never edited: the file stays identical to what past decisions used, and a change is a new version file.

## What evaluate returns
`evaluate` returns a PolicyResult: `policy_version`, `hard_deny`, `allowed_outcomes` (always in the order approve, deny, escalate) and `rule_results`. R1, R2 and R3 always all run, in that order, and every result is recorded, passed or failed. A result cannot contradict its rule results: `hard_deny` is true exactly when a failed rule has effect `hard_deny`, and `allowed_outcomes` is derived from them. `rule_results` has exactly one of two shapes: R1, R2, R3 in that order, or R4 alone as a failure; an empty, partial, reordered or repeated array is rejected. Each rule also has a fixed effect (R1, R2 and R4 `hard_deny`, R3 `limit_outcomes`), and a result whose entry has another effect is rejected.

The database column `decisions.rule_results` stores only the array of rule results; `policy_version` and `allowed_outcomes` have their own columns. A rule result has five keys: `rule_id`, `passed`, `effect` (what a failure of this rule does: `hard_deny` or `limit_outcomes`), `reason_code` and `details`. The key is called `reason_code` so that it is never confused with the return's free-text `reason`, which the rules never see. `reason_code` and the keys of `details` are short snake_case codes, `details` is flat, and its string values are at most 64 characters. The details hold only numbers, booleans, ISO dates and values taken from the policy file, never the client's category or currency.

Example 1, all rules pass (shoes, 1999 USD, delivered 2026-11-01, `as_of` 2026-11-14 at 09:30 UTC):

```json
{
  "policy_version": "v1:8b7bd6fa56e53a2fb799f5fb60855bde004a6b6adb6ac30c70667c7f9abc324c",
  "hard_deny": false,
  "allowed_outcomes": ["approve", "deny", "escalate"],
  "rule_results": [
    {"rule_id": "R1", "passed": true, "effect": "hard_deny", "reason_code": "within_window",
     "details": {"delivered_on": "2026-11-01", "as_of_date_utc": "2026-11-14", "days_since_delivery": 13, "window_days": 30}},
    {"rule_id": "R2", "passed": true, "effect": "hard_deny", "reason_code": "category_allowed",
     "details": {"category_excluded": false, "matched_category": null}},
    {"rule_id": "R3", "passed": true, "effect": "limit_outcomes", "reason_code": "within_ceiling",
     "details": {"amount_minor": 1999, "ceiling_minor": 20000, "currency": "USD", "currency_matches": true}}
  ]
}
```

Example 2, R1 and R2 fail together (digital, delivered 2026-09-01, 1999 USD). R3 still runs and passes, and the hard deny wins:

```json
{
  "policy_version": "v1:8b7bd6fa56e53a2fb799f5fb60855bde004a6b6adb6ac30c70667c7f9abc324c",
  "hard_deny": true,
  "allowed_outcomes": ["deny"],
  "rule_results": [
    {"rule_id": "R1", "passed": false, "effect": "hard_deny", "reason_code": "outside_window",
     "details": {"delivered_on": "2026-09-01", "as_of_date_utc": "2026-11-14", "days_since_delivery": 74, "window_days": 30}},
    {"rule_id": "R2", "passed": false, "effect": "hard_deny", "reason_code": "category_excluded",
     "details": {"category_excluded": true, "matched_category": "digital"}},
    {"rule_id": "R3", "passed": true, "effect": "limit_outcomes", "reason_code": "within_ceiling",
     "details": {"amount_minor": 1999, "ceiling_minor": 20000, "currency": "USD", "currency_matches": true}}
  ]
}
```

R3 reports `currency_mismatch` before `above_ceiling`: a case in another currency is not approvable and its amount is not compared with the ceiling. An amount above the ceiling gives `hard_deny` false and `allowed_outcomes` of deny and escalate.

R4 fires at insert, before R1 to R3 run, so the returns API stores an array with one entry for a duplicate return:

```json
[{"rule_id": "R4", "passed": false, "effect": "hard_deny", "reason_code": "active_return_exists", "details": {}}]
```
