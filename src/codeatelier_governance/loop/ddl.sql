-- Loop / anomaly detection tracking table.
--
-- Records every tool call per session for loop detection. Old rows are
-- opportunistically cleaned up (probability 1/100 on each record_call).

CREATE TABLE IF NOT EXISTS governance_loop_tracking (
    id          BIGSERIAL        PRIMARY KEY,
    session_id  UUID             NOT NULL,
    agent_id    VARCHAR(256)     NOT NULL,
    tool_name   VARCHAR(256)     NOT NULL,
    called_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_loop_tracking_session_tool
    ON governance_loop_tracking (session_id, tool_name, called_at);
