-- Cost / budget enforcement tables.
--
-- Postgres-backed counters for multi-process correctness. Concurrent
-- track() calls from different worker processes are serialized at the
-- row level by Postgres's UPSERT machinery — no advisory lock or
-- SELECT FOR UPDATE needed.

CREATE TABLE IF NOT EXISTS governance_cost_session_usage (
    agent_id      VARCHAR(256)     NOT NULL,
    session_id    UUID             NOT NULL,
    usd_used      DOUBLE PRECISION NOT NULL DEFAULT 0,
    tokens_used   BIGINT           NOT NULL DEFAULT 0,
    last_updated  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, session_id)
);

CREATE TABLE IF NOT EXISTS governance_cost_agent_daily (
    agent_id      VARCHAR(256)     NOT NULL,
    day_utc       DATE             NOT NULL,
    usd_used      DOUBLE PRECISION NOT NULL DEFAULT 0,
    tokens_used   BIGINT           NOT NULL DEFAULT 0,
    last_updated  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, day_utc)
);

-- Per-model daily cost breakdown (v0.3).
-- Nullable model column on main tables is backwards-compatible; this
-- separate table gives a clean per-model aggregation surface.
CREATE TABLE IF NOT EXISTS governance_cost_model_daily (
    agent_id      VARCHAR(256)     NOT NULL,
    model         VARCHAR(128)     NOT NULL,
    day_utc       DATE             NOT NULL,
    usd_used      DOUBLE PRECISION NOT NULL DEFAULT 0,
    tokens_used   BIGINT           NOT NULL DEFAULT 0,
    last_updated  TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, model, day_utc)
);

CREATE INDEX IF NOT EXISTS idx_cost_session_agent
    ON governance_cost_session_usage (agent_id);
CREATE INDEX IF NOT EXISTS idx_cost_daily_agent
    ON governance_cost_agent_daily (agent_id, day_utc DESC);
CREATE INDEX IF NOT EXISTS idx_cost_model_daily_agent
    ON governance_cost_model_daily (agent_id, day_utc DESC);
