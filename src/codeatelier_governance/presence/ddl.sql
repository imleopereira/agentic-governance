-- Agent presence tracking table.
--
-- Single row per agent, upserted on heartbeat. Stale agents are
-- marked 'unresponsive' by check_stale().

CREATE TABLE IF NOT EXISTS governance_agent_presence (
    agent_id        VARCHAR(256)  PRIMARY KEY,
    status          VARCHAR(16)   NOT NULL DEFAULT 'live'
                        CHECK (status IN ('live', 'idle', 'unresponsive')),
    last_heartbeat  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    metadata_json   JSONB         DEFAULT '{}'::jsonb,
    operator_id     VARCHAR(256)  DEFAULT NULL
);
