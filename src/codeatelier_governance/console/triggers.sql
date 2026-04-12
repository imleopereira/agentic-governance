-- LISTEN/NOTIFY triggers for real-time SSE delivery.
--
-- Three Postgres channels:
--   governance_events   -- fired on every audit event INSERT
--   governance_gates    -- fired on gate INSERT or UPDATE (resolution, claim)
--   governance_presence -- fired on presence INSERT or UPDATE
--
-- PAYLOAD SIZE GUARD (required by CTO and Security review):
--   pg_notify() hard-limits payloads to 8 KB (Postgres source: async.c).
--   Each trigger measures the built JSON with octet_length() before calling
--   pg_notify. If the payload exceeds 7 KB (1 KB headroom for the channel
--   name and Postgres framing overhead), a minimal fallback summary is sent.
--   A malicious or misconfigured agent_id cannot cause NOTIFY failures that
--   would silently break SSE delivery for all connected clients.
--
-- APPEND-ONLY INVARIANT:
--   The audit trigger fires AFTER INSERT only -- it does NOT fire on UPDATE
--   or DELETE and does NOT modify the row (RETURN NEW is a pass-through).
--   Gate / presence triggers fire AFTER INSERT OR UPDATE, which is correct
--   since gates are resolved via UPDATE (resolved_at, resolution) and
--   presence rows are upserted on heartbeat.

-- ---------------------------------------------------------------------------
-- 1. Audit events: INSERT only (append-only invariant preserved)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION notify_governance_event()
RETURNS trigger AS $$
DECLARE
  payload TEXT;
BEGIN
  -- Summary fields only.  HMAC, prev_hash, and full metadata are intentionally
  -- excluded: they are sensitive and not required for SSE-side routing.
  payload := json_build_object(
    'event_id',   NEW.event_id,
    'agent_id',   NEW.agent_id,
    'session_id', NEW.session_id,
    'kind',       NEW.kind,
    'chain_seq',  NEW.chain_seq,
    'created_at', NEW.created_at
  )::text;

  -- 8 KB guard: fall back to a minimal summary to prevent NOTIFY failure.
  IF octet_length(payload) > 7168 THEN
    payload := json_build_object(
      'event_id',  NEW.event_id,
      'kind',      NEW.kind,
      'chain_seq', NEW.chain_seq,
      'truncated', true
    )::text;
  END IF;

  PERFORM pg_notify('governance_events', payload);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_event_notify ON governance_audit_events;
CREATE TRIGGER audit_event_notify
  AFTER INSERT ON governance_audit_events
  FOR EACH ROW EXECUTE FUNCTION notify_governance_event();

-- ---------------------------------------------------------------------------
-- 2. Gate changes: INSERT or UPDATE
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION notify_gate_change()
RETURNS trigger AS $$
DECLARE
  payload TEXT;
BEGIN
  payload := json_build_object(
    'request_id',      NEW.request_id,
    'agent_id',        NEW.agent_id,
    'kind',            NEW.kind,
    'resolved_at',     NEW.resolved_at,
    'resolution',      NEW.resolution,
    'reviewer_id',     NEW.reviewer_id,
    'reviewing_since', NEW.reviewing_since
  )::text;

  -- 8 KB guard
  IF octet_length(payload) > 7168 THEN
    payload := json_build_object(
      'request_id',  NEW.request_id,
      'kind',        NEW.kind,
      'resolved_at', NEW.resolved_at,
      'truncated',   true
    )::text;
  END IF;

  PERFORM pg_notify('governance_gates', payload);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS gate_change_notify ON governance_gates_pending;
CREATE TRIGGER gate_change_notify
  AFTER INSERT OR UPDATE ON governance_gates_pending
  FOR EACH ROW EXECUTE FUNCTION notify_gate_change();

-- ---------------------------------------------------------------------------
-- 3. Agent presence: INSERT or UPDATE
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION notify_presence_change()
RETURNS trigger AS $$
DECLARE
  payload TEXT;
BEGIN
  payload := json_build_object(
    'agent_id',       NEW.agent_id,
    'status',         NEW.status,
    'last_heartbeat', NEW.last_heartbeat
  )::text;

  -- 8 KB guard
  IF octet_length(payload) > 7168 THEN
    payload := json_build_object(
      'agent_id',  NEW.agent_id,
      'status',    NEW.status,
      'truncated', true
    )::text;
  END IF;

  PERFORM pg_notify('governance_presence', payload);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS presence_change_notify ON governance_agent_presence;
CREATE TRIGGER presence_change_notify
  AFTER INSERT OR UPDATE ON governance_agent_presence
  FOR EACH ROW EXECUTE FUNCTION notify_presence_change();
