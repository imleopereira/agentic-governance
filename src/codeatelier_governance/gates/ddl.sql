-- HITL gates pending requests.
--
-- Single-table state machine. resolved_at NULL = pending. Resolution
-- transitions are atomic via UPDATE WITH WHERE resolved_at IS NULL,
-- which guarantees single-use semantics across all worker processes.

CREATE TABLE IF NOT EXISTS governance_gates_pending (
    request_id    UUID         PRIMARY KEY,
    agent_id      VARCHAR(256) NOT NULL,
    kind          VARCHAR(128) NOT NULL,
    action_hash   VARCHAR(128) NOT NULL,
    token         TEXT         NOT NULL,
    expires_at    TIMESTAMPTZ  NOT NULL,
    payload_json  JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at   TIMESTAMPTZ  NULL,
    resolution    VARCHAR(16)  NULL  -- 'granted' / 'denied'
);

CREATE INDEX IF NOT EXISTS idx_gates_unresolved
    ON governance_gates_pending (resolved_at)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_gates_agent
    ON governance_gates_pending (agent_id, created_at DESC);
