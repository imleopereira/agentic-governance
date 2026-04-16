-- Video 1 seed teardown. Drops every table the seed populated so the
-- next take starts from an empty database. Run via:
--
--   psql postgresql://governance:governance@localhost:5435/gov_video1_seed \
--        < scripts/promo/video1_article12/teardown.sql
--
-- The next seed.py run will re-run DDL + alembic to recreate everything.

DROP TABLE IF EXISTS governance_audit_events CASCADE;
DROP TABLE IF EXISTS governance_audit_chain_keys CASCADE;
DROP TABLE IF EXISTS governance_agent_keys CASCADE;
DROP TABLE IF EXISTS governance_agent_key_revocations CASCADE;
DROP TABLE IF EXISTS governance_agent_presence CASCADE;
DROP TABLE IF EXISTS governance_policies CASCADE;
DROP TABLE IF EXISTS governance_gates_pending CASCADE;
DROP TABLE IF EXISTS governance_cost_session_usage CASCADE;
DROP TABLE IF EXISTS governance_cost_agent_daily CASCADE;
DROP TABLE IF EXISTS governance_cost_model_daily CASCADE;
DROP TABLE IF EXISTS governance_wrapper_registrations CASCADE;
DROP TABLE IF EXISTS governance_loop_tracking CASCADE;
DROP TABLE IF EXISTS governance_console_users CASCADE;
DROP TABLE IF EXISTS governance_console_sessions CASCADE;
DROP TABLE IF EXISTS alembic_version CASCADE;

DROP FUNCTION IF EXISTS governance_audit_append_only() CASCADE;
