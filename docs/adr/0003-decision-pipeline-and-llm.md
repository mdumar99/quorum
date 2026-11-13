# ADR-0003: Decision pipeline and LLM integration

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** mdumar99

## Context
Phase 1 is the first phase that makes decisions about returns, so the pipeline has to meet three requirements. It must be testable in CI without a live model or an API key. It must be fail-closed: when anything is uncertain, the result is a recorded failure or an escalation, never an approval by default. And it must be auditable: every decision can be explained from stored inputs, a policy version and, for model output, a recorded model and prompt version. This ADR fixes the pipeline shape, how policy is modelled, where the rules end and the model begins, and how the LLM is integrated. The pipeline deadline lives in ADR-0004, and the database side is in ADR-0002.

## Decision

### 1. Pipeline shape
Intake (validate, snapshot facts) → Policy Check (deterministic) → Decision (LLM). Nodes are plain typed functions.

Phase 1 does not adopt LangGraph. The graph would have one conditional edge, which is an `if` in a plain function, and the framework would add a state schema and a dependency without adding behaviour the code lacks. Because the nodes are plain typed functions with a stable `PolicyResult` contract, wrapping them in a graph later is a thin change. The Phase 2 panel, with several agents per case, is where a graph earns its place, and that decision belongs to that phase's ADR.

### 2. Policy as versioned data
- A versioned file holds parameters; rule logic is code keyed by rule ID. The file is validated at startup.
- The policy rules (R1–R3) are pure: `evaluate(policy, case, as_of)`. `as_of` is the database time (`now()`) of the transaction that first inserts the return (ADR-0002, Decision 2). It is never client-supplied and is reused on every retry.
- `policy_version` (version string plus content hash) is pinned with `as_of` at first persist. Old versions stay loadable. Both columns, and the fact snapshot, are excluded from the app role's `UPDATE` grants, so a retry cannot change them (ADR-0002, Decision 3).
- Amounts are integer minor units or Decimal, never float. Passed rules are recorded as well as failed ones.
- Window semantics: days are counted between the delivery date (a calendar date) and the date of `as_of` in UTC. The window is inclusive: a return received 30 days after delivery is in time, and one received 31 days after is not. UTC keeps the result deterministic and reproducible, at the cost that a customer near local midnight can be a day off at the boundary. That is accepted for Phase 1.
- The first policy (v1), with its parameters in the versioned file:
  - **R1, return window:** received more than 30 days after delivery, or with a delivery date later than the date of `as_of` → hard deny.
  - **R2, excluded categories** (v1: `final_sale`, `digital`) → hard deny.
  - **R3, amount ceiling** (v1: 20000 minor units; Phase 1 assumes one currency): above the ceiling, the model may deny or escalate but never approve.
- **R4, duplicate, is not a policy rule.** It is a database invariant (ADR-0002, ADR-0004): the partial unique index on the order line rejects a second active return through the `23505` path, both on insert and on a retry's `failed → processing` update. The rejection is recorded with rule ID `R4` so the audit trail reads uniformly, and it is a hard deny that never reaches the model. R4 has no parameter in the policy file, so `policy_version` does not govern it. "Active" has the same definition as the index predicate. A pre-check before the insert may exist as an optimisation, but the index is the authority.
- Because R4 fires at insert, before R1–R3 run, a duplicate that would also fail R1 or R2 records only R4. The audit trail shows the first reason the return was stopped, not every reason it would have been.

### 3. Rule/LLM boundary
- A hard deny short-circuits before any LLM call.
- `allowed_outcomes` is derived from the rules and validated after the model returns. Within the ceiling it is `{approve, deny, escalate}`; above it (R3) it is `{deny, escalate}`. A violation is a permanent `invalid_llm_output`, never coerced.
- `escalate` leads to `status: pending_review` (ADR-0004).
- `decided_by` is `rules` or `llm`.

### 4. Facts source
Client-supplied and snapshotted on the return row, behind a facts-provider interface. The rules apply policy consistently to asserted facts and do not verify them.

### 5. LLM client and provider
- `LLMClient` interface; Groq adapter; fake adapter that can simulate timeout, rate limit and invalid output.
- Own deadline, no hidden SDK retries. No retries happen inside the pipeline: a transient failure is returned to the client as a failed return carrying `retry_after_seconds`, and the client retries with the same idempotency key (ADR-0004).
- Provider errors are normalized from the SDK into transient (`llm_timeout`, `rate_limited`, `provider_unavailable`) and permanent (`invalid_llm_output`, including refusals and truncation). `capacity` is not a provider error: it comes from the pipeline's own semaphore (Decision 7) and is also transient.
- Groq with a strict-schema-capable model, pinned by explicit ID. The model ID, provider and prompt version are recorded on each LLM decision (Decision 10). Phase 1 uses synthetic data only.
- Facts checked on 2026-10-05 from the Groq docs ([structured outputs](https://console.groq.com/docs/structured-outputs), [rate limits](https://console.groq.com/docs/rate-limits), [spend limits](https://console.groq.com/docs/spend-limits)): strict schema-constrained output is available only on select models, with a best-effort mode elsewhere, and strict mode requires every field to be `required` and `additionalProperties: false`, so the decision schema is written that way. Free-tier rate limits are per model and tight, with daily token caps, and exceeding them returns HTTP 429 with a `retry-after` header. Spend limits exist only on paid tiers. The exact model ID is chosen from Groq's models page when the adapter is built and recorded in that ticket; it was not checked for this ADR.
- Why Groq: the free tier means there is no spend to cap or to run away, so Phase 1 needs no budget controls. Its tight limits also make the `rate_limited` path real rather than hypothetical, which exercises the transient-failure handling in normal use. The cost is those same limits: daily token caps will bind during live testing, and strict mode narrows the choice of model. That is why the fake provider carries CI and the smoke test (Decision 8), and live tests stay few and opt-in. If the account is ever upgraded to a paid tier, set a spend limit before enabling billing.

### 6. Keys and configuration
- `LLM_PROVIDER` is required and has no default in code. A deployment that forgets it fails at startup instead of silently making fake decisions, which would be the opposite of fail-closed.
- Key is a `SecretStr`, never logged, required only when the provider is not `fake`.
- `.env.example` carries a placeholder key and sets `LLM_PROVIDER=fake`, so a clean clone and the CI smoke job need no key. Compose passes both explicitly via `environment:`.
- No GitHub Actions secrets in Phase 1.

### 7. Concurrency cap
A per-process semaphore guards only the LLM call, with a zero-wait try-acquire. Full means a transient `capacity` failure. Replays and rule-decided cases never touch it. The size is the setting `LLM_MAX_CONCURRENCY`, default 4.

### 8. Testing
The fake provider carries unit tests, CI and the smoke test. Live tests are few, `@pytest.mark.live`, opt-in.

A test selects a failure explicitly. Unit tests construct the fake adapter with a scripted sequence of outcomes, and the Compose and smoke-test path uses a `FAKE_LLM_MODE` setting. The fake never inspects the return's text for triggers, and the setting is read only by the fake adapter, so it has no effect when the provider is not `fake`.

### 9. Untrusted input
The return's free text is untrusted: passed as delimited data, output validated, and the model never the only gate. The API caps the text's length, and a reason longer than the maximum (2,000 characters, ADR-0004 Decision 1) is rejected with `422`. The cap bounds token use against the free tier's daily caps and shrinks the injection surface. Residual risk: within `allowed_outcomes`, injected text can still sway the model, so an approval the rules permit may be one a human would reject. Validation bounds the outcome, not the judgment. The model's rationale is also derived from untrusted text, so it must be treated as untrusted wherever it is stored or displayed.

### 10. What each attempt records
ADR-0004 says `decisions` rows exist only for valid decisions, and every attempt writes an audit row. So the record is split by where it can exist:

**Every LLM call, valid or not, in the attempt's audit row payload:**
- `provider` (including `fake`), model ID and `prompt_version`;
- latency and token counts;
- the raw model output, stored verbatim as text (an invalid response may not be valid JSON);
- the attempt number (ADR-0004, Decision 5).

**Every valid decision, in its `decisions` row:**
- the outcome and `decided_by`;
- the rule results (passed and failed) and the `allowed_outcomes` derived from them, and a copy of `policy_version`. `as_of` and the authoritative `policy_version` are on the return, pinned at first persist (Decision 2); the decision's copy is written from the return's value in the same transaction and must equal it, and a test asserts that;
- for LLM decisions, the same provider, model, prompt version, latency, tokens and raw output fields, so a decision can be read without joining the audit log.

Raw output is untrusted text in insert-only tables, so it is capped: at most 16 KiB (16,384 bytes of UTF-8) is stored, cut at a character boundary so the stored text is valid UTF-8, with the original length in bytes and a SHA-256 hash of the full output, plus a `truncated` flag. The hash lets a reviewer confirm that two attempts produced identical output without storing it twice.

`prompt_version` is defined like `policy_version`: a version string plus a content hash of the template. Rule-decided cases made no model call, so their LLM fields are empty. Because `provider` is recorded on every LLM call, a decision made by the fake adapter is visible in the audit trail.

## Consequences
### Positive
- Rule outcomes are reproducible from the inputs, `policy_version` and `as_of`. Model outputs are recorded with the provider, model ID, prompt version, latency, token counts and raw output, so every decision can be explained even when it cannot be reproduced.
- The pipeline fails closed. Hard denies never reach the model, output outside `allowed_outcomes` is rejected rather than coerced, a missing `LLM_PROVIDER` stops startup, and every failure falls into a defined transient or permanent class.
- Rule-decided cases complete even during a provider outage.
- Free text reaches only one model call, the Decision node, so it cannot talk a rule out of firing. It is also stored and may later be displayed, which Decision 9 treats as untrusted.
- CI and the smoke test need no key and no network, and the fake provider can simulate each failure, so the failure paths are testable.
- The provider is replaceable by one adapter and a pinned model ID.

### Negative / trade-offs
- Free-tier limits cap how much live testing and evaluation can run, narrow the model choice to strict-schema-capable models, and are outside our control, since the provider can change them.
- Old policy versions must stay loadable for as long as any decision references them, so policy files can never be deleted.
- Facts are client-asserted. The rules guarantee consistent application of policy, not truth: a caller who misstates the delivery date passes R1. A lookup behind the facts-provider interface is the Phase 2 answer.
- Policy expressiveness is limited to what rule code can state. Interpretive clauses wait for a hybrid check in Phase 2.
- The concurrency cap is per process, so with several workers the effective cap is the semaphore size times the worker count.
- Every environment must set `LLM_PROVIDER` explicitly.
- Storing raw model output, latency and token counts on every LLM call grows the audit and decisions tables and keeps untrusted text in them. The 16 KiB cap bounds the growth per call.
- Dropping LangGraph departs from the handoff's Phase 1 plan ("one LangGraph pipeline"). The Phase 1 milestone description states the plan as plain functions, with LangGraph deferred to Phase 2.
- Prompt injection is bounded, not eliminated (Decision 9).

## Alternatives considered
| Decision | Alternative | Why not chosen |
|---|---|---|
| Policy Check | LLM-based | It means two LLM calls per return, which doubles the failure surface (timeouts, rate limits, malformed output) and lets free text reach both nodes. Rule outcomes would become records rather than reproducible results, and hard rules would depend on a model. The flexibility it offers is deferred to a Phase 2 hybrid behind the same `PolicyResult` contract. |
| Pipeline | LangGraph adopted now | The pipeline has one conditional edge, which is an `if`, so the framework would add a state schema and a dependency without adding behaviour. The nodes are plain typed functions, so wrapping them later is a thin change. The Phase 2 panel is where it pays off. |
| Provider | OpenAI | A plausible fit for a fail-closed design, with strict schema output and account-level spend controls. Its spend-limit behaviour and error codes were not verified for this ADR, so they are not relied on here. The decisive point is that it costs real money, which Groq's free tier avoids for Phase 1. The neutral client keeps it a one-adapter change, and the provider's limits would be verified, with a date and source, in the ADR that adopts it. |
| Provider | Local model | No key and no cost, but output quality is expected to be weaker for this task, CI cannot run it, and containers need a route to the host. |
