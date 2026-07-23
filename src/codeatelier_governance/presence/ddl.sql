-- Agent presence tracking table.
--
-- Single row per agent, upserted on heartbeat. Stale agents are
-- marked 'unresponsive' by check_stale().
--
-- SECURITY (self-unhalt defense): the operator halt marker lives in the
-- dedicated columns halted_by / halted_at / halt_reason, NOT in
-- metadata_json. The agent's own heartbeat UPSERT rewrites
-- metadata_json wholesale on every beat, so a marker stored there could
-- be erased by the very agent it is meant to stop (self-unhalt). The
-- dedicated columns are never listed in the heartbeat write path, and
-- the column-level REVOKE below makes that invariant a DB-enforced
-- guarantee rather than a code convention.

CREATE TABLE IF NOT EXISTS governance_agent_presence (
    agent_id        VARCHAR(256)  PRIMARY KEY,
    status          VARCHAR(16)   NOT NULL DEFAULT 'live'
                        CHECK (status IN ('live', 'idle', 'unresponsive')),
    last_heartbeat  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    metadata_json   JSONB         DEFAULT '{}'::jsonb,
    operator_id     VARCHAR(256)  DEFAULT NULL,
    -- Operator halt marker. Written only by the privileged console/halt
    -- role; deliberately outside metadata_json so the agent heartbeat
    -- cannot erase its own halt. status stays ('live','idle',
    -- 'unresponsive') — halt is tracked here, not in status.
    halted_by       VARCHAR(256)  DEFAULT NULL,
    halted_at       TIMESTAMPTZ   DEFAULT NULL,
    halt_reason     VARCHAR(2000) DEFAULT NULL
);

-- Column-level grant lock-down for the self-unhalt invariant.
--
-- PostgreSQL grants tables from PUBLIC by default. We revoke table-wide
-- UPDATE and re-grant it only on the four columns the heartbeat UPSERT
-- actually sets (status, last_heartbeat, metadata_json, operator_id).
-- The halt-marker columns (halted_by, halted_at, halt_reason) are left
-- unwritable by PUBLIC, so an agent running under the app role can never
-- clear its own halt — the write is rejected at the grant level.
--
-- SELF-UNHALT DEFENSE — TWO DEPLOYMENT MODELS. The invariant is enforced by
-- two independent mechanisms: this column REVOKE (an agent with raw SQL
-- cannot UPDATE halted_by=NULL) and the BEFORE DELETE trigger below (a
-- halted row cannot be deleted-then-reinserted).
--
--   1. SINGLE-ROLE (the agent cannot issue arbitrary SQL; it only calls SDK
--      methods): the REVOKE below is not required for safety in this model. No
--      SDK method clears a halt and the DELETE trigger blocks
--      close-then-reinsert, so the halt is safe without it. NOTE: the shipped
--      DDL is hardened-by-default and applies the REVOKE unconditionally, so a
--      single-role deployment that runs this DDL as a NON-owner role gets every
--      live-agent halt() DENIED (HaltPersistenceError) on the shared engine.
--      For halt() to succeed on the shared engine in a single-role deployment
--      you must do ONE of:
--        (a) remove the REVOKE/GRANT lines below before running this DDL; OR
--        (b) run under the table OWNER role (the owner keeps UPDATE regardless
--            of the PUBLIC REVOKE); OR
--        (c) configure a privileged halt DSN (model 2 below) that retains
--            UPDATE on the halt columns.
--   2. HARDENED / MULTI-ROLE (the agent CAN issue raw SQL, e.g. a SQL tool or
--      a compromised agent): apply this REVOKE to the AGENT role AND give the
--      SDK a PRIVILEGED halt connection — GovernanceConfig
--      presence_halt_database_url / GOVERNANCE_HALT_DATABASE_URL, used by
--      PresenceModule ONLY for the halt write. The agent role then cannot
--      self-unhalt; the privileged role is the sole writer of the marker.
--      Deployments using an explicit application role with direct grants must
--      ALSO REVOKE UPDATE on the halt columns from that role (same runbook
--      caveat as the audit-events REVOKE in migration 978884c6b7f1).
REVOKE UPDATE ON governance_agent_presence FROM PUBLIC;
GRANT UPDATE (status, last_heartbeat, metadata_json, operator_id)
    ON governance_agent_presence TO PUBLIC;

-- SECURITY (self-unhalt defense, part 2): a halted agent's row must not be
-- DELETE-able. Unlike the heartbeat UPDATE, DELETE is table-level and cannot
-- be revoked per column, so close_agent could otherwise delete the whole row
-- (halt marker included) and a re-INSERT would recreate it unhalted. This
-- trigger refuses to delete a row while it carries an operator halt marker;
-- an operator must clear the halt first (a privileged UPDATE the app role
-- cannot make). Non-halted rows delete normally, so close_agent still works.
CREATE OR REPLACE FUNCTION governance_presence_no_delete_while_halted()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.halted_by IS NOT NULL THEN
        RAISE EXCEPTION 'governance_agent_presence: cannot delete a halted agent (halted_by=%); clear the halt first', OLD.halted_by;
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_presence_no_delete_halted ON governance_agent_presence;
CREATE TRIGGER trg_presence_no_delete_halted
    BEFORE DELETE ON governance_agent_presence
    FOR EACH ROW
    EXECUTE FUNCTION governance_presence_no_delete_while_halted();
