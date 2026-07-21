"""Integration test: alembic upgrade head against a seeded real Postgres.

This test exists specifically to catch the class of bug that shipped in
the f6a1 pre-release: a migration that works against an empty test DB
but crashes against any DB with existing ``governance_audit_events``
rows because of the append-only row trigger.

Per ``feedback_live_test_before_release.md``: unit tests that only run
against an empty test DB are insufficient to catch migration
regressions. The only reliable detector is a seeded DB + real DDL + a
real ``alembic upgrade head``. This test is that detector.

Marked ``@pytest.mark.integration`` so it does not run in the default
``pytest`` invocation. Run explicitly with::

    pytest tests/integration/test_migration_against_seeded_db.py \
        -m integration -v

Requires a working Docker daemon on the test host. The test spins up
a throwaway ``postgres:16`` container on a dedicated port, seeds it
through the real SDK write path, runs the migrations, asserts the
invariants, and tears the container down in a ``finally`` block.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
# v0.6.2 relocated ``alembic.ini`` into the package (P1 wheel-packaging
# fix) so ``pip install`` users get the file under
# ``site-packages/codeatelier_governance/``. The repo-root copy is gone;
# integration tests resolve the file via the src-layout path instead.
ALEMBIC_INI = REPO_ROOT / "src" / "codeatelier_governance" / "alembic.ini"
# v0.5.x bootstrap DDL set — all files the SDK applies at install time
# BEFORE any Alembic migration runs. The base Alembic revision
# ``a1b2c3d4e5f6`` assumes these tables already exist (it adds columns
# to ``governance_gates_pending``).
DDL_MODULES = ("audit", "gates", "scope", "cost", "loop", "presence")
CONTAINER_NAME = f"governance-mig-test-{os.getpid()}"


def _free_port() -> int:
    """Ask the OS for an unused TCP port for the throwaway container."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _wait_for_postgres(port: int, timeout_s: float = 30.0) -> None:
    """Block until the Postgres container accepts connections."""
    import psycopg  # lazy import — only available under [migrations] extra

    deadline = time.time() + timeout_s
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with psycopg.connect(
                f"host=127.0.0.1 port={port} user=test password=test dbname=test",
                connect_timeout=2,
            ) as conn:
                conn.execute("SELECT 1")
                return
        except Exception as exc:  # noqa: BLE001 — polling loop
            last_err = exc
            time.sleep(0.5)
    raise RuntimeError(f"postgres did not become ready in {timeout_s}s: {last_err}")


def _start_container(port: int) -> None:
    subprocess.run(
        [
            "docker", "run", "--rm", "-d",
            "--name", CONTAINER_NAME,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_USER=test",
            "-e", "POSTGRES_DB=test",
            "-p", f"{port}:5432",
            "postgres:16",
        ],
        check=True,
        capture_output=True,
    )


def _stop_container() -> None:
    subprocess.run(
        ["docker", "rm", "-f", CONTAINER_NAME],
        check=False,
        capture_output=True,
    )


def _apply_bootstrap_ddl(port: int) -> None:
    """Apply the full v0.5.x bootstrap DDL — what an existing deployment has.

    The SDK ships a ``ddl.sql`` per module; the installer applies all
    of them before the first Alembic run. We replicate that here so
    the migration sees a realistic pre-v0.6 schema (gates, scope,
    cost, etc.), not just the audit table in isolation. The base
    revision ``a1b2c3d4e5f6`` adds columns to ``governance_gates_pending``
    and fails if the gates DDL has not been applied.
    """
    import psycopg

    with psycopg.connect(
        f"host=127.0.0.1 port={port} user=test password=test dbname=test",
        autocommit=True,
    ) as conn:
        for mod in DDL_MODULES:
            path = REPO_ROOT / "src" / "codeatelier_governance" / mod / "ddl.sql"
            if not path.is_file():
                continue
            conn.execute(path.read_text())


def _seed_audit_rows_legacy(port: int, count: int) -> None:
    """Write ``count`` v0.5.x-shaped rows via raw INSERT.

    We intentionally do NOT use the current ``AuditModule.log()`` write
    path here: the runtime SQLAlchemy table at HEAD already carries the
    v0.6 columns (``signature``, ``signing_key_fingerprint``,
    ``signature_status``) and would fail to insert against the
    pre-migration schema. That mismatch is exactly the upgrade
    scenario we are modelling: a customer who wrote rows with v0.5.x
    and is now running ``alembic upgrade head`` under v0.6.

    Raw INSERT with a valid 64-char hex HMAC and the minimal column
    set is the authentic v0.5.x shape — it is what the v0.5.x SDK
    would have produced. The trigger still fires on UPDATE/DELETE,
    which is the defense we are exercising against in the migration.
    """
    import psycopg

    session_id = str(uuid.uuid4())
    prev_hash: str | None = None
    with psycopg.connect(
        f"host=127.0.0.1 port={port} user=test password=test dbname=test",
        autocommit=True,
    ) as conn:
        for i in range(count):
            event_id = str(uuid.uuid4())
            # Deterministic dummy HMAC — test fixture, not real auth.
            hmac_value = f"{i:064x}"
            conn.execute(
                """
                INSERT INTO governance_audit_events
                    (event_id, session_id, agent_id, kind, metadata_json,
                     prev_hash, hmac_value, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, NOW())
                """,
                (
                    event_id,
                    session_id,
                    "integration.seed",
                    "integration.seed",
                    "{}",
                    prev_hash,
                    hmac_value,
                ),
            )
            prev_hash = hmac_value


def _run_alembic_upgrade(url: str) -> None:
    """Run ``alembic upgrade head`` programmatically against ``url``."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def _assert_post_migration(port: int) -> None:
    import psycopg

    with psycopg.connect(
        f"host=127.0.0.1 port={port} user=test password=test dbname=test",
        autocommit=True,
    ) as conn:
        # All 4 new tables exist
        for table in (
            "governance_agent_keys",
            "governance_agent_key_revocations",
            "governance_audit_chain_keys",
            "governance_wrapper_registrations",
        ):
            row = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
                (table,),
            ).fetchone()
            assert row is not None, f"missing table: {table}"

        # 3 new columns on governance_audit_events
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'governance_audit_events'"
            ).fetchall()
        }
        for col in ("signature", "signing_key_fingerprint", "signature_status"):
            assert col in cols, f"missing column: {col}"

        # Existing rows keep legacy_unsigned
        legacy = conn.execute(
            "SELECT COUNT(*) FROM governance_audit_events "
            "WHERE signature_status = 'legacy_unsigned'"
        ).fetchone()
        assert legacy is not None and legacy[0] == 10, (
            f"expected 10 legacy_unsigned rows, got {legacy}"
        )

        # UPDATE must still be blocked post-migration (trigger + revoke)
        failed = False
        try:
            conn.execute(
                "UPDATE governance_audit_events SET agent_id = 'x' WHERE TRUE"
            )
        except Exception:
            failed = True
        assert failed, "UPDATE on governance_audit_events should be blocked"


async def _assert_new_row_defaults_unsigned(url: str) -> None:
    """Post-migration writes should default ``signature_status='unsigned'``."""
    import psycopg

    from codeatelier_governance.audit.models import AuditEvent
    from codeatelier_governance.audit.module import AuditModule
    from codeatelier_governance.audit.postgres_store import PostgresAuditStore

    store = PostgresAuditStore(database_url=url)
    module = AuditModule(store=store, secret=bytes(range(32)))
    await module.start()
    try:
        await module.log(
            AuditEvent(
                session_id=uuid.uuid4(),
                agent_id="integration.post",
                kind="integration.post",
                metadata={},
            )
        )
    finally:
        await module.close()

    # Strip the +asyncpg for psycopg verification
    sync_url = url.replace("postgresql+asyncpg://", "postgresql://")
    # psycopg accepts postgresql:// natively
    with psycopg.connect(sync_url) as conn:
        row = conn.execute(
            "SELECT signature_status FROM governance_audit_events "
            "WHERE agent_id = 'integration.post' LIMIT 1"
        ).fetchone()
        assert row is not None, "post-migration row not written"
        assert row[0] == "unsigned", f"expected 'unsigned', got {row[0]!r}"


def test_alembic_upgrade_head_against_seeded_db() -> None:
    if not _docker_available():
        pytest.skip("docker not available on this host")

    port = _free_port()
    try:
        _start_container(port)
        _wait_for_postgres(port)
        _apply_bootstrap_ddl(port)

        async_url = (
            f"postgresql+asyncpg://test:test@127.0.0.1:{port}/test"
        )
        _seed_audit_rows_legacy(port, count=10)

        # Sanity: confirm seed writes landed. AuditModule.log() catches
        # storage errors and returns a placeholder record, so a broken
        # bootstrap would silently drop rows and the post-migration
        # legacy_unsigned count would be 0 — failing the wrong assertion
        # later. Fail loudly here instead.
        import psycopg

        with psycopg.connect(
            f"host=127.0.0.1 port={port} user=test password=test dbname=test"
        ) as conn:
            seeded = conn.execute(
                "SELECT COUNT(*) FROM governance_audit_events"
            ).fetchone()
        assert seeded is not None and seeded[0] == 10, (
            f"seed phase failed: expected 10 rows, got {seeded}. "
            "AuditModule.log() likely degraded silently — check bootstrap DDL."
        )

        _run_alembic_upgrade(async_url)

        _assert_post_migration(port)
        asyncio.run(_assert_new_row_defaults_unsigned(async_url))
    finally:
        _stop_container()
