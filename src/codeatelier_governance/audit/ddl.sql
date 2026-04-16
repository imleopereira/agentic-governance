-- governance_audit_events: append-only, tamper-evident audit log
--
-- Apply this DDL once at SDK install time. The append-only triggers below are
-- the database-level enforcement of the same invariant the application layer
-- guarantees via HMAC chains and frozen Pydantic records. Defense in depth.
--
-- Operators MUST also revoke UPDATE/DELETE/TRUNCATE on this table from the
-- application role. See the REVOKE comment at the bottom.

CREATE TABLE IF NOT EXISTS governance_audit_events (
    -- chain_seq is the authoritative insertion-order key. Reading the
    -- "latest event in this session" relies on this column, NOT created_at,
    -- because created_at is set in the application and is not monotonic
    -- with commit order across concurrent worker processes.
    chain_seq       BIGSERIAL    NOT NULL,
    event_id        UUID         PRIMARY KEY,
    session_id      UUID         NOT NULL,
    agent_id        VARCHAR(256) NOT NULL,
    parent_event_id UUID         NULL,
    kind            VARCHAR(128) NOT NULL,
    input_hash      VARCHAR(128) NULL,
    output_hash     VARCHAR(128) NULL,
    metadata_json   JSONB        NOT NULL DEFAULT '{}'::jsonb,
    prev_hash       VARCHAR(128) NULL,
    hmac_value      VARCHAR(128) NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL,
    -- v0.6 Ed25519 signing columns. Mirrors alembic migration
    -- f6a1_ed25519_agent_identity.py so that fresh installs via `cga migrate`
    -- (which runs this DDL only, NOT alembic) produce the same schema as a
    -- migrated v0.5 -> v0.6 database. Fresh rows default to 'unsigned'; the
    -- 'legacy_unsigned' value is reserved for rows inserted before v0.6 and
    -- backfilled by the alembic migration.
    signature               BYTEA NULL,
    signing_key_fingerprint TEXT  NULL,
    signature_status        TEXT  NOT NULL DEFAULT 'unsigned'
);

CREATE INDEX IF NOT EXISTS idx_audit_session     ON governance_audit_events (session_id);
CREATE INDEX IF NOT EXISTS idx_audit_session_seq ON governance_audit_events (session_id, chain_seq DESC);
CREATE INDEX IF NOT EXISTS idx_audit_agent       ON governance_audit_events (agent_id);
CREATE INDEX IF NOT EXISTS idx_audit_parent      ON governance_audit_events (parent_event_id);
CREATE INDEX IF NOT EXISTS idx_audit_created     ON governance_audit_events (created_at);

-- Append-only enforcement: block UPDATE and DELETE at the database level.
CREATE OR REPLACE FUNCTION governance_audit_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'governance_audit_events is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_no_update ON governance_audit_events;
CREATE TRIGGER trg_audit_no_update
    BEFORE UPDATE ON governance_audit_events
    FOR EACH ROW
    EXECUTE FUNCTION governance_audit_append_only();

DROP TRIGGER IF EXISTS trg_audit_no_delete ON governance_audit_events;
CREATE TRIGGER trg_audit_no_delete
    BEFORE DELETE ON governance_audit_events
    FOR EACH ROW
    EXECUTE FUNCTION governance_audit_append_only();

-- Model field: LLM model name associated with the event (v0.2+).
ALTER TABLE governance_audit_events ADD COLUMN IF NOT EXISTS model VARCHAR(128) NULL;

-- Defense-in-depth: revoke mutation privileges from the SDK role.
-- Replace `governance_sdk_role` with the actual role you connect as.
--
--   REVOKE UPDATE, DELETE, TRUNCATE ON governance_audit_events FROM governance_sdk_role;
--   GRANT  INSERT, SELECT          ON governance_audit_events TO   governance_sdk_role;
