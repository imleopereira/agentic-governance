"""Session and parent-event context propagated via contextvars.

contextvars travel correctly across asyncio tasks, so the @track decorator
can set a parent_event_id for the duration of a wrapped function and any
nested audit calls inside that function will pick it up automatically.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator
from uuid import UUID, uuid4

_current_session: ContextVar[UUID | None] = ContextVar(
    "audit_session_id", default=None
)
_current_parent: ContextVar[UUID | None] = ContextVar(
    "audit_parent_event_id", default=None
)


def current_session() -> UUID | None:
    """Return the session id active in the current context, or None."""
    return _current_session.get()


def current_parent() -> UUID | None:
    """Return the parent event id active in the current context, or None."""
    return _current_parent.get()


@contextmanager
def session(session_id: UUID | None = None) -> Iterator[UUID]:
    """Scope a block of code to an audit session.

    Usage:
        with sdk.audit.session() as sid:
            await sdk.audit.log(AuditEvent(agent_id="x", kind="y"))
    """
    sid = session_id or uuid4()
    token = _current_session.set(sid)
    try:
        yield sid
    finally:
        _current_session.reset(token)


@contextmanager
def parent_event(event_id: UUID) -> Iterator[None]:
    """Set the current parent event id for nested events."""
    token = _current_parent.set(event_id)
    try:
        yield
    finally:
        _current_parent.reset(token)
