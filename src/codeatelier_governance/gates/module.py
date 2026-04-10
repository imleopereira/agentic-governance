"""Human-in-the-loop approval gates exposed via ``sdk.gates``.

Public API:
    req = await sdk.gates.request(kind, agent_id, payload)
    await sdk.gates.grant(token)
    await sdk.gates.deny(token)
    granted = await sdk.gates.wait_for(request_id, timeout=300)

    @sdk.gates.require_approval(kind="patient.delete", agent_id="x", timeout=600)
    async def delete_patient(...): ...

Threat model handled:
    * Forged tokens: HMAC signature verification.
    * Replay: tokens are single-use; resolved set tracks consumed request_ids.
    * Tampering with action_hash: hmac binds request_id+action_hash+expires_at.
    * Race between two grants: per-module asyncio.Lock serializes resolution.
    * Self-approval: tokens have to come from outside the agent's process —
      v0.1 documents this as an operator responsibility (no built-in identity
      gate; pluggable in v0.2).
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, ParamSpec, TypeVar
from uuid import UUID, uuid4

from ..audit.models import AuditEvent
from ..audit.module import AuditModule
from .errors import (
    ApprovalDenied,
    ApprovalPending,
    ApprovalTimeout,
    ApprovalTokenError,
)
from .models import ApprovalRequest
from .tokens import make_token, parse_token

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_EXPIRES_IN = timedelta(hours=1)
MIN_GATES_SECRET_BYTES = 32
MAX_PAYLOAD_BYTES = 64 * 1024


def _hash_action_payload(value: Any) -> str:
    """Stable SHA-256 hash of the action payload, capped at 64 KiB."""
    try:
        serialized = json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError):
        serialized = repr(value)
    data = serialized.encode("utf-8")[:MAX_PAYLOAD_BYTES]
    return hashlib.sha256(data).hexdigest()


class GatesModule:
    """Human-in-the-loop approval gates."""

    def __init__(
        self,
        audit: AuditModule,
        *,
        secret: bytes,
        default_expires_in: timedelta = DEFAULT_EXPIRES_IN,
    ) -> None:
        if len(secret) < MIN_GATES_SECRET_BYTES:
            raise ValueError(
                f"gates secret must be at least {MIN_GATES_SECRET_BYTES} bytes; "
                f"use secrets.token_bytes(32)."
            )
        self._audit = audit
        self._secret = secret
        self._default_expires_in = default_expires_in
        self._pending: dict[UUID, ApprovalRequest] = {}
        self._resolved: set[UUID] = set()
        self._outcomes: dict[UUID, bool] = {}
        self._events: dict[UUID, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def request(
        self,
        kind: str,
        agent_id: str,
        payload: Any | None = None,
        *,
        expires_in: timedelta | None = None,
    ) -> ApprovalRequest:
        """Open an approval request and return the pending object.

        The caller is responsible for surfacing the request to a human (UI,
        Slack, email, etc.). The human's tool calls ``grant(token)`` or
        ``deny(token)`` with the token field of the returned request.
        """
        request_id = uuid4()
        action_hash = _hash_action_payload(payload)
        expires_at = datetime.now(timezone.utc) + (
            expires_in or self._default_expires_in
        )
        token = make_token(
            secret=self._secret,
            request_id=request_id,
            action_hash=action_hash,
            expires_at=expires_at,
        )
        req = ApprovalRequest(
            request_id=request_id,
            agent_id=agent_id,
            kind=kind,
            action_hash=action_hash,
            expires_at=expires_at,
            payload=payload if isinstance(payload, dict) else {},
            token=token,
        )
        async with self._lock:
            self._pending[request_id] = req
            self._events[request_id] = asyncio.Event()
        await self._audit.log(
            AuditEvent(
                agent_id=agent_id,
                kind="approval.requested",
                metadata={
                    "request_id": str(request_id),
                    "approval_kind": kind,
                    "action_hash": action_hash,
                },
            )
        )
        return req

    async def grant(self, token: str) -> None:
        """Approve the request bound to ``token``. Single-use, time-bound."""
        await self._resolve(token, granted=True)

    async def deny(self, token: str) -> None:
        """Deny the request bound to ``token``. Single-use, time-bound."""
        await self._resolve(token, granted=False)

    async def _resolve(self, token: str, *, granted: bool) -> None:
        request_id, action_hash, _expires = parse_token(
            secret=self._secret, token=token
        )
        async with self._lock:
            if request_id in self._resolved:
                raise ApprovalTokenError(
                    "approval token: already used (single-use only)"
                )
            req = self._pending.get(request_id)
            if req is None:
                raise ApprovalTokenError(
                    "approval token: unknown request_id"
                )
            if req.action_hash != action_hash:
                raise ApprovalTokenError(
                    "approval token: action_hash mismatch"
                )
            self._resolved.add(request_id)
            self._outcomes[request_id] = granted
            event = self._events.get(request_id)
            self._pending.pop(request_id, None)
        if event is not None:
            event.set()
        await self._audit.log(
            AuditEvent(
                agent_id=req.agent_id,
                kind="approval.granted" if granted else "approval.denied",
                metadata={
                    "request_id": str(request_id),
                    "approval_kind": req.kind,
                },
            )
        )

    async def wait_for(self, request_id: UUID, timeout: float) -> bool:
        """Block until the request is resolved. Returns True if granted.

        Raises :class:`ApprovalTimeout` if the timeout elapses without
        resolution. Raises :class:`ApprovalDenied` if the request was denied.
        """
        async with self._lock:
            event = self._events.get(request_id)
        if event is None:
            raise ApprovalTokenError(
                f"wait_for: unknown request_id={request_id}"
            )
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ApprovalTimeout(
                f"approval timeout after {timeout}s for request {request_id}"
            ) from exc
        granted = self._outcomes.get(request_id, False)
        if not granted:
            raise ApprovalDenied(
                f"approval denied for request {request_id}"
            )
        return True

    def require_approval(
        self,
        *,
        kind: str,
        agent_id: str,
        timeout: float = 600.0,
        blocking: bool = True,
    ) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
        """Decorator: open an approval request before running the function.

        If ``blocking=True`` (default), the wrapped call blocks up to
        ``timeout`` seconds waiting for grant/deny.
        If ``blocking=False``, the wrapped call raises :class:`ApprovalPending`
        immediately and the caller resumes by calling the function again
        after a human has resolved the request.
        """

        def decorator(
            func: Callable[P, Awaitable[R]],
        ) -> Callable[P, Awaitable[R]]:
            if not inspect.iscoroutinefunction(func):
                raise TypeError(
                    f"@gates.require_approval requires an async function; "
                    f"{func.__name__} is sync."
                )

            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                payload = {"args": list(args), "kwargs": dict(kwargs)}
                req = await self.request(
                    kind=kind, agent_id=agent_id, payload=payload
                )
                if not blocking:
                    raise ApprovalPending(str(req.request_id))
                await self.wait_for(req.request_id, timeout=timeout)
                return await func(*args, **kwargs)

            return wrapper

        return decorator
