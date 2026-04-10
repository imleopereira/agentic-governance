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

CREATE INDEX IF NOT EXISTS idx_cost_session_agent
    ON governance_cost_session_usage (agent_id);
CREATE INDEX IF NOT EXISTS idx_cost_daily_agent
    ON governance_cost_agent_daily (agent_id, day_utc DESC);
