# ADR-0004: Returns API contract

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** mdumar99

## Context
Phase 2 turns "decision" into "decision or human review" and moves execution to a worker. The contract must survive that. Phase 1 can run the whole pipeline inside the request, but a close case in Phase 2 waits for a human for hours, and no request can wait that long, so `POST /returns` will have to answer "here is a handle, the outcome arrives later". If Phase 1 shipped a bare "created, here is the decision" response, Phase 2 would change its shape and break clients. This ADR therefore fixes the parts that must not change later: the resource shape, the status vocabulary and its transitions, the HTTP behaviour, idempotency, recovery from crashes and cancellation, and what counts as an active return. The pipeline itself is ADR-0003, and the database side is ADR-0002.

## Decision

### 1. Request, resource and deadlines
`POST /returns` takes: `order_id`, `order_line_id`, `category`, `amount_minor` (integer), `currency`, `delivered_on` (a calendar date) and `reason` (free text, at most 2,000 characters). A longer reason, a currency other than the single Phase 1 currency, or any missing or malformed field gets `422` with no resource. The currency check exists because R3 compares amounts in one currency only (ADR-0003).

`POST /returns` and `GET /returns/{id}` return the same resource: id, status, outcome (or null), decision (or null), error_type (or null), retry_after_seconds (or null). `id` is a random UUID (v4), so the unauthenticated `GET` cannot be enumerated. Clients branch on `status`, not on the HTTP code, and tolerate unknown statuses.

Deadlines follow one ordering invariant: **pool checkout timeout < LLM call timeout < pipeline deadline < client timeout**. Phase 1 defaults, all configurable: pool checkout 5 s, LLM call 20 s, pipeline deadline 30 s, staleness grace 30 s, and a documented client timeout of at least 40 s. The staleness threshold in Decision 5 is the pipeline deadline plus grace (60 s).

`decision` includes the model's rationale, which is untrusted text (ADR-0003, Decision 9). Whoever renders the resource must escape it.

Execution is synchronous: `POST /returns` stores the return, runs the pipeline inline and responds with the resource in its final state. The database transaction is split in two so that none is held open across the LLM call. The client timeout must exceed the pipeline deadline.

### 2. Vocabulary and transitions
- status: `processing`, `decided`, `pending_review`, `failed`. outcome: `approve`, `deny`, `escalate`.
- `error_type` is one check-constrained list: transient `llm_timeout`, `rate_limited`, `provider_unavailable`, `capacity` and `abandoned`; permanent `invalid_llm_output`. No other value is written, and a new value needs a migration.
- Every transition out of `processing` is a compare-and-set on both `status` and `attempt` (Decision 5).

| From | To | When |
|---|---|---|
| processing | decided | The pipeline produced `approve` or `deny`: a rule hard deny, or a model outcome that passed validation against `allowed_outcomes` (ADR-0003). |
| processing | pending_review | The validated model outcome is `escalate`. |
| processing | failed | Any failure after the return is persisted: transient (`llm_timeout`, `rate_limited`, `provider_unavailable`, `capacity`, or `abandoned` for a stale row taken over in Decision 5) or permanent (`invalid_llm_output`). |
| failed (transient) | processing | A retry with the same `Idempotency-Key`. `attempt` is incremented. `retry_after_seconds` is advisory (Decision 4). The update re-enters the unique index, so it can lose to R4 (Decision 6). |
| failed (transient) | decided | A retry that loses the unique-index race: `deny`, citing R4 (Decision 6). |
| failed (permanent), decided | none | terminal |
| pending_review | none in Phase 1 | Phase 2 adds exits |

### 3. HTTP shape
- Created: `201` plus `Location`. Invalid input: `422`, no resource. Pipeline failure, including `capacity`: `201` with `status: failed`. `503` plus `Retry-After` only when the return cannot be persisted.
- Replay returns the current state of the resource with the original status code, plus `Idempotent-Replayed`. That keeps replay and polling in agreement once Phase 2 can move a return from `pending_review` to `decided`.
- A same-key request after a transient failure is a retry, not a replay: it re-runs the pipeline and returns `201` without `Idempotent-Replayed`. That header means only "served from storage, nothing re-ran".
- Alerting watches `status=failed` in logs or metrics, since HTTP codes no longer show pipeline failures.

### 4. Idempotency
- `Idempotency-Key` is required, and a payload hash detects reuse. A missing key gets `422` with no resource, the same as any other invalid input. (The expired IETF `Idempotency-Key` draft is believed to suggest `400` here; this was not verified, and `422` is our choice, not the draft's.)
- Same key with a different payload: `422`, with no resource. Duplicate while the original is in flight, meaning the row is younger than the staleness threshold (Decision 5): `409`, with the in-flight resource (`status: processing`) as the body and a `Location` header, so the client can poll `GET /returns/{id}` instead of guessing. These two follow the expired IETF `Idempotency-Key` draft (-07): `422` for a reused key with a different payload and `409` for a retry while the original is still being processed. `409` is chosen over joining the in-flight request because joining would hold a second connection open for the whole pipeline.
- A retry re-runs only after a transient failure. `retry_after_seconds` is advisory: an early retry runs anyway, and the concurrency cap (ADR-0003, Decision 7) and the provider's own limits bound the damage. Every attempt writes an audit row; `decisions` rows exist only for valid decisions.
- `retry_after_seconds` is taken from the provider's `retry-after` when there is one, otherwise a fixed default per error type, and is capped at 3,600. A provider daily cap can ask for many hours; the cap keeps the value meaningful to a client, which may simply fail again with `rate_limited`.
- Keys expire. A late retry with an expired key is a new submission. At most one approval can exist per order line whatever key is used, because R4 denies a submission while an earlier one is active, so a short TTL cannot cause a double refund. A late retry after a `deny` or a failure is simply a new attempt. A late retry after an approval gets "deny, R4": safe, but it looks wrong to a client that has forgotten its first request, so the client documentation says so.
- Where the key and payload hash are stored, and the TTL, are schema-ticket decisions. This ADR fixes only what an expired key means.

### 5. Recovery, cancellation and attempt fencing
- **Attempt fencing.** `returns.attempt` is a monotonic integer, set to 1 on insert and incremented every time a row enters `processing`. Each attempt remembers its own number. Every transition out of `processing`, including a takeover, is `UPDATE … WHERE id = $id AND status = 'processing' AND attempt = $mine`, and the row count is checked. Zero rows means another attempt now owns the row, so this attempt writes only its audit row and stops. Status alone is not enough because takeover makes status repeat (`processing → failed → processing`): without `attempt`, a late-finishing original attempt would match the new attempt's row and overwrite it. `attempt` is recorded in every audit row.
- **One staleness rule.** It covers a request that finds a `processing` row, whether it is a same-key duplicate or a submission under another key. If the row is younger than the staleness threshold (Decision 1), a same-key request gets `409` with `Location` (Decision 4) and a different-key submission is denied by R4 (Decision 6). If it is older, the row is stale and is taken over. Age is measured with the database clock (ADR-0002).
- **Takeover** is the fenced compare-and-set `processing → failed` (`error_type: abandoned`, transient), using the attempt number read from the stale row, plus an audit row. A same-key retry then continues as a normal retry (`failed → processing`, `attempt + 1`). A different-key submission retries its insert once, and if that collides again, R4 applies. This needs no background job, and a crash cannot block an order line for good. If the takeover was triggered by a different key, the original client's later retry with its own key collides with the new active row and is denied by R4 (Decision 6); client documentation should say so.
- **Cancellation.** A client disconnect did not cancel the handler in the tested stack (fastapi 0.141.1, starlette 1.7.0, uvicorn 0.54.0 with httptools and uvloop), with or without the `log_requests` middleware. The pipeline ran to completion and wrote its normal result, so Phase 1 specifies no cancellation write: the pipeline finishes normally. An abandoned request keeps working; in the test, a write blocked on a row lock completed about 6 s after the client had gone. A row left in `processing` for another reason, such as a crash or a server shutdown, is recovered by the staleness takeover above. Cancellation by server shutdown and a shielded write were not tested. If a later change makes cancellation reachable, a new ADR supersedes this rule.
- **Spike result (P1-3, see the [#39 closing comment](https://github.com/mdumar99/quorum/issues/39#issuecomment-6841235110)):** five cases, one run each, real uvicorn in a container. A as expected. B contradicted: the handler was not cancelled and the row ended `decided`. C the same with the middleware removed. D: the fence rejected the stale success-path write (rowcount 0) and the row stayed `processing` at attempt 2. E: the blocked write completed after the lock was released.
- Monitoring distinguishes `pending_review` (expected waiting) from stuck `processing`.

### 6. "Active" return and R4
- A partial unique index per order line over rows where `status IN ('processing','pending_review') OR (status = 'decided' AND outcome = 'approve')`. `returns.outcome` is denormalized and written in the same transaction as the decision row.
- The app role's column-level `UPDATE` on `returns` is exactly `status`, `outcome`, `error_type`, `retry_after_seconds`, `attempt` and `updated_at`. Every other column, including `as_of`, `policy_version`, the fact snapshot and the idempotency key, has no `UPDATE` grant (ADR-0002, Decision 3).
- `failed` is deliberately not active. A failed return produced no decision and cannot yield a refund, so it does not hold the slot, and the index re-checks when a retry moves it back to `processing`. A stale `processing` row does sit inside the predicate, which is why takeover on collision (Decision 5) is needed.
- The loser of a collision with an active row receives `201` with `status: decided`, `outcome: deny` and a decision citing R4. A new submission that hits the index on insert is stored directly as a `decided`/`deny` row, which is outside the predicate so the insert succeeds, together with its decision and audit rows in one transaction. A retry that hits the index on the `failed → processing` update is finalized as `deny` the same way. Each statement that can hit the index runs inside a savepoint, because a `23505` aborts the surrounding transaction unless the statement is wrapped. Rolling back to the savepoint lets the transaction go on to write the deny. The conflicting return's id goes in the audit row and is not returned to the client, so a response never reveals another submission.
- A collision with a stale `processing` row is not a loser case. The row is taken over as in Decision 5 and the insert is retried once.
- Cost: a `pending_review` row blocks resubmission until Phase 2.

### 7. Limitations
- No authentication in Phase 1 (host port bound to `127.0.0.1`).
- Combined with R4, that allows abuse: anyone who can reach the port can submit an order line that escalates and park it in `pending_review` indefinitely, blocking legitimate returns for it until Phase 2. Accepted only because the port is local-only.
- Facts are client-asserted (ADR-0003).

## Consequences
### Positive
- Phase 2 can add `pending_review` exits and a worker without changing the resource shape or the status codes, because clients already branch on `status` and tolerate unknown values.
- `POST` and `GET` return one shape, and a failure is data, so the client always keeps the id of the return it created.
- A retry with the same key can never create a second return, and the database allows only one active return per order line whatever key is used.
- Recovery from a crashed attempt needs no background job, only takeover on the next request that meets the stale row.
- Every attempt, including failed ones and R4 collisions, leaves an audit row.

### Negative / trade-offs
- HTTP status no longer shows pipeline failures, so alerting has to watch `status=failed` in logs or metrics, which is monitoring work we have to build.
- Clients carry more than with a plain `201`/5xx API: they must send an idempotency key, branch on `status`, and tolerate values they do not know yet.
- `pending_review` rows block resubmission and have no exit until Phase 2, so Phase 1 can accumulate waiting rows.
- Synchronous execution holds the request open for the whole pipeline, so the client timeout must exceed the pipeline deadline.
- Anything that renders `decision` must treat the rationale as untrusted text (ADR-0003).
- With no authentication in Phase 1, anyone who can reach the port can submit returns, poll them, and park order lines in `pending_review` (Decision 7). The `127.0.0.1` bind is the only guard.
- Every transition carries an attempt check, which is one more condition in every status update and one more thing to test.

## Open before Accepted
Per the ADR README, an ADR is Accepted when its PR merges and is not edited afterwards. Only the item below can change a decision here, so it is settled before the PR merges. The two-connection tests, the attempt-fencing test and key storage and TTL are ticket acceptance criteria.
- [x] Cancellation spike result (Decision 5). The experiment disconnects a client mid-pipeline and records whether the handler is cancelled, what state the row is left in, and whether the fenced shielded write lands. If a disconnect does not cancel the handler, the cancellation rule shrinks to "the pipeline finishes normally" and Decision 5 says so. Result: a disconnect did not cancel the handler (cases B and C, with and without the `log_requests` middleware), so the rule shrinks to "the pipeline finishes normally" and Decision 5 says so. The shielded write was not exercised. See the [#39 closing comment](https://github.com/mdumar99/quorum/issues/39#issuecomment-6841235110).

## Alternatives considered
| Decision | Alternative | Why not chosen |
|---|---|---|
| Execution | In-process async with `202` | The contract is already shaped for async, so this would only add temporary machinery that Phase 2's worker replaces (ADR-0001). It also adds restart handling for tasks lost with the process, and polling in tests and the smoke test. |
| Execution | Durable queue now | It is Phase 2 work per ADR-0001. Redis is already provisioned, but a queue adds a worker process, delivery guarantees, retry and dead-letter design, and Compose and CI changes. The Phase 1 contract does not block it, because moving later changes how execution happens, not the shape clients see. |
| Failure shape | 5xx on pipeline failure | The return row already exists, and a 5xx would hide its id, so the client could not poll it. It would also make a pipeline failure look the same as "could not persist", which are different situations. Failure is resource state, and 5xx is reserved for a return that could not be stored. The cost is HTTP-level alerting, covered in Decision 3. |
| Idempotency | Join the in-flight request instead of `409` | Joining would hold a second connection open for the whole pipeline and needs a wait mechanism. A `409` with the resource and `Location` lets the client poll instead. |
| Escalation | `202` for `pending_review` | It would change the status code between phases, which is a second contract change. The return is created either way, so `201` stays and the body's `status` carries the state. |
| Recovery | Compare-and-set on `status` only | Takeover makes status repeat (`processing → failed → processing`), so a late-finishing original attempt would match the new attempt's row and overwrite it. The attempt number fences that out. |
| Idempotency | Optional key | Unkeyed retries could not be recognised, so a timeout followed by a retry would arrive as a new return and the audit trail could not link the attempts. R4 would still stop a double refund, but it is the backstop and not the primary guard. |
