"""Returns, decisions and audit_log: the first tables of the data model (#44).

Hand-written to the schema proposal approved on issue #44:
https://github.com/mdumar99/quorum/issues/44#issuecomment-6875124145
It follows ADR-0002 (D1 forward-only, D3 privileges), ADR-0003 (D2, D3, D10) and ADR-0004 (D1,
D2, D4, D5, D6).

Forward-only (ADR-0002, Decision 1): there is no downgrade. Audit data is never deleted, so an
automatic "undo" would delete rows that policy says are kept. A mistake is corrected by a new
revision that moves forward.

Grants (ADR-0002, Decision 3): quorum_owner creates every table in `public`, so the default
privileges from db/bootstrap.sql already give quorum_app SELECT and INSERT. The only grant here is
the column-level UPDATE on `returns`. `decisions` and `audit_log` get no UPDATE, and DELETE and
TRUNCATE are never granted.

Notes on the SQL:
- A CHECK passes when its expression is NULL, so every rule that must reject a null is written
  with an explicit IS NOT NULL.
- Postgres reports the first failing CHECK in name order. The rules are split so that each CHECK
  can fail first: `returns_state_ck` ignores unknown statuses (`returns_status_ck` reports them),
  and the outcome-in-allowed_outcomes clause lives in `decisions_outcome_ck`, not in
  `decisions_allowed_outcomes_ck`.
- The `>= 0` on latency and token counts sits inside `decisions_llm_fields_ck` and
  `audit_log_llm_call_ck`; the proposal names no separate constraint for it.
- The SQL contains no `:name` pattern (SQLAlchemy would read it as a bind parameter) and no `%`.
"""

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.returns (
            id uuid NOT NULL DEFAULT gen_random_uuid(),
            order_id text NOT NULL,
            order_line_id text NOT NULL,
            category text NOT NULL,
            amount_minor bigint NOT NULL,
            currency text NOT NULL,
            delivered_on date NOT NULL,
            reason text NOT NULL,
            status text NOT NULL,
            outcome text,
            error_type text,
            retry_after_seconds integer,
            attempt integer NOT NULL DEFAULT 1,
            as_of timestamptz NOT NULL DEFAULT now(),
            policy_version text NOT NULL,
            idempotency_key text NOT NULL,
            payload_hash text NOT NULL,
            key_generation integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT returns_pkey PRIMARY KEY (id),
            CONSTRAINT returns_id_policy_version_uq UNIQUE (id, policy_version),
            CONSTRAINT returns_id_v4_ck CHECK (
                substring(id::text from 15 for 1) = '4'
                AND substring(id::text from 20 for 1) IN ('8', '9', 'a', 'b')
            ),
            CONSTRAINT returns_order_id_length_ck CHECK (char_length(order_id) BETWEEN 1 AND 255),
            CONSTRAINT returns_order_line_id_length_ck CHECK (
                char_length(order_line_id) BETWEEN 1 AND 255
            ),
            CONSTRAINT returns_idempotency_key_length_ck CHECK (
                char_length(idempotency_key) BETWEEN 1 AND 255
            ),
            CONSTRAINT returns_amount_minor_ck CHECK (amount_minor >= 0),
            CONSTRAINT returns_status_ck CHECK (
                status IN ('processing', 'decided', 'pending_review', 'failed')
            ),
            CONSTRAINT returns_outcome_ck CHECK (
                outcome IS NULL OR outcome IN ('approve', 'deny', 'escalate')
            ),
            CONSTRAINT returns_error_type_ck CHECK (
                error_type IS NULL OR error_type IN (
                    'llm_timeout', 'rate_limited', 'provider_unavailable', 'capacity',
                    'abandoned', 'invalid_llm_output'
                )
            ),
            CONSTRAINT returns_reason_length_ck CHECK (char_length(reason) <= 2000),
            CONSTRAINT returns_retry_after_ck CHECK (
                retry_after_seconds IS NULL OR retry_after_seconds BETWEEN 0 AND 3600
            ),
            CONSTRAINT returns_attempt_ck CHECK (attempt >= 1),
            CONSTRAINT returns_key_generation_ck CHECK (key_generation >= 1),
            CONSTRAINT returns_state_ck CHECK (
                status NOT IN ('processing', 'decided', 'pending_review', 'failed')
                OR (
                    status = 'processing'
                    AND outcome IS NULL AND error_type IS NULL AND retry_after_seconds IS NULL
                )
                OR (
                    status = 'decided'
                    AND outcome IS NOT NULL AND outcome IN ('approve', 'deny')
                    AND error_type IS NULL AND retry_after_seconds IS NULL
                )
                OR (
                    status = 'pending_review'
                    AND outcome IS NOT NULL AND outcome = 'escalate'
                    AND error_type IS NULL AND retry_after_seconds IS NULL
                )
                OR (
                    status = 'failed'
                    AND outcome IS NULL AND error_type IS NOT NULL
                    AND (error_type <> 'invalid_llm_output' OR retry_after_seconds IS NULL)
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX returns_idempotency_key_generation_uq
            ON public.returns (idempotency_key, key_generation)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX returns_active_order_line_uq
            ON public.returns (order_id, order_line_id)
            WHERE status IN ('processing', 'pending_review')
               OR (status = 'decided' AND outcome = 'approve')
        """
    )
    op.execute(
        """
        CREATE TABLE public.decisions (
            return_id uuid NOT NULL,
            policy_version text NOT NULL,
            outcome text NOT NULL,
            decided_by text NOT NULL,
            rule_results jsonb NOT NULL,
            allowed_outcomes text[] NOT NULL,
            provider text,
            model_id text,
            prompt_version text,
            latency_ms integer,
            prompt_tokens integer,
            completion_tokens integer,
            raw_output text,
            raw_output_bytes integer,
            raw_output_sha256 text,
            raw_output_truncated boolean,
            CONSTRAINT decisions_pkey PRIMARY KEY (return_id),
            CONSTRAINT decisions_return_policy_fk
                FOREIGN KEY (return_id, policy_version)
                REFERENCES public.returns (id, policy_version) ON DELETE RESTRICT,
            CONSTRAINT decisions_allowed_outcomes_ck CHECK (
                cardinality(allowed_outcomes) >= 1
                AND allowed_outcomes <@ ARRAY['approve', 'deny', 'escalate']::text[]
            ),
            CONSTRAINT decisions_decided_by_ck CHECK (decided_by IN ('rules', 'llm')),
            CONSTRAINT decisions_llm_fields_ck CHECK (
                (
                    decided_by = 'rules'
                    AND provider IS NULL AND model_id IS NULL AND prompt_version IS NULL
                    AND latency_ms IS NULL AND prompt_tokens IS NULL AND completion_tokens IS NULL
                    AND raw_output IS NULL AND raw_output_bytes IS NULL
                    AND raw_output_sha256 IS NULL AND raw_output_truncated IS NULL
                )
                OR (
                    decided_by = 'llm'
                    AND provider IS NOT NULL AND model_id IS NOT NULL
                    AND prompt_version IS NOT NULL
                    AND latency_ms IS NOT NULL AND latency_ms >= 0
                    AND (prompt_tokens IS NULL OR prompt_tokens >= 0)
                    AND (completion_tokens IS NULL OR completion_tokens >= 0)
                    AND raw_output IS NOT NULL AND raw_output_bytes IS NOT NULL
                    AND raw_output_sha256 IS NOT NULL AND raw_output_truncated IS NOT NULL
                )
            ),
            CONSTRAINT decisions_outcome_ck CHECK (
                outcome IN ('approve', 'deny', 'escalate')
                AND outcome = ANY (allowed_outcomes)
            ),
            CONSTRAINT decisions_raw_output_ck CHECK (
                (
                    raw_output IS NULL AND raw_output_bytes IS NULL
                    AND raw_output_sha256 IS NULL AND raw_output_truncated IS NULL
                )
                OR (
                    raw_output IS NOT NULL AND raw_output_bytes IS NOT NULL
                    AND raw_output_sha256 IS NOT NULL AND raw_output_truncated IS NOT NULL
                    AND octet_length(raw_output) <= 16384
                    AND raw_output_bytes >= octet_length(raw_output)
                    AND raw_output_truncated = (raw_output_bytes > octet_length(raw_output))
                    AND raw_output_sha256 ~ '^[0-9a-f]{64}$'
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.audit_log (
            id bigint GENERATED ALWAYS AS IDENTITY,
            return_id uuid NOT NULL,
            attempt integer NOT NULL,
            event_type text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            provider text,
            model_id text,
            prompt_version text,
            latency_ms integer,
            prompt_tokens integer,
            completion_tokens integer,
            raw_output text,
            raw_output_bytes integer,
            raw_output_sha256 text,
            raw_output_truncated boolean,
            conflicting_return_id uuid,
            CONSTRAINT audit_log_pkey PRIMARY KEY (id),
            CONSTRAINT audit_log_return_fk
                FOREIGN KEY (return_id) REFERENCES public.returns (id) ON DELETE RESTRICT,
            CONSTRAINT audit_log_conflicting_return_fk
                FOREIGN KEY (conflicting_return_id) REFERENCES public.returns (id)
                ON DELETE RESTRICT,
            CONSTRAINT audit_log_attempt_ck CHECK (attempt >= 1),
            CONSTRAINT audit_log_event_type_ck CHECK (char_length(event_type) >= 1),
            CONSTRAINT audit_log_llm_call_ck CHECK (
                (provider IS NULL) = (model_id IS NULL)
                AND (model_id IS NULL) = (prompt_version IS NULL)
                AND (latency_ms IS NULL OR latency_ms >= 0)
                AND (prompt_tokens IS NULL OR prompt_tokens >= 0)
                AND (completion_tokens IS NULL OR completion_tokens >= 0)
            ),
            CONSTRAINT audit_log_raw_output_ck CHECK (
                (
                    raw_output IS NULL AND raw_output_bytes IS NULL
                    AND raw_output_sha256 IS NULL AND raw_output_truncated IS NULL
                )
                OR (
                    raw_output IS NOT NULL AND raw_output_bytes IS NOT NULL
                    AND raw_output_sha256 IS NOT NULL AND raw_output_truncated IS NOT NULL
                    AND octet_length(raw_output) <= 16384
                    AND raw_output_bytes >= octet_length(raw_output)
                    AND raw_output_truncated = (raw_output_bytes > octet_length(raw_output))
                    AND raw_output_sha256 ~ '^[0-9a-f]{64}$'
                )
            )
        )
        """
    )
    op.execute(
        """
        GRANT UPDATE (status, outcome, error_type, retry_after_seconds, attempt, updated_at)
            ON public.returns TO quorum_app
        """
    )
