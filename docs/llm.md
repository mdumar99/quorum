# The LLM client

How the API talks to a model (ADR-0003, Decisions 5, 6, 8 and 10: `docs/adr/0003-decision-pipeline-and-llm.md`). This page covers the client half that needs no model and no database: the interface, the fake adapter, the settings, the normalized errors and the capture of the raw output. The Groq adapter is P1-14, and the prompt, the decision schema and the semaphore are P1-12.

## Where things are
- The client base class (deadline, timing, capture, error mapping): `apps/api/app/llm/client.py`.
- The fake adapter: `apps/api/app/llm/fake.py`. The factory that builds the client: `apps/api/app/llm/factory.py`.
- The errors: `apps/api/app/llm/errors.py`. The types: `apps/api/app/llm/types.py`. The capture: `apps/api/app/llm/capture.py`.
- The settings: `apps/api/app/config.py`. The client is built once at startup in `apps/api/app/main.py` and kept on `app.state.llm_client`.

## Settings
| Setting | Meaning |
|---|---|
| `LLM_PROVIDER` | `fake` or `groq`. Required, no default: a missing value stops the boot, so a deployment never makes fake decisions by accident. |
| `LLM_API_KEY` | One key for any provider. It is hidden from `repr`, from dumps, from the text of a validation error (`str` and `repr`) and from logs. `.errors()` and `.json()` of a validation error still return the input unless the caller passes `include_input=False`; the app never calls them on a `Settings` error. Required when the provider is not `fake`; an empty or whitespace-only value counts as missing. |
| `LLM_TIMEOUT_SECONDS` | The client's own deadline. Default 20, positive and finite. |
| `FAKE_LLM_MODE` | The fake's behaviour (below). Default `ok`. It is read only for the fake and has no effect for any other provider. |

`LLM_PROVIDER=groq` without a key fails settings validation. With a key, the boot stops with a clear message until P1-14 builds the adapter. A clean clone uses `cp .env.example .env`, which sets `LLM_PROVIDER=fake` and an empty key. Your own `.env` needs `LLM_PROVIDER=fake`, or the stack and local `pytest` stop at startup. CI sets it in the `api` job.

## The client
`LLMClient.complete(request)` takes a finished prompt and its `prompt_version` and returns the full text with a call record. An adapter implements only `_call`; the base class does the rest, so no adapter can skip it:
- **Deadline:** `LLM_TIMEOUT_SECONDS`, enforced with `asyncio.timeout`. A call that exceeds it raises `LLMTimeoutError`.
- **No retries:** a failed call is raised once. The caller decides (ADR-0003, Decision 5).
- **Timing:** latency in whole milliseconds, on success and on failure.
- **Capture:** the raw output is capped for storage (below) and put in the record.
- **Mapping:** an adapter raises `ProviderFailure`; the client raises the matching error with the record attached, so a failed attempt can still be audited.
- **Anything else** an adapter raises is a bug and propagates unchanged. A cancelled call raises `CancelledError`, never a timeout.

## Errors
Each error's `error_type` is one of the values `returns.error_type` allows (`db/schema.sql`).

| Error | `error_type` | Kind |
|---|---|---|
| `LLMTimeoutError` | `llm_timeout` | transient |
| `RateLimitedError` | `rate_limited` | transient; carries the provider's `retry-after` as a float, as given |
| `ProviderUnavailableError` | `provider_unavailable` | transient |
| `InvalidLLMOutputError` | `invalid_llm_output` | permanent (including refusals and truncation) |

`capacity` and `abandoned` are in the schema but are not provider errors: the pipeline raises them (P1-12, P1-13), so they are not defined here. `RateLimitedError.retry_after_seconds` stays a float; P1-12 rounds it up to an integer and caps it at 3600. `LLMConfigError` is not an `LLMError`: it stops the boot.

## The fake
It carries the unit tests, CI and the smoke test. It never looks at the prompt and never sleeps.
- **A script** (unit tests): `FakeLLMClient(script=[FakeReply(...), FakeFailure(...)])` gives one outcome per call. A script that runs out raises `FakeScriptExhausted`, which is not an `LLMError`.
- **A mode** (`FAKE_LLM_MODE`): it repeats for every call.

| Mode | Result |
|---|---|
| `ok` | A fixed placeholder text, no token counts. The default. |
| `llm_timeout` | `LLMTimeoutError`, raised at once. |
| `rate_limited` | `RateLimitedError` with a `retry-after` of 1.0. |
| `provider_unavailable` | `ProviderUnavailableError`. |
| `invalid_llm_output` | `InvalidLLMOutputError`, with a short bad text as the raw output. |

A script and a mode cannot be given together. Every record names `fake` as the provider, so a decision made by the fake is visible in the audit trail.

## What is stored from a call
Every call, valid or not, gives a record with ten values that are the LLM columns of `audit_log` and `decisions` (`record.columns()`): provider, model ID, `prompt_version`, latency, the two token counts and the four raw-output fields. The raw output is untrusted text in insert-only tables, so (ADR-0003, Decision 10):
- At most 16,384 bytes of UTF-8 are stored, cut at a character boundary.
- The byte count and the SHA-256 are of the original output, before any replacement or cut. `truncated` is true exactly when the original is longer than what is stored.
- The stored text is the original except for two replacements, each the same size in bytes as what it replaces, so the schema's `raw_output_bytes >= octet_length(raw_output)` always holds:
  - each NUL character (U+0000) becomes U+001A, one byte for one byte, because Postgres `text` cannot hold a NUL;
  - each lone surrogate becomes U+FFFD, three bytes for three bytes, because UTF-8 cannot encode a surrogate. The original's size counts a surrogate as those three bytes.
- Output is never rejected for either: every call is recorded. These are the only cases where the stored text is not verbatim.
