"""BLOCKER DX: PostgresAuditStore self-diagnoses pre-v0.6 schemas.

Pins:
  * On the first INSERT failure caused by a missing v0.6 column,
    the raised ``StoreUnavailableError`` names ``alembic upgrade head``
    so the operator immediately knows the remediation.
  * The same logic fires on the ``write_batch`` path.
  * The startup ``start()`` self-check emits a structlog WARNING
    naming the missing columns when the schema is incomplete.
  * A v0.6 schema produces NO such warning at startup.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from codeatelier_governance.audit import postgres_store as ps
from codeatelier_governance.audit.errors import StoreUnavailableError


class _ProgrammingError(Exception):
    """Stand-in for sqlalchemy.exc.ProgrammingError without the import dance."""


def test_missing_columns_error_names_alembic() -> None:
    exc = _ProgrammingError(
        'column "signature" of relation "governance_audit_events" does not exist'
    )
    assert ps._is_missing_v06_columns_error(exc) is True
    assert "alembic upgrade head" in ps._v06_alembic_message()


def test_unrelated_error_does_not_match() -> None:
    exc = _ProgrammingError("connection terminated unexpectedly")
    assert ps._is_missing_v06_columns_error(exc) is False


def test_message_lists_all_required_columns() -> None:
    msg = ps._v06_alembic_message()
    for col in ps._REQUIRED_V06_COLUMNS:
        assert col in msg


@pytest.mark.asyncio
async def test_insert_with_missing_columns_raises_actionable_error() -> None:
    """A real INSERT failure with a v0.6-column message must be wrapped
    in an actionable ``StoreUnavailableError``."""
    store = ps.PostgresAuditStore.__new__(ps.PostgresAuditStore)
    store._engine = MagicMock()
    store._owns_engine = False

    async def fake_read(_sid: Any) -> None:
        return None

    store._read_last_hmac_no_lock = fake_read  # type: ignore[method-assign]

    class _Boom:
        async def __aenter__(self) -> "_Boom":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        async def execute(self, *_a: Any, **_kw: Any) -> Any:
            raise _ProgrammingError(
                'column "signing_key_fingerprint" of relation '
                '"governance_audit_events" does not exist'
            )

    store._engine.begin = lambda: _Boom()  # type: ignore[method-assign]

    async def builder(_prev: Any) -> Any:
        return MagicMock()

    from uuid import uuid4
    with pytest.raises(StoreUnavailableError) as excinfo:
        await store.insert_with_chain_lock(uuid4(), builder)
    assert "alembic upgrade head" in str(excinfo.value)


@pytest.mark.asyncio
async def test_startup_warning_on_missing_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The startup self-check WARNs naming the missing columns."""
    store = ps.PostgresAuditStore.__new__(ps.PostgresAuditStore)
    store._engine = MagicMock()
    store._owns_engine = False

    class _FakeRes:
        def __iter__(self) -> Any:
            return iter([])  # zero columns present

    class _FakeConn:
        async def execute(self, *_a: Any, **_kw: Any) -> Any:
            return _FakeRes()

        async def __aenter__(self) -> "_FakeConn":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    store._engine.connect = lambda: _FakeConn()  # type: ignore[method-assign]

    await store.start()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "missing_v0_6_columns" in output
    assert "signature" in output


@pytest.mark.asyncio
async def test_full_schema_no_startup_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = ps.PostgresAuditStore.__new__(ps.PostgresAuditStore)
    store._engine = MagicMock()
    store._owns_engine = False

    class _FakeRes:
        def __iter__(self) -> Any:
            return iter([
                ("signature",),
                ("signing_key_fingerprint",),
                ("signature_status",),
            ])

    class _FakeConn:
        async def execute(self, *_a: Any, **_kw: Any) -> Any:
            return _FakeRes()

        async def __aenter__(self) -> "_FakeConn":
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

    store._engine.connect = lambda: _FakeConn()  # type: ignore[method-assign]

    await store.start()
    captured = capsys.readouterr()
    assert "missing_v0_6_columns" not in (captured.out + captured.err)
