-- governance_policies: read-copy of scope and budget policies for the console.
--
-- The SDK upserts here on register(); the console reads from here.
-- Enforcement still uses the in-memory dict — this table is NOT the
-- enforcement source of truth.

CREATE TABLE IF NOT EXISTS governance_policies (
    agent_id      VARCHAR(256) NOT NULL,
    policy_type   VARCHAR(32)  NOT NULL,  -- 'scope' or 'budget'
    policy_json   JSONB        NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (agent_id, policy_type)
);
