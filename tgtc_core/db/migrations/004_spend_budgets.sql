-- Persistent, immutable provider ceilings.  A reservation is made before every
-- physical request and remains counted after crashes, refusals and uncertain results.
CREATE TABLE IF NOT EXISTS spend_budgets (
    budget_id                       text PRIMARY KEY,
    state                           text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'closed')),
    fantastic_requests_limit       integer NOT NULL CHECK (fantastic_requests_limit >= 0),
    fantastic_credits_limit        integer NOT NULL CHECK (fantastic_credits_limit >= 0),
    apollo_requests_limit           integer NOT NULL CHECK (apollo_requests_limit >= 0),
    apollo_credits_limit            integer NOT NULL CHECK (apollo_credits_limit >= 0),
    anthropic_requests_limit        integer NOT NULL CHECK (anthropic_requests_limit >= 0),
    anthropic_input_tokens_limit    integer NOT NULL CHECK (anthropic_input_tokens_limit >= 0),
    anthropic_output_tokens_limit   integer NOT NULL CHECK (anthropic_output_tokens_limit >= 0),
    expires_at                      timestamptz NOT NULL,
    created_at                      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS spend_reservations (
    id                       bigserial PRIMARY KEY,
    budget_id                text NOT NULL REFERENCES spend_budgets(budget_id),
    provider                 text NOT NULL CHECK (provider IN ('fantastic', 'apollo', 'anthropic')),
    operation                text NOT NULL,
    attempt_id               bigint NOT NULL REFERENCES request_attempts(id),
    estimated_credits        numeric NOT NULL DEFAULT 0 CHECK (estimated_credits >= 0),
    input_tokens_reserved    integer NOT NULL DEFAULT 0 CHECK (input_tokens_reserved >= 0),
    output_tokens_reserved   integer NOT NULL DEFAULT 0 CHECK (output_tokens_reserved >= 0),
    input_tokens_used        integer,
    output_tokens_used       integer,
    status                   text NOT NULL DEFAULT 'reserved'
                             CHECK (status IN ('reserved', 'served', 'uncertain', 'refused', 'failed')),
    created_at               timestamptz NOT NULL DEFAULT now(),
    finished_at              timestamptz,
    UNIQUE (attempt_id)
);
CREATE INDEX IF NOT EXISTS spend_reservations_budget_provider_idx
    ON spend_reservations (budget_id, provider, created_at);
