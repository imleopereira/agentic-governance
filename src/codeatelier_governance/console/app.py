"""FastAPI application for the Governance Console.

Read-only API server that exposes the audit chain, cost counters, gate
states, and governance posture over HTTP. The frontend (Next.js) consumes
this API.

All verification and business logic delegates to the existing SDK modules.
The console adds zero new tables and zero new storage — it reads the same
Postgres the SDK already writes to.

Security constraints:
    * The HMAC secret is used server-side ONLY for chain verification.
      It is NEVER serialized to the frontend.
    * Field-level redaction of metadata is applied before serving.
    * CORS is restricted to the console's own origin.
    * Session-based auth with httpOnly cookies (v0.4.0+).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models.responses import ComplianceReportView, VerifyChainResponse
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import structlog

try:
    from fastapi import Depends, FastAPI, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
except ImportError as exc:
    raise ImportError(
        "The governance console requires FastAPI. "
        "Install with: pip install codeatelier-governance[console]"
    ) from exc

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..audit.chain import canonical_json, verify_event
from ..audit.models import AuditEvent, AuditEventRecord
from ..audit.module import AuditModule
from ..gates.errors import ApprovalTokenError
from ..gates.tokens import parse_token
from .auth import create_session_id, hash_password, session_expires_at, verify_password
from .models.responses import (
    AgentPoliciesResponse,
    AgentPresenceResponse,
    AgentPresenceRow,
    BatchApproveFailure,
    BatchApproveResponse,
    EventStatsResponse,
    GateAgentPresence,
    GateClaimResponse,
    GateContextResponse,
    GateEscalateResponse,
    GateRecentEvent,
    PolicyListResponse,
    PolicyRow,
    SessionRevokeResponse,
)
from .redaction import redact_secrets

_logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------
DATABASE_URL = os.environ.get("GOVERNANCE_DATABASE_URL", "")
AUDIT_SECRET = os.environ.get("GOVERNANCE_AUDIT_SECRET", "")
CONSOLE_TOKEN = os.environ.get("GOVERNANCE_CONSOLE_TOKEN", "")
DEV_MODE = os.environ.get("GOVERNANCE_CONSOLE_DEV_MODE", "").lower() == "true"
# BLOCKER C4: explicit acknowledgement env var. Without this set,
# DEV_MODE refuses to start the app (dev mode silently grants admin to
# every caller — a leaked env var would otherwise mean unauthenticated
# admin in production).
DEV_MODE_ACK = os.environ.get("GOVERNANCE_CONSOLE_ALLOW_DEV_MODE", "") == "1"
# BLOCKER C4: bind address read from env so the startup guard can refuse
# DEV_MODE on a non-localhost bind. Defaults to 127.0.0.1 to match the
# console __main__ default.
CONSOLE_HOST = os.environ.get("GOVERNANCE_CONSOLE_HOST", "127.0.0.1")
SESSION_TTL_HOURS = int(os.environ.get("GOVERNANCE_CONSOLE_SESSION_TTL_HOURS", "8"))
CORS_ORIGINS = os.environ.get(
    "GOVERNANCE_CONSOLE_CORS_ORIGINS", "http://localhost:3000"
).split(",")

# Metadata fields to ALWAYS redact before serving to the frontend.
# Operators can extend this list via GOVERNANCE_CONSOLE_REDACT_KEYS.
REDACT_KEYS: set[str] = set(
    os.environ.get("GOVERNANCE_CONSOLE_REDACT_KEYS", "").split(",")
) | {"password", "secret", "token", "api_key", "authorization"}


# ---------------------------------------------------------------------------
# Rate limiter: in-memory, per-IP, for /api/auth/login only
# ---------------------------------------------------------------------------
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 60
_login_attempts: dict[str, list[float]] = {}


def _check_rate_limit(ip: str) -> float | None:
    """Return seconds until retry if rate-limited, else None."""
    now = time.monotonic()
    attempts = _login_attempts.get(ip, [])
    # Prune stale entries
    attempts = [t for t in attempts if now - t < _LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = attempts
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        oldest = attempts[0]
        retry_after = _LOGIN_WINDOW_SECONDS - (now - oldest)
        return max(1.0, retry_after)
    return None


def _record_login_attempt(ip: str) -> None:
    """Record a login attempt timestamp."""
    now = time.monotonic()
    if ip not in _login_attempts:
        _login_attempts[ip] = []
    _login_attempts[ip].append(now)
    # Opportunistic cleanup: remove IPs with only stale entries
    stale_ips = [
        k for k, v in _login_attempts.items()
        if all(now - t >= _LOGIN_WINDOW_SECONDS for t in v)
    ]
    for k in stale_ips:
        del _login_attempts[k]


# ---------------------------------------------------------------------------
# F6 Track B: per-user rate limiter for authenticated endpoints
# ---------------------------------------------------------------------------
# The login rate limiter above is per-IP by design (anonymous, no user
# identity yet). Once authenticated, we rate-limit per user_id — an
# attacker behind a single NAT should not DoS a co-tenant, and a runaway
# browser tab should not burn the DB.
_USER_RATE_LIMIT_MAX = int(os.environ.get("GOVERNANCE_CONSOLE_USER_RATE_LIMIT", "60"))
_USER_RATE_LIMIT_WINDOW_SECONDS = 60
_user_request_times: dict[str, list[float]] = {}


def _check_user_rate_limit(user_id: str) -> float | None:
    """Return seconds-until-retry if ``user_id`` is over quota, else None.

    Sliding 60 s window; cap is ``_USER_RATE_LIMIT_MAX`` requests.
    """
    now = time.monotonic()
    times = _user_request_times.get(user_id, [])
    times = [t for t in times if now - t < _USER_RATE_LIMIT_WINDOW_SECONDS]
    _user_request_times[user_id] = times
    if len(times) >= _USER_RATE_LIMIT_MAX:
        oldest = times[0]
        retry = _USER_RATE_LIMIT_WINDOW_SECONDS - (now - oldest)
        return max(1.0, retry)
    times.append(now)
    _user_request_times[user_id] = times
    # Opportunistic GC so the dict can't grow forever.
    if len(_user_request_times) > 4096:
        stale = [
            k
            for k, v in _user_request_times.items()
            if all(now - t >= _USER_RATE_LIMIT_WINDOW_SECONDS for t in v)
        ]
        for k in stale:
            del _user_request_times[k]
    return None


async def rate_limit_per_user(request: Request) -> None:
    """FastAPI dependency: 429 if the authenticated user is over quota.

    MUST be ordered AFTER ``authenticate`` so ``request.state.user_id``
    is populated. Anonymous callers fall through cleanly.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        return
    retry_after = _check_user_rate_limit(user_id)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded. Slow down and retry shortly.",
            headers={"Retry-After": str(int(retry_after))},
        )


# ---------------------------------------------------------------------------
# Audit module for console-originated events (gate grant/deny)
# ---------------------------------------------------------------------------
audit_module: AuditModule | None = None

# ---------------------------------------------------------------------------
# SSE: shared asyncpg LISTEN/NOTIFY state (per-worker, in-memory fan-out)
# ---------------------------------------------------------------------------
_SSE_QUEUE_MAX = 200           # drop oldest when a client queue exceeds this
_SSE_HEARTBEAT_INTERVAL = 15   # seconds between keepalive / housekeeping ticks
_SSE_REVALIDATE_INTERVAL = 30  # seconds between session re-validation checks
_SSE_MAX_DURATION = 4 * 3600   # maximum SSE connection lifetime (4 hours)
_SSE_REPLAY_CAP = 500          # max events replayed on Last-Event-ID reconnect
_SSE_CLAIM_STALE_SECS = 300    # stale reviewer claim threshold: 5 minutes

# client_id -> asyncio.Queue (str payload or None sentinel for revocation)
_sse_clients: dict[str, asyncio.Queue[str | None]] = {}
# session_cookie -> set[client_id] for instant server-side revocation
_sse_session_map: dict[str, set[str]] = defaultdict(set)
# background LISTEN task handle
_sse_listen_task: asyncio.Task[None] | None = None



def _normalize_url(url: str) -> str:
    if not url:
        raise ValueError(
            "Console startup failed: GOVERNANCE_DATABASE_URL env var is not set.\n"
            "Expected: a postgresql:// connection string.\n"
            "Fix: export GOVERNANCE_DATABASE_URL=postgresql://user:pass@host/db"
        )
    from ..utils import normalize_db_url
    return normalize_db_url(url, component="console")




def _asyncpg_url(sqlalchemy_url: str) -> str:
    """Convert SQLAlchemy asyncpg URL to raw asyncpg DSN.

    asyncpg.connect() expects postgresql:// not postgresql+asyncpg://.
    """
    if sqlalchemy_url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + sqlalchemy_url[len("postgresql+asyncpg://"):]
    return sqlalchemy_url


def _broadcast_sse(msg: str) -> None:
    """Fan-out a message string to all connected SSE client queues.

    Backpressure: if a queue exceeds _SSE_QUEUE_MAX, the oldest item is
    dropped before enqueueing the new message.
    """
    for q in list(_sse_clients.values()):
        if q.qsize() >= _SSE_QUEUE_MAX:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def _revoke_sse_session(session_cookie: str) -> None:
    """Signal all SSE connections for a session to close immediately.

    Pushes None (sentinel) to every client queue for this session. The SSE
    generator treats None as a shutdown signal and exits within one
    queue-read timeout (~1 s), satisfying the security requirement that
    revoked sessions close within 5 s.
    """
    client_ids = _sse_session_map.pop(session_cookie, set())
    for client_id in client_ids:
        q = _sse_clients.get(client_id)
        if q is not None:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass


async def _sse_listen_loop() -> None:
    """Shared asyncpg LISTEN connection for SSE fan-out.

    Runs for the lifetime of the worker process. Reconnects automatically
    with exponential back-off (1s -> 60s) on connection failure.

    Periodic housekeeping on every _SSE_HEARTBEAT_INTERVAL tick:
      1. Stale claim release: NULL out reviewer_id/reviewing_since on gates
         held > _SSE_CLAIM_STALE_SECS without resolution. The resulting
         UPDATE fires gate_change_notify, broadcasting the state change to
         all SSE clients via NOTIFY (no pg_cron required).
      2. Heartbeat broadcast: sends keepalive to all client queues to
         prevent proxy/LB connection timeouts.
    """
    global engine

    backoff = 1.0
    pg_conn: Any = None

    while True:
        try:
            raw_url = _asyncpg_url(_normalize_url(DATABASE_URL))
            pg_conn = await asyncpg.connect(raw_url)

            def _on_notify(
                conn: Any, pid: int, channel: str, payload: str
            ) -> None:
                msg = json.dumps({"channel": channel, "payload": payload})
                _broadcast_sse(msg)

            await pg_conn.add_listener("governance_events", _on_notify)
            await pg_conn.add_listener("governance_gates", _on_notify)
            await pg_conn.add_listener("governance_presence", _on_notify)

            backoff = 1.0
            _logger.info("sse.listen_connected")

            while True:
                await asyncio.sleep(_SSE_HEARTBEAT_INTERVAL)
                _broadcast_sse(": keepalive")

                if engine is not None:
                    try:
                        async with engine.begin() as conn:
                            await conn.execute(
                                text(
                                    "UPDATE governance_gates_pending "
                                    "SET reviewer_id = NULL, reviewing_since = NULL "
                                    "WHERE reviewer_id IS NOT NULL "
                                    "AND resolved_at IS NULL "
                                    "AND reviewing_since < NOW() - "
                                    "make_interval(secs => :stale_secs)"
                                ),
                                {"stale_secs": _SSE_CLAIM_STALE_SECS},
                            )
                    except Exception:
                        _logger.warning(
                            "sse.stale_claim_cleanup_failed", exc_info=False
                        )

        except asyncio.CancelledError:
            break
        except Exception:
            _logger.warning(
                "sse.listen_reconnecting", backoff=backoff, exc_info=False
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        finally:
            if pg_conn is not None:
                try:
                    await pg_conn.close()
                except Exception:
                    pass
                pg_conn = None

def _redact_metadata(meta: dict[str, Any] | list[Any] | Any) -> Any:
    """Strip sensitive keys from metadata before serving to the frontend.

    Walks nested dicts and lists recursively, redacting any key at any
    depth that matches the REDACT_KEYS set.
    """
    if isinstance(meta, dict):
        return {
            k: (
                "***REDACTED***"
                if k.lower() in REDACT_KEYS
                else _redact_metadata(v)
            )
            for k, v in meta.items()
        }
    if isinstance(meta, list):
        return [_redact_metadata(item) for item in meta]
    return meta


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
engine: AsyncEngine | None = None


def _validate_cors_origins(origins: list[str]) -> None:
    """Reject CORS wildcard '*' — require explicit origins."""
    for origin in origins:
        if origin.strip() == "*":
            raise ValueError(
                "CORS wildcard '*' is not allowed. "
                "List specific origins in GOVERNANCE_CONSOLE_CORS_ORIGINS."
            )


def _enforce_dev_mode_guards() -> None:
    """BLOCKER C4: enforce DEV_MODE start-up safety invariants.

    DEV_MODE silently grants ``role=admin`` to every caller. A leaked
    env var in a k8s deployment would otherwise mean unauthenticated
    admin on /halt, /batch-approve, and user-CRUD. The guards below
    refuse to start unless:

      1. the operator has explicitly acknowledged the risk by setting
         ``GOVERNANCE_CONSOLE_ALLOW_DEV_MODE=1``;
      2. the bind address is localhost (``127.0.0.1`` / ``localhost`` /
         ``::1``).

    A loud structlog ERROR (NOT warn) is emitted on every startup with
    DEV_MODE enabled so the line shows up in centralized logging even
    if the operator misses it locally.
    """
    if not DEV_MODE:
        return
    _logger.error(
        "console.dev_mode_enabled",
        message=(
            "GOVERNANCE_CONSOLE_DEV_MODE=true — all callers get admin. "
            "This must NEVER be true in production."
        ),
        bind_host=CONSOLE_HOST,
    )
    if not DEV_MODE_ACK:
        raise RuntimeError(
            "GOVERNANCE_CONSOLE_DEV_MODE=true is set but "
            "GOVERNANCE_CONSOLE_ALLOW_DEV_MODE=1 is NOT set. Refusing "
            "to start: set ALLOW_DEV_MODE=1 to explicitly acknowledge "
            "that every caller will be granted admin role."
        )
    localhost_binds = {"127.0.0.1", "localhost", "::1"}
    if CONSOLE_HOST not in localhost_binds:
        raise RuntimeError(
            f"GOVERNANCE_CONSOLE_DEV_MODE=true cannot be enabled on a "
            f"non-localhost bind (got {CONSOLE_HOST!r}). Set "
            f"GOVERNANCE_CONSOLE_HOST=127.0.0.1 or unset DEV_MODE."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    _enforce_dev_mode_guards()
    _validate_cors_origins(CORS_ORIGINS)
    global engine, audit_module, _sse_listen_task
    engine = create_async_engine(
        _normalize_url(DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    # Initialize audit module for console-originated events (gate grant/deny)
    if AUDIT_SECRET:
        try:
            from ..audit.postgres_store import PostgresAuditStore

            audit_store = PostgresAuditStore(DATABASE_URL)
            audit_module = AuditModule(
                audit_store,
                secret=AUDIT_SECRET.encode("utf-8"),
            )
        except ValueError:
            _logger.warning("console.audit_module_init_skipped_weak_secret")
            audit_module = None

    # Start shared asyncpg LISTEN loop for SSE fan-out
    if DATABASE_URL:
        _sse_listen_task = asyncio.create_task(_sse_listen_loop())

    yield

    # Graceful shutdown: cancel the LISTEN task first
    if _sse_listen_task is not None:
        _sse_listen_task.cancel()
        try:
            await _sse_listen_task
        except asyncio.CancelledError:
            pass
        _sse_listen_task = None

    if audit_module is not None:
        await audit_module.close()
    if engine is not None:
        await engine.dispose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Code Atelier Governance Console",
    version="0.5.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------------------------
# Auth: request models
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """Login request body."""

    model_config = ConfigDict(strict=True)

    username: str = Field(max_length=256)
    password: str = Field(max_length=1024)


class CreateUserRequest(BaseModel):
    """Create user request body."""

    model_config = ConfigDict(strict=True)

    username: str = Field(max_length=256)
    password: str = Field(max_length=1024)
    role: str = "viewer"


class UpdateUserRequest(BaseModel):
    """Update user request body."""

    model_config = ConfigDict(strict=True)

    role: str | None = None
    disabled: bool | None = None


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
async def authenticate(request: Request) -> None:
    """Three-mode auth: user auth, legacy token, dev mode.

    Injects ``user_id`` and ``role`` into ``request.state``.
    """
    # Health endpoint is unauthenticated
    if request.url.path == "/api/health":
        return

    # Dev mode: no auth, synthetic user
    if DEV_MODE:
        request.state.user_id = "dev"
        request.state.role = "admin"
        return

    # Session cookie auth
    session_cookie = request.cookies.get("governance_session")
    if session_cookie and engine is not None:
        try:
            sid = UUID(session_cookie)
        except ValueError:
            raise HTTPException(401, "Invalid session cookie.")
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT s.user_id, u.role, s.expires_at, s.revoked "
                    "FROM governance_console_sessions s "
                    "JOIN governance_console_users u ON s.user_id = u.user_id "
                    "WHERE s.session_id = :sid AND u.disabled = FALSE"
                ),
                {"sid": str(sid)},
            )
            row = res.mappings().first()
        if row is None:
            raise HTTPException(401, "Session not found.")
        if row["revoked"]:
            raise HTTPException(401, "Session revoked.")
        if row["expires_at"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            raise HTTPException(401, "Session expired.")
        request.state.user_id = str(row["user_id"])
        request.state.role = row["role"]
        return

    # Legacy bearer token fallback
    auth_header = request.headers.get("Authorization", "")
    if CONSOLE_TOKEN and auth_header == f"Bearer {CONSOLE_TOKEN}":
        request.state.user_id = "legacy-token"
        request.state.role = "viewer"
        return

    raise HTTPException(401, "Authentication required.")


def require_role(role: str) -> Any:
    """Dependency that checks request.state.role against required role."""
    async def check(request: Request) -> None:
        user_role = getattr(request.state, "role", None)
        if user_role != role and user_role != "admin":
            raise HTTPException(403, f"Requires {role} role.")
    return Depends(check)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": "0.5.0"}


# ---------------------------------------------------------------------------
# SSE: real-time event stream (asyncpg LISTEN/NOTIFY + asyncio.Queue fan-out)
# ---------------------------------------------------------------------------
@app.get("/api/stream/events")
async def stream_events(
    request: Request,
    last_event_id: str | None = Query(None),
) -> Any:
    """Server-Sent Events endpoint: audit events, gate changes, presence updates.

    Architecture: one shared asyncpg LISTEN connection per worker (started in
    lifespan). Each connecting client registers an asyncio.Queue. The shared
    LISTEN callback fans out NOTIFY payloads to all queues simultaneously.

    Auth: session cookie (same as all other endpoints). Re-validated every
    30 s. Maximum connection duration: 4 hours (forced reconnect thereafter).
    On server-side session revocation, the connection closes within 1 s.

    Reconnect: client sends Last-Event-ID (chain_seq of last seen audit event).
    Server replays up to _SSE_REPLAY_CAP missed events from governance_audit_events.

    Backpressure: if a client queue exceeds _SSE_QUEUE_MAX items, the oldest
    is dropped. Heartbeat: keepalive comment every _SSE_HEARTBEAT_INTERVAL s.
    """
    from starlette.responses import StreamingResponse

    session_cookie = request.cookies.get("governance_session")
    if not session_cookie and not DEV_MODE:
        token_header = request.headers.get("x-governance-token", "")
        if not (CONSOLE_TOKEN and token_header == f"Bearer {CONSOLE_TOKEN}"):
            raise HTTPException(401, "Authentication required for SSE stream.")

    # Initial session validation before opening the stream
    if session_cookie and engine is not None:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT user_id FROM governance_console_sessions "
                    "WHERE session_id = :sid AND expires_at > NOW() "
                    "AND revoked = FALSE"
                ),
                {"sid": session_cookie},
            )
            if res.first() is None:
                raise HTTPException(401, "Session expired or revoked.")

    client_id = str(uuid4())
    q: asyncio.Queue[str | None] = asyncio.Queue()
    _sse_clients[client_id] = q
    if session_cookie:
        _sse_session_map[session_cookie].add(client_id)

    async def event_generator() -> Any:
        """Yield SSE events from the fan-out queue plus initial replay."""
        try:
            start_time = time.monotonic()
            last_revalidate = time.monotonic()

            # Replay missed events when client reconnects with Last-Event-ID
            if last_event_id and engine is not None:
                try:
                    last_seq = int(last_event_id)
                    async with engine.connect() as conn:
                        res = await conn.execute(
                            text(
                                "SELECT event_id, agent_id, session_id, kind, "
                                "created_at, chain_seq "
                                "FROM governance_audit_events "
                                "WHERE chain_seq > :seq "
                                "ORDER BY chain_seq ASC "
                                "LIMIT :cap"
                            ),
                            {"seq": last_seq, "cap": _SSE_REPLAY_CAP},
                        )
                        for row in res.mappings():
                            evt = {
                                "event_id": str(row["event_id"]),
                                "agent_id": row["agent_id"],
                                "session_id": str(row["session_id"])
                                if row["session_id"] else None,
                                "kind": row["kind"],
                                "chain_seq": row["chain_seq"],
                                "created_at": row["created_at"].isoformat()
                                if row["created_at"] else None,
                            }
                            seq = row["chain_seq"]
                            yield f"id: {seq}\nevent: audit\ndata: {json.dumps(evt)}\n\n"
                except (ValueError, Exception):
                    pass

            while True:
                if time.monotonic() - start_time > _SSE_MAX_DURATION:
                    _logger.info("sse.max_duration_reached", client_id=client_id)
                    break

                if await request.is_disconnected():
                    break

                if (
                    time.monotonic() - last_revalidate > _SSE_REVALIDATE_INTERVAL
                    and session_cookie
                    and engine is not None
                ):
                    last_revalidate = time.monotonic()
                    async with engine.connect() as conn:
                        res = await conn.execute(
                            text(
                                "SELECT 1 FROM governance_console_sessions "
                                "WHERE session_id = :sid AND expires_at > NOW() "
                                "AND revoked = FALSE"
                            ),
                            {"sid": session_cookie},
                        )
                        if res.first() is None:
                            _logger.info("sse.session_expired", client_id=client_id)
                            break

                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if msg is None:
                    break

                if msg == ": keepalive":
                    yield ": keepalive\n\n"
                    continue

                try:
                    envelope = json.loads(msg)
                    channel = envelope.get("channel", "")
                    payload_str = envelope.get("payload", "{}")

                    if channel == "governance_events":
                        event_type = "audit"
                        payload_data = json.loads(payload_str)
                        event_id_str = str(payload_data.get("chain_seq", ""))
                    elif channel == "governance_gates":
                        event_type = "gate"
                        payload_data = json.loads(payload_str)
                        event_id_str = "gate-" + str(payload_data.get("request_id", ""))
                    elif channel == "governance_presence":
                        event_type = "presence"
                        payload_data = json.loads(payload_str)
                        event_id_str = "presence-" + str(payload_data.get("agent_id", ""))
                    else:
                        continue

                    yield (
                        f"id: {event_id_str}\n"
                        f"event: {event_type}\n"
                        f"data: {json.dumps(payload_data)}\n\n"
                    )
                except (json.JSONDecodeError, KeyError, TypeError):
                    _logger.warning("sse.malformed_payload", exc_info=False)

        finally:
            _sse_clients.pop(client_id, None)
            if session_cookie:
                _sse_session_map.get(session_cookie, set()).discard(client_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
async def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate and create a session. Sets an httpOnly cookie."""
    # Rate limiting: 5 attempts per IP per 60s window
    client_ip = request.client.host if request.client else "unknown"
    retry_after = _check_rate_limit(client_ip)
    if retry_after is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many login attempts. Try again later."},
            headers={"Retry-After": str(int(retry_after))},
        )
    _record_login_attempt(client_ip)
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    # Opportunistic session cleanup
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "DELETE FROM governance_console_sessions "
                "WHERE expires_at < NOW() OR revoked = TRUE"
            )
        )
    # Look up user
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT user_id, password_hash, role, disabled "
                "FROM governance_console_users "
                "WHERE username = :username"
            ),
            {"username": body.username.lower()},
        )
        row = res.mappings().first()
    if row is None or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Invalid username or password.")
    if row["disabled"]:
        _logger.info(
            "auth.login_disabled_account",
            username_hash=hashlib.sha256(body.username.lower().encode()).hexdigest()[:16],
        )
        raise HTTPException(401, "Invalid username or password.")
    # Create session
    sid = create_session_id()
    expires = session_expires_at(SESSION_TTL_HOURS)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO governance_console_sessions "
                "(session_id, user_id, expires_at) "
                "VALUES (:sid, :uid, :expires)"
            ),
            {"sid": str(sid), "uid": str(row["user_id"]), "expires": expires},
        )
    response = JSONResponse(
        content={"ok": True, "username": body.username, "role": row["role"]}
    )
    max_age = SESSION_TTL_HOURS * 3600
    secure = not DEV_MODE
    response.set_cookie(
        key="governance_session",
        value=str(sid),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/api",
        max_age=max_age,
    )
    return response


@app.post("/api/auth/logout", dependencies=[Depends(authenticate)])
async def logout(request: Request) -> JSONResponse:
    """Revoke the current session and clear the cookie."""
    session_cookie = request.cookies.get("governance_session")
    if session_cookie and engine is not None:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE governance_console_sessions "
                    "SET revoked = TRUE "
                    "WHERE session_id = :sid"
                ),
                {"sid": session_cookie},
            )
        # Signal all SSE connections for this session to close within 1 s
        _revoke_sse_session(session_cookie)
    response = JSONResponse(content={"ok": True})
    response.delete_cookie(key="governance_session", path="/api")
    return response


@app.get("/api/auth/me", dependencies=[Depends(authenticate)])
async def auth_me(request: Request) -> dict[str, Any]:
    """Return the current authenticated user."""
    user_id = getattr(request.state, "user_id", "unknown")
    role = getattr(request.state, "role", "unknown")
    username = "unknown"
    if engine is not None and user_id not in ("dev", "legacy-token", "unknown"):
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT username FROM governance_console_users "
                    "WHERE user_id = :uid"
                ),
                {"uid": user_id},
            )
            row = res.first()
            if row:
                username = row[0]
    elif user_id == "dev":
        username = "dev"
    elif user_id == "legacy-token":
        username = "legacy-token"
    return {"user_id": user_id, "username": username, "role": role}


# ---------------------------------------------------------------------------
# Admin: user management
# ---------------------------------------------------------------------------
@app.get(
    "/api/auth/users",
    dependencies=[Depends(authenticate), require_role("admin")],
)
async def list_users() -> list[dict[str, Any]]:
    """List all console users (admin only)."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT user_id, username, role, disabled, created_at, updated_at "
                "FROM governance_console_users "
                "ORDER BY created_at"
            )
        )
        return [
            {
                "user_id": str(row["user_id"]),
                "username": row["username"],
                "role": row["role"],
                "disabled": row["disabled"],
                "created_at": row["created_at"].isoformat()
                if row["created_at"]
                else None,
                "updated_at": row["updated_at"].isoformat()
                if row["updated_at"]
                else None,
            }
            for row in res.mappings()
        ]


@app.post(
    "/api/auth/users",
    dependencies=[Depends(authenticate), require_role("admin")],
)
async def create_user(body: CreateUserRequest) -> dict[str, Any]:
    """Create a new console user (admin only)."""
    if body.role not in ("viewer", "admin"):
        raise HTTPException(400, "Role must be 'viewer' or 'admin'.")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    uid = uuid4()
    pw_hash = hash_password(body.password)
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO governance_console_users "
                    "(user_id, username, password_hash, role, created_at, updated_at) "
                    "VALUES (:uid, :username, :pw_hash, :role, :now, :now)"
                ),
                {
                    "uid": str(uid),
                    "username": body.username.lower(),
                    "pw_hash": pw_hash,
                    "role": body.role,
                    "now": now,
                },
            )
    except Exception as exc:
        from sqlalchemy.exc import IntegrityError
        if isinstance(exc, IntegrityError):
            raise HTTPException(409, f"Username '{body.username}' already exists.") from None
        _logger.error(
            "console.create_user_failed",
            error_type=type(exc).__name__,
        )
        raise HTTPException(500, "Failed to create user. Check server logs.") from None
    return {"ok": True, "user_id": str(uid), "username": body.username.lower()}


@app.patch(
    "/api/auth/users/{user_id}",
    dependencies=[Depends(authenticate), require_role("admin")],
)
async def update_user(user_id: UUID, body: UpdateUserRequest) -> dict[str, Any]:
    """Update a user's role or disabled status (admin only)."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    updates: list[str] = []
    params: dict[str, Any] = {"uid": str(user_id), "now": datetime.now(timezone.utc)}
    if body.role is not None:
        if body.role not in ("viewer", "admin"):
            raise HTTPException(400, "Role must be 'viewer' or 'admin'.")
        updates.append("role = :role")
        params["role"] = body.role
    if body.disabled is not None:
        updates.append("disabled = :disabled")
        params["disabled"] = body.disabled
    if not updates:
        raise HTTPException(400, "No fields to update.")
    updates.append("updated_at = :now")
    set_clause = ", ".join(updates)
    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                f"UPDATE governance_console_users SET {set_clause} "
                "WHERE user_id = :uid"
            ),
            params,
        )
        if res.rowcount == 0:
            raise HTTPException(404, "User not found.")
    return {"ok": True, "user_id": str(user_id)}


@app.delete(
    "/api/auth/sessions/{session_id}",
    dependencies=[Depends(authenticate), require_role("admin")],
    response_model=SessionRevokeResponse,
)
async def revoke_session(
    session_id: UUID, request: Request
) -> SessionRevokeResponse:
    """Revoke a specific session (admin only).

    Writes a ``pipeline.session_revoked`` audit event to the HMAC chain so
    revocation is tamper-evidently recorded. Per CLAUDE.md invariant
    "audit logs are append-only, never updated or deleted" — session
    deletion must leave a permanent audit trace.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    operator_id = getattr(request.state, "user_id", None) or "unknown"
    # Pseudonymize operator id in audit metadata — log the SHA-256 prefix so
    # the console can correlate without leaking the raw UUID/username.
    revoked_by_hash = hashlib.sha256(
        str(operator_id).encode("utf-8")
    ).hexdigest()[:16]
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(
                text(
                    "UPDATE governance_console_sessions SET revoked = TRUE "
                    "WHERE session_id = :sid AND revoked = FALSE "
                    "RETURNING created_at"
                ),
                {"sid": str(session_id)},
            )
            row = res.mappings().first()
            if row is None:
                raise HTTPException(404, "Session not found or already revoked.")
            created_at = row["created_at"]
    except HTTPException:
        raise
    except Exception as exc:
        _logger.error(
            "console.revoke_session_failed",
            error_type=type(exc).__name__,
        )
        raise HTTPException(500, "Failed to revoke session.") from None

    # Append-only audit record: the DB row flipped, but the chain captures
    # the event permanently (F3 PRD mandate).
    if audit_module is not None:
        try:
            audit_session_id = UUID(
                hashlib.md5(str(session_id).encode("utf-8")).hexdigest()
            )
            await audit_module.log(
                AuditEvent(
                    agent_id="console",
                    session_id=audit_session_id,
                    kind="pipeline.session_revoked",
                    metadata={
                        "session_id": str(session_id),
                        "revoked_by": revoked_by_hash,
                        "revoked_at": now.isoformat(),
                        "session_created_at": created_at.isoformat()
                        if created_at
                        else None,
                    },
                )
            )
        except Exception as exc:
            # Audit failure MUST NOT leave the caller without a revoke ack —
            # the DB row is already updated. Log loudly and carry on.
            _logger.error(
                "console.revoke_session_audit_failed",
                session_id=str(session_id),
                error_type=type(exc).__name__,
            )

    # Signal all SSE connections for this session to close within 1 s
    _revoke_sse_session(str(session_id))
    return SessionRevokeResponse(ok=True, session_id=str(session_id))


@app.get("/api/agents", dependencies=[Depends(authenticate)])
async def list_agents(
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List agents by recent activity."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT agent_id, COUNT(*) as event_count, "
                "MAX(created_at) as last_active "
                "FROM governance_audit_events "
                "GROUP BY agent_id "
                "ORDER BY last_active DESC "
                "LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [
            {
                "agent_id": row["agent_id"],
                "event_count": row["event_count"],
                "last_active": row["last_active"].isoformat()
                if row["last_active"]
                else None,
            }
            for row in res.mappings()
        ]


@app.get("/api/events", dependencies=[Depends(authenticate)])
async def list_events(
    agent_id: str | None = None,
    kind: str | None = None,
    session_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """Paginated audit event explorer with filters."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    # Build the query using parameterized WHERE — no f-string interpolation
    # of user values. Each filter is a fixed string with a named parameter.
    base = (
        "SELECT event_id, session_id, agent_id, parent_event_id, "
        "kind, model, input_hash, output_hash, metadata_json, "
        "prev_hash, hmac_value, created_at, chain_seq "
        "FROM governance_audit_events "
    )
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if agent_id:
        clauses.append("agent_id = :agent_id")
        params["agent_id"] = agent_id
    if kind:
        clauses.append("kind = :kind")
        params["kind"] = kind
    if session_id:
        clauses.append("session_id = :session_id")
        params["session_id"] = session_id
    # Clauses are all hardcoded strings with named params — no user input
    # reaches the SQL text. Assembling them with AND is safe because the
    # set of possible clauses is fixed at compile time.
    where_sql = " AND ".join(clauses) if clauses else "TRUE"
    query = text(
        base + "WHERE " + where_sql + " ORDER BY chain_seq DESC LIMIT :limit OFFSET :offset"
    )
    async with engine.connect() as conn:
        res = await conn.execute(query, params)
        return [
            {
                "event_id": str(row["event_id"]),
                "session_id": str(row["session_id"]),
                "agent_id": row["agent_id"],
                "parent_event_id": str(row["parent_event_id"])
                if row["parent_event_id"]
                else None,
                "kind": row["kind"],
                "model": row["model"],
                "input_hash": row["input_hash"],
                "output_hash": row["output_hash"],
                "metadata": _redact_metadata(row["metadata_json"] or {}),
                "prev_hash": row["prev_hash"],
                "hmac_value": row["hmac_value"],
                "chain_seq": row["chain_seq"],
                "created_at": row["created_at"].isoformat()
                if row["created_at"]
                else None,
            }
            for row in res.mappings()
        ]


@app.get("/api/session/{session_id}/verify", dependencies=[Depends(authenticate)])
async def verify_session_chain(session_id: UUID) -> dict[str, Any]:
    """Verify the HMAC chain integrity for an entire session.

    The HMAC secret is used server-side only. The frontend receives a
    boolean per event + an overall pass/fail. The secret itself is NEVER
    returned.
    """
    if not AUDIT_SECRET:
        raise HTTPException(
            500,
            "GOVERNANCE_AUDIT_SECRET env var required for chain verification.",
        )
    secret = AUDIT_SECRET.encode("utf-8")
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT event_id, session_id, agent_id, parent_event_id, "
                "kind, model, input_hash, output_hash, metadata_json, "
                "prev_hash, hmac_value, created_at "
                "FROM governance_audit_events "
                "WHERE session_id = :sid ORDER BY chain_seq"
            ),
            {"sid": str(session_id)},
        )
        rows = list(res.mappings())

    if not rows:
        return {"session_id": str(session_id), "event_count": 0, "verified": True}

    results: list[dict[str, Any]] = []
    all_ok = True
    for row in rows:
        record = AuditEventRecord(
            event_id=row["event_id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            parent_event_id=row["parent_event_id"],
            kind=row["kind"],
            model=row["model"],
            input_hash=row["input_hash"],
            output_hash=row["output_hash"],
            metadata=row["metadata_json"] or {},
            prev_hash=row["prev_hash"],
            hmac=row["hmac_value"],
            created_at=row["created_at"],
        )
        ok = verify_event(record, secret)
        if not ok:
            all_ok = False
        results.append(
            {
                "event_id": str(row["event_id"]),
                "kind": row["kind"],
                "verified": ok,
            }
        )

    return {
        "session_id": str(session_id),
        "event_count": len(rows),
        "verified": all_ok,
        "events": results,
        "first_failure": next(
            (r["event_id"] for r in results if not r["verified"]), None
        ),
    }


@app.get("/api/cost/agents", dependencies=[Depends(authenticate)])
async def cost_agents() -> list[dict[str, Any]]:
    """Cost summary per agent for today."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT agent_id, usd_used, tokens_used "
                "FROM governance_cost_agent_daily "
                "WHERE day_utc = (NOW() AT TIME ZONE 'UTC')::DATE "
                "ORDER BY usd_used DESC"
            )
        )
        return [
            {
                "agent_id": row["agent_id"],
                "usd_used_today": float(row["usd_used"]),
                "tokens_used_today": int(row["tokens_used"]),
            }
            for row in res.mappings()
        ]


@app.get("/api/cost/sessions", dependencies=[Depends(authenticate)])
async def cost_sessions(
    agent_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Cost per session, ordered by spend."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if agent_id:
        clauses.append("agent_id = :agent_id")
        params["agent_id"] = agent_id
    where_sql = " AND ".join(clauses) if clauses else "TRUE"
    query = text(
        "SELECT agent_id, session_id, usd_used, tokens_used, last_updated "
        "FROM governance_cost_session_usage "
        "WHERE " + where_sql + " "
        "ORDER BY usd_used DESC LIMIT :limit"
    )
    async with engine.connect() as conn:
        res = await conn.execute(query, params)
        return [
            {
                "agent_id": row["agent_id"],
                "session_id": str(row["session_id"]),
                "usd_used": float(row["usd_used"]),
                "tokens_used": int(row["tokens_used"]),
                "last_updated": row["last_updated"].isoformat()
                if row["last_updated"]
                else None,
            }
            for row in res.mappings()
        ]


@app.get("/api/gates/pending", dependencies=[Depends(authenticate), Depends(rate_limit_per_user)])
async def gates_pending() -> list[dict[str, Any]]:
    """List all unresolved approval requests."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id, agent_id, kind, action_hash, "
                "created_at, expires_at, reviewer_id, reviewing_since "
                "FROM governance_gates_pending "
                "WHERE resolved_at IS NULL "
                "ORDER BY created_at DESC"
            )
        )
        return [
            {
                "request_id": str(row["request_id"]),
                "agent_id": row["agent_id"],
                "kind": row["kind"],
                "action_hash": row["action_hash"],
                "created_at": row["created_at"].isoformat()
                if row["created_at"] else None,
                "expires_at": row["expires_at"].isoformat()
                if row["expires_at"] else None,
                "reviewer_id": str(row["reviewer_id"])
                if row["reviewer_id"] else None,
                "reviewing_since": row["reviewing_since"].isoformat()
                if row.get("reviewing_since") else None,
            }
            for row in res.mappings()
        ]


@app.get("/api/gates/recent", dependencies=[Depends(authenticate), Depends(rate_limit_per_user)])
async def gates_recent(
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Recent gate resolutions."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id, agent_id, kind, resolution, "
                "created_at, resolved_at "
                "FROM governance_gates_pending "
                "WHERE resolved_at IS NOT NULL "
                "ORDER BY resolved_at DESC LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [
            {
                "request_id": str(row["request_id"]),
                "agent_id": row["agent_id"],
                "kind": row["kind"],
                "resolution": row["resolution"],
                "created_at": row["created_at"].isoformat()
                if row["created_at"]
                else None,
                "resolved_at": row["resolved_at"].isoformat()
                if row["resolved_at"]
                else None,
            }
            for row in res.mappings()
        ]


async def _check_self_approval(
    conn: Any,
    agent_id: str,
    user_id: str,
    request_id: UUID,
) -> None:
    """Enforce self-approval prevention on HITL gates.

    Looks up the agent's operator_id from the presence table and compares
    it to the authenticated console user. Three outcomes:

    - operator_id matches user_id: raise 403 (blocked)
    - operator_id is NULL: allow but log an audit warning
    - operator_id differs from user_id: allow (verified safe)
    """
    res = await conn.execute(
        text(
            "SELECT operator_id FROM governance_agent_presence "
            "WHERE agent_id = :agent_id"
        ),
        {"agent_id": agent_id},
    )
    presence_row = res.mappings().first()
    operator_id = presence_row["operator_id"] if presence_row else None

    if operator_id is not None and operator_id == user_id:
        _logger.warning(
            "console.self_approval_blocked",
            agent_id=agent_id,
            user_id=user_id,
            request_id=str(request_id),
        )
        raise HTTPException(
            403, "Cannot approve your own agent's requests."
        )

    if operator_id is None:
        _logger.warning(
            "console.self_approval_blocked_no_operator",
            agent_id=agent_id,
            user_id=user_id,
            request_id=str(request_id),
        )
        raise HTTPException(
            403,
            "Cannot verify operator identity: agent has no operator_id set. "
            "Register operator_id via sdk.presence.heartbeat(agent_id, operator_id=...) "
            "before requesting HITL approval.",
        )
    else:
        _logger.info(
            "console.self_approval_check_passed",
            agent_id=agent_id,
            operator_id=operator_id,
            user_id=user_id,
            request_id=str(request_id),
        )


@app.post("/api/gates/{request_id}/grant", dependencies=[Depends(authenticate)])
async def grant_gate(request_id: UUID, request: Request) -> dict[str, Any]:
    """Grant a pending approval gate request."""
    if not AUDIT_SECRET:
        raise HTTPException(
            500,
            "GOVERNANCE_AUDIT_SECRET env var required for gate operations.",
        )
    secret = AUDIT_SECRET.encode("utf-8")
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    user_id = getattr(request.state, "user_id", None)
    user_role = getattr(request.state, "role", None)
    async with engine.begin() as conn:
        # Read the pending gate row
        res = await conn.execute(
            text(
                "SELECT request_id, agent_id, kind, token, action_hash, reviewer_id "
                "FROM governance_gates_pending "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {"rid": str(request_id)},
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(404, "Gate request not found or already resolved.")

        # Self-approval prevention (fail-closed on missing operator_id)
        if user_id is not None:
            await _check_self_approval(conn, row["agent_id"], user_id, request_id)

        # Claim enforcement: if claimed, acting user must be the claimant
        # or an admin (admin-override preserves incident-response path).
        reviewer_id = row.get("reviewer_id")
        if (
            reviewer_id is not None
            and user_id is not None
            and str(reviewer_id) != user_id
            and user_role != "admin"
        ):
            raise HTTPException(
                403,
                "Gate is claimed by another reviewer. "
                "Wait for the reviewer to act or for the claim to expire.",
            )

        # Verify the HMAC signature on the stored token. The token
        # format is defined by ``gates.tokens.make_token``:
        #     f"{request_id}:{action_hash}:{expires_at_iso}:{hmac_hex}"
        # Dogfood the SDK's own ``parse_token`` so the verification path
        # is identical to the one used by agents calling ``sdk.gates.grant``.
        # ``parse_token`` checks:
        #   (a) HMAC signature matches under the current audit secret
        #   (b) token has not expired
        # and returns the parsed (request_id, action_hash, expires_at).
        # We additionally cross-check that the parsed request_id matches
        # the URL path param and that the parsed action_hash matches the
        # stored ``action_hash`` column — the two v0.1 bindings that
        # prevent token-swap and action-hash-swap attacks.
        token_value = row["token"]
        if token_value:
            try:
                parsed_rid, parsed_action_hash, _exp = parse_token(
                    secret=secret, token=token_value
                )
            except ApprovalTokenError as exc:
                raise HTTPException(400, f"Token HMAC verification failed: {exc}") from exc
            if parsed_rid != request_id:
                raise HTTPException(400, "Token HMAC verification failed.")
            stored_ah = row["action_hash"]
            if stored_ah is not None and parsed_action_hash != stored_ah:
                raise HTTPException(400, "Token HMAC verification failed.")

        now = datetime.now(timezone.utc)
        # v0.6.2 P0: close the TOCTOU between the SELECT-for-authz and the
        # UPDATE. At default READ COMMITTED isolation a racing ``claim``
        # could mutate ``reviewer_id`` between the two statements — the
        # authz check would pass on stale state (reviewer_id=NULL) and
        # the UPDATE would commit anyway, emitting a bogus
        # ``approval.granted`` audit row on a claim owned by another
        # reviewer. Mitigation mirrors ``escalate_gate``:
        #   * Pin the UPDATE to the reviewer_id we read with
        #     ``IS NOT DISTINCT FROM`` (handles the NULL-unclaimed case).
        #   * Use RETURNING + a .first()-is-None check to detect the race.
        #   * Admins bypass the pin so incident-response flows still work
        #     when a reviewer is claimed between SELECT and UPDATE.
        # A lost race → 409, NOT silent success.
        if user_role == "admin":
            update_res = await conn.execute(
                text(
                    "UPDATE governance_gates_pending "
                    "SET resolved_at = :now, resolution = :resolution "
                    "WHERE request_id = :rid AND resolved_at IS NULL "
                    "RETURNING request_id"
                ),
                {
                    "now": now,
                    "resolution": "granted",
                    "rid": str(request_id),
                },
            )
        else:
            update_res = await conn.execute(
                text(
                    "UPDATE governance_gates_pending "
                    "SET resolved_at = :now, resolution = :resolution "
                    "WHERE request_id = :rid AND resolved_at IS NULL "
                    "AND reviewer_id IS NOT DISTINCT FROM :expected_reviewer "
                    "RETURNING request_id"
                ),
                {
                    "now": now,
                    "resolution": "granted",
                    "rid": str(request_id),
                    "expected_reviewer": str(reviewer_id) if reviewer_id else None,
                },
            )
        if update_res.first() is None:
            raise HTTPException(
                409,
                "Gate was claimed by another reviewer mid-request; "
                "refresh and try again.",
            )

    # Audit event via SDK — part of the HMAC chain. Emitted ONLY on
    # success (the RETURNING row above confirms the UPDATE committed).
    if audit_module is not None:
        # Deterministic session_id derived from request_id for traceability
        session_id = UUID(
            hashlib.md5(str(request_id).encode("utf-8")).hexdigest()
        )
        await audit_module.log(
            AuditEvent(
                agent_id=row["agent_id"],
                session_id=session_id,
                kind="approval.granted",
                metadata={
                    "request_id": str(request_id),
                    "gate_kind": row["kind"],
                    "granted_by": user_id,
                },
            )
        )

    return {"ok": True, "request_id": str(request_id), "resolution": "granted"}



class DenyRequest(BaseModel):
    """Deny gate request body."""

    model_config = ConfigDict(strict=True)

    rationale: str = Field(
        min_length=1,
        max_length=2000,
        description=(
            "Plain-text reason for denial. Stored in the HMAC-chained audit event "
            "and in the gate row. Never rendered as HTML."
        ),
    )


@app.post("/api/gates/{request_id}/deny", dependencies=[Depends(authenticate)])
async def deny_gate(
    request_id: UUID, body: DenyRequest, request: Request
) -> dict[str, Any]:
    """Deny a pending approval gate request. Requires a rationale.

    The rationale is stored as plain text in:
      1. governance_gates_pending.rationale (mutable column for display)
      2. The HMAC-chained audit event metadata (immutable, tamper-evident)
    """
    if not AUDIT_SECRET:
        raise HTTPException(
            500,
            "GOVERNANCE_AUDIT_SECRET env var required for gate operations.",
        )
    secret = AUDIT_SECRET.encode("utf-8")
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    user_id = getattr(request.state, "user_id", None)
    user_role = getattr(request.state, "role", None)
    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id, agent_id, kind, token, action_hash, reviewer_id "
                "FROM governance_gates_pending "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {"rid": str(request_id)},
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(404, "Gate request not found or already resolved.")

        if user_id is not None:
            await _check_self_approval(conn, row["agent_id"], user_id, request_id)

        # Claim enforcement: if claimed, acting user must be the claimant
        # or an admin (admin-override preserves incident-response path).
        reviewer_id = row.get("reviewer_id")
        if (
            reviewer_id is not None
            and user_id is not None
            and str(reviewer_id) != user_id
            and user_role != "admin"
        ):
            raise HTTPException(
                403,
                "Gate is claimed by another reviewer. "
                "Wait for the reviewer to act or for the claim to expire.",
            )

        # Verify the HMAC signature on the stored token — same logic as
        # ``grant_gate``. See the comment there for the full rationale.
        token_value = row["token"]
        if token_value:
            try:
                parsed_rid, parsed_action_hash, _exp = parse_token(
                    secret=secret, token=token_value
                )
            except ApprovalTokenError as exc:
                raise HTTPException(400, f"Token HMAC verification failed: {exc}") from exc
            if parsed_rid != request_id:
                raise HTTPException(400, "Token HMAC verification failed.")
            stored_ah = row["action_hash"]
            if stored_ah is not None and parsed_action_hash != stored_ah:
                raise HTTPException(400, "Token HMAC verification failed.")

        now = datetime.now(timezone.utc)
        # v0.6.2 P0: close the TOCTOU between the SELECT-for-authz and
        # the UPDATE. See ``grant_gate`` for the full rationale — same
        # race, same mitigation. Admins bypass the reviewer pin so
        # incident-response flows still work when a gate is claimed
        # between SELECT and UPDATE; non-admins get the pin, a lost
        # race 409s without emitting a bogus ``approval.denied`` audit
        # row on a claim owned by another reviewer.
        if user_role == "admin":
            update_res = await conn.execute(
                text(
                    "UPDATE governance_gates_pending "
                    "SET resolved_at = :now, resolution = :resolution, "
                    "rationale = :rationale "
                    "WHERE request_id = :rid AND resolved_at IS NULL "
                    "RETURNING request_id"
                ),
                {
                    "now": now,
                    "resolution": "denied",
                    "rationale": body.rationale,
                    "rid": str(request_id),
                },
            )
        else:
            update_res = await conn.execute(
                text(
                    "UPDATE governance_gates_pending "
                    "SET resolved_at = :now, resolution = :resolution, "
                    "rationale = :rationale "
                    "WHERE request_id = :rid AND resolved_at IS NULL "
                    "AND reviewer_id IS NOT DISTINCT FROM :expected_reviewer "
                    "RETURNING request_id"
                ),
                {
                    "now": now,
                    "resolution": "denied",
                    "rationale": body.rationale,
                    "rid": str(request_id),
                    "expected_reviewer": str(reviewer_id) if reviewer_id else None,
                },
            )
        if update_res.first() is None:
            raise HTTPException(
                409,
                "Gate was claimed by another reviewer mid-request; "
                "refresh and try again.",
            )

    # Audit event — emitted ONLY on success (the RETURNING row above
    # confirms the UPDATE committed).
    if audit_module is not None:
        session_id = UUID(
            hashlib.md5(str(request_id).encode("utf-8")).hexdigest()
        )
        await audit_module.log(
            AuditEvent(
                agent_id=row["agent_id"],
                session_id=session_id,
                kind="approval.denied",
                metadata={
                    "request_id": str(request_id),
                    "gate_kind": row["kind"],
                    "denied_by": user_id,
                    "rationale": body.rationale,
                },
            )
        )

    return {"ok": True, "request_id": str(request_id), "resolution": "denied"}


# F1 scope list fields that must be lifted to typed top-level attrs on
# PolicyRow because ``policy: dict[str, MetadataValue]`` is scalar-only.
_POLICY_LIST_FIELDS = (
    "allowed_tools",
    "hidden_tools",
    "allowed_apis",
    "allowed_models",
)


def _coerce_string_list(raw: Any) -> list[str] | None:
    """Narrow a raw policy-JSON value to ``list[str]`` or ``None``.

    Drops non-string entries fail-closed so a drifted backend cannot
    smuggle arbitrary scalars into the typed API surface. Returns
    ``None`` when the field is absent / not a list.
    """
    if not isinstance(raw, list):
        return None
    return [v for v in raw if isinstance(v, str)]


def _build_policy_row(row: Any) -> PolicyRow:
    """Build a typed ``PolicyRow`` from a raw governance_policies mapping.

    DA Wave 4 blocker fix (F1): lifts the four scope list fields out of
    the raw policy JSON and onto typed top-level attributes. The scalar
    residue stays in ``policy`` (which is strict scalar-only).
    """
    raw_policy = redact_secrets(row["policy_json"] or {})
    # Split scalars from list fields so ``policy`` stays scalar-only.
    list_fields: dict[str, list[str] | None] = {}
    scalar_policy: dict[str, Any] = {}
    for key, val in raw_policy.items():
        if key in _POLICY_LIST_FIELDS:
            list_fields[key] = _coerce_string_list(val)
        else:
            scalar_policy[key] = val
    return PolicyRow(
        agent_id=row["agent_id"],
        policy_type=row["policy_type"],
        policy=scalar_policy,
        allowed_tools=list_fields.get("allowed_tools"),
        hidden_tools=list_fields.get("hidden_tools"),
        allowed_apis=list_fields.get("allowed_apis"),
        allowed_models=list_fields.get("allowed_models"),
        updated_at=row["updated_at"],
    )


@app.get(
    "/api/policies",
    dependencies=[Depends(authenticate), Depends(rate_limit_per_user)],
    response_model=PolicyListResponse,
)
async def list_policies() -> PolicyListResponse:
    """Return all policies from the governance_policies table."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, policy_type, policy_json, updated_at "
                    "FROM governance_policies "
                    "ORDER BY agent_id, policy_type"
                )
            )
            rows = list(res.mappings())
    except Exception as exc:
        _logger.warning(
            "console.list_policies_failed", exc_type=type(exc).__name__
        )
        raise HTTPException(500, "Failed to load policies.") from None

    policies = [_build_policy_row(row) for row in rows]
    return PolicyListResponse(policies=policies)


@app.get(
    "/api/policies/{agent_id}",
    dependencies=[Depends(authenticate), Depends(rate_limit_per_user)],
    response_model=AgentPoliciesResponse,
)
async def get_agent_policies(agent_id: str) -> AgentPoliciesResponse:
    """Return scope + budget policies for a single agent."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, policy_type, policy_json, updated_at "
                    "FROM governance_policies "
                    "WHERE agent_id = :agent_id "
                    "ORDER BY policy_type"
                ),
                {"agent_id": agent_id},
            )
            rows = list(res.mappings())
    except Exception as exc:
        _logger.warning(
            "console.get_agent_policies_failed", exc_type=type(exc).__name__
        )
        raise HTTPException(500, "Failed to load agent policies.") from None

    policies = [_build_policy_row(row) for row in rows]
    return AgentPoliciesResponse(agent_id=agent_id, policies=policies)


@app.get("/api/posture", dependencies=[Depends(authenticate)])
async def governance_posture() -> dict[str, Any]:
    """Governance posture overview — the Persona D CEO demo page.

    Maps the four enforcement modules to a per-agent summary with
    pass/warn/fail status. One glance, one screenshot.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        # Agent activity
        agents_res = await conn.execute(
            text(
                "SELECT agent_id, COUNT(*) as event_count, "
                "MAX(created_at) as last_active "
                "FROM governance_audit_events "
                "GROUP BY agent_id ORDER BY last_active DESC LIMIT 50"
            )
        )
        agents = {r["agent_id"]: dict(r) for r in agents_res.mappings()}

        # Scope violations today
        violations_res = await conn.execute(
            text(
                "SELECT agent_id, COUNT(*) as violation_count "
                "FROM governance_audit_events "
                "WHERE kind = 'scope.violation' "
                "AND created_at >= (NOW() AT TIME ZONE 'UTC')::DATE "
                "GROUP BY agent_id "
                "LIMIT 50"
            )
        )
        violations = {r["agent_id"]: r["violation_count"] for r in violations_res.mappings()}

        # Budget usage today
        budget_res = await conn.execute(
            text(
                "SELECT agent_id, usd_used, tokens_used "
                "FROM governance_cost_agent_daily "
                "WHERE day_utc = (NOW() AT TIME ZONE 'UTC')::DATE "
                "LIMIT 50"
            )
        )
        budgets = {r["agent_id"]: dict(r) for r in budget_res.mappings()}

        # Pending approvals
        pending_res = await conn.execute(
            text(
                "SELECT agent_id, COUNT(*) as pending_count "
                "FROM governance_gates_pending "
                "WHERE resolved_at IS NULL "
                "GROUP BY agent_id "
                "LIMIT 50"
            )
        )
        pending = {r["agent_id"]: r["pending_count"] for r in pending_res.mappings()}

        # Budget exceeded events today
        exceeded_res = await conn.execute(
            text(
                "SELECT agent_id, COUNT(*) as exceeded_count "
                "FROM governance_audit_events "
                "WHERE kind = 'budget.exceeded' "
                "AND created_at >= (NOW() AT TIME ZONE 'UTC')::DATE "
                "GROUP BY agent_id "
                "LIMIT 50"
            )
        )
        exceeded = {r["agent_id"]: r["exceeded_count"] for r in exceeded_res.mappings()}

        # Latest scope violation per agent (for inline details on posture cards)
        latest_violation_res = await conn.execute(
            text(
                "SELECT DISTINCT ON (agent_id) agent_id, metadata_json, created_at "
                "FROM governance_audit_events "
                "WHERE kind = 'scope.violation' "
                "AND created_at >= (NOW() AT TIME ZONE 'UTC')::DATE "
                "ORDER BY agent_id, created_at DESC"
            )
        )
        latest_violations: dict[str, dict[str, Any]] = {}
        for r in latest_violation_res.mappings():
            meta = r["metadata_json"] or {}
            latest_violations[r["agent_id"]] = {
                "tool": meta.get("tool", meta.get("action", "unknown")),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }

        # Budget policies for WARN threshold calculation
        budget_policies: dict[str, dict[str, Any]] = {}
        try:
            policies_res = await conn.execute(
                text(
                    "SELECT agent_id, policy_json "
                    "FROM governance_policies "
                    "WHERE policy_type = 'budget'"
                )
            )
            for r in policies_res.mappings():
                pj = r["policy_json"]
                if isinstance(pj, dict):
                    budget_policies[r["agent_id"]] = pj
        except Exception as exc:
            # Table may not exist yet; treat as no policies
            _logger.debug(
                "console.budget_policies_query_skipped",
                exc_type=type(exc).__name__,
            )

    posture: list[dict[str, Any]] = []
    for agent_id, info in agents.items():
        v_count = violations.get(agent_id, 0)
        b = budgets.get(agent_id, {})
        p_count = pending.get(agent_id, 0)
        e_count = exceeded.get(agent_id, 0)

        # Cost status: FAIL if budget exceeded, WARN if spend > 50% of any
        # registered budget cap, PASS otherwise (including uncapped agents).
        usd_today = float(b.get("usd_used", 0))
        cost_status = "PASS"
        if e_count > 0:
            cost_status = "FAIL"
        elif agent_id in budget_policies and usd_today > 0:
            bp = budget_policies[agent_id]
            daily_cap = bp.get("per_agent_usd_daily")
            if daily_cap is not None and float(daily_cap) > 0:
                if usd_today > float(daily_cap) * 0.5:
                    cost_status = "WARN"

        posture.append(
            {
                "agent_id": agent_id,
                "event_count": info["event_count"],
                "last_active": info["last_active"].isoformat()
                if info["last_active"]
                else None,
                "scope": {
                    "status": "FAIL" if v_count > 0 else "PASS",
                    "violations_today": v_count,
                    "latest_violation": latest_violations.get(agent_id),
                },
                "cost": {
                    "status": cost_status,
                    "usd_today": usd_today,
                    "tokens_today": int(b.get("tokens_used", 0)),
                    "exceeded_today": e_count,
                },
                "gates": {
                    "status": "WARN" if p_count > 0 else "PASS",
                    "pending": p_count,
                },
                "audit": {
                    "status": "PASS",
                    "events_total": info["event_count"],
                },
            }
        )

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "agent_count": len(posture),
        "agents": posture,
    }


@app.get("/api/cost/models", dependencies=[Depends(authenticate)])
async def cost_model_breakdown(
    agent_id: str | None = Query(None, min_length=1),
) -> list[dict[str, Any]]:
    """Per-model cost breakdown for today.

    Returns rows from ``governance_cost_model_daily`` for the current UTC
    day. Filter by ``agent_id`` when provided; returns all agents otherwise.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    try:
        clauses: list[str] = ["day_utc = (NOW() AT TIME ZONE 'UTC')::DATE"]
        params: dict[str, Any] = {}
        if agent_id:
            clauses.append("agent_id = :agent_id")
            params["agent_id"] = agent_id
        where_sql = " AND ".join(clauses)
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, model, usd_used, tokens_used, last_updated "
                    f"FROM governance_cost_model_daily WHERE {where_sql} "
                    "ORDER BY usd_used DESC"
                ),
                params,
            )
            return [
                {
                    "agent_id": row["agent_id"],
                    "model": row["model"],
                    "usd_used_today": float(row["usd_used"]),
                    "tokens_used_today": int(row["tokens_used"]),
                    "last_updated": row["last_updated"].isoformat()
                    if row["last_updated"]
                    else None,
                }
                for row in res.mappings()
            ]
    except Exception as exc:
        # Table may not exist yet if migration hasn't been run
        _logger.debug(
            "console.cost_model_breakdown_skipped",
            exc_type=type(exc).__name__,
        )
        return []


@app.get(
    "/api/agents/presence",
    dependencies=[Depends(authenticate), Depends(rate_limit_per_user)],
    response_model=AgentPresenceResponse,
)
async def agent_presence() -> AgentPresenceResponse:
    """List all agents with their presence status."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT agent_id, status, last_heartbeat, started_at, metadata_json "
                    "FROM governance_agent_presence "
                    "ORDER BY agent_id"
                )
            )
            rows = list(res.mappings())
    except Exception as exc:
        # Table may not exist yet if migration hasn't been run
        _logger.debug(
            "console.agent_presence_skipped",
            exc_type=type(exc).__name__,
        )
        return AgentPresenceResponse(agents=[])

    agents = [
        AgentPresenceRow(
            agent_id=row["agent_id"],
            status=row["status"],
            last_heartbeat=row["last_heartbeat"],
            started_at=row["started_at"],
            metadata=redact_secrets(row["metadata_json"] or {}),
        )
        for row in rows
    ]
    return AgentPresenceResponse(agents=agents)

# ---------------------------------------------------------------------------
# New endpoint: Agent halt switch (v0.6 F2.5 rename of the v0.5.4 kill switch)
# ---------------------------------------------------------------------------
# C0 controls to strip from `reason` (post-escape) so that an operator can't
# sneak raw terminal / log-injection bytes into the audit chain. Note `\x09`
# (tab), `\x0a` (LF), `\x0d` (CR) are EXCLUDED here because they are already
# converted to their printable backslash forms in the escape step above.
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HALT_REASON_MAX = 512
# Back-compat alias (removed in v0.7). v0.5.x code that reached into
# app-internal constants keeps working for one release.
_KILL_REASON_MAX = _HALT_REASON_MAX


class HaltRequest(BaseModel):
    """Halt-switch request body.

    Security (F2 P0, Cybersec HIGH): the `reason` field flows all the way
    into the HMAC-chained audit row. An unescaped newline would let an
    operator forge a follow-on "event" visually in chain exports. An
    unbounded length would let them DoS a reviewer's UI. A zalgo bomb
    (combining marks) would expand post-decode if we capped bytes instead
    of Unicode codepoints. The validator below normalizes, length-caps,
    escapes, then strips C0 controls — in that order.

    v0.6 F2.5: renamed from ``KillRequest``. The ``KillRequest`` name is
    preserved as a module-level alias (``KillRequest = HaltRequest``) for
    one release and removed in v0.7.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    reason: str = Field(
        min_length=1,
        description="Required: why the agent is being halted. Max 512 chars "
        "after NFC normalization; control characters are escaped or stripped.",
    )

    @field_validator("reason")
    @classmethod
    def _sanitize_reason(cls, value: str) -> str:
        # BLOCKER C5: delegate to the shared sanitizer in
        # codeatelier_governance.audit.sanitization so the HTTP layer and
        # the programmatic audit layer apply IDENTICAL rules. The HTTP
        # `reason` field uses the tighter 512-char cap.
        from codeatelier_governance.audit.sanitization import (
            HALT_REASON_MAX_LEN,
            sanitize_string,
        )
        return sanitize_string(value, max_len=HALT_REASON_MAX_LEN)


# Backward-compat alias (deprecated; removed in v0.7). v0.5.x callers that
# import ``KillRequest`` from this module keep working. Because this is an
# identity alias, ``isinstance(req, KillRequest)`` and
# ``isinstance(req, HaltRequest)`` are both True.
KillRequest = HaltRequest


@app.post(
    "/api/agents/{agent_id}/halt",
    dependencies=[Depends(authenticate), require_role("admin")],
)
async def halt_agent(
    agent_id: str, body: HaltRequest, request: Request
) -> dict[str, Any]:
    """Halt an agent -- admin only. Audit-logged. Requires reason.

    Sets the agent's presence status to unresponsive and records halt
    metadata in metadata_json. Process termination is the host's responsibility.

    v0.6 F2.5: renamed from ``kill_agent`` / ``POST /api/agents/{id}/kill``.
    The old route is preserved as a deprecated alias that delegates to this
    handler and attaches ``Deprecation``/``Sunset`` response headers.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    user_id = getattr(request.state, "user_id", "unknown")
    now = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        res = await conn.execute(
            text("SELECT agent_id FROM governance_agent_presence WHERE agent_id = :aid"),
            {"aid": agent_id},
        )
        if res.first() is None:
            raise HTTPException(404, "Agent not found in presence table.")

        halt_meta = json.dumps({
            "_halted_by": user_id,
            "_halted_at": now.isoformat(),
            "_halt_reason": body.reason,
        })
        await conn.execute(
            text(
                "UPDATE governance_agent_presence "
                "SET status = 'unresponsive', "
                "    metadata_json = COALESCE(metadata_json, '{}'::jsonb) "
                "                    || :meta::jsonb "
                "WHERE agent_id = :aid"
            ),
            {"aid": agent_id, "meta": halt_meta},
        )

    if audit_module is not None:
        session_id = UUID(
            hashlib.md5(("halt:" + agent_id + ":" + now.isoformat()).encode()).hexdigest()
        )
        await audit_module.log(
            AuditEvent(
                agent_id=agent_id,
                session_id=session_id,
                kind="agent.halted",
                metadata={
                    "halted_by": user_id,
                    "reason": body.reason,
                    "halted_at": now.isoformat(),
                },
            )
        )

    _logger.info("console.agent_halted", agent_id=agent_id, halted_by=user_id)
    return {
        "ok": True,
        "agent_id": agent_id,
        "action": "halted",
        "halted_at": now.isoformat(),
    }


# Back-compat route (v0.6 → removed in v0.7). Delegates to halt_agent and
# attaches Deprecation + Sunset headers. Body-preserving 308 redirects are
# flaky across HTTP clients (some downgrade POST to GET), so we internally
# delegate and return the same JSON payload the /halt route returns, plus
# an additional WARN log on the deprecated structlog event.
@app.post(
    "/api/agents/{agent_id}/kill",
    dependencies=[Depends(authenticate), require_role("admin")],
)
async def kill_agent(
    agent_id: str, body: HaltRequest, request: Request
) -> JSONResponse:
    """Deprecated alias of ``POST /api/agents/{agent_id}/halt``.

    Preserved for one release (v0.6) so v0.5.x clients keep working while
    they upgrade. Removed in v0.7. Emits a deprecated structlog event
    ``console.agent_killed`` at WARN level alongside the new
    ``console.agent_halted`` event and attaches ``Deprecation: true`` and
    ``Sunset: Wed, 15 Oct 2026 00:00:00 GMT`` response headers.
    """
    _logger.warning(
        "console.agent_killed",
        agent_id=agent_id,
        msg="deprecated event kind; use console.agent_halted.",
    )
    payload = await halt_agent(agent_id, body, request)
    return JSONResponse(
        content=payload,
        headers={
            "Deprecation": "true",
            "Sunset": "Wed, 15 Oct 2026 00:00:00 GMT",
            "Link": '</api/agents/{agent_id}/halt>; rel="successor-version"',
        },
    )


# ---------------------------------------------------------------------------
# New endpoint: Event stats
# ---------------------------------------------------------------------------
@app.get(
    "/api/events/stats",
    dependencies=[Depends(authenticate), Depends(rate_limit_per_user)],
    response_model=EventStatsResponse,
)
async def event_stats() -> EventStatsResponse:
    """Event counts for the last hour, grouped by kind.

    Returns total_last_hour, per_kind_counts, events_per_minute (rolling 5 min).
    Query timeout: 2 s to prevent slow COUNT queries from blocking the pool.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET LOCAL statement_timeout = '2000'"))
            res = await conn.execute(
                text(
                    "SELECT kind, COUNT(*) AS cnt "
                    "FROM governance_audit_events "
                    "WHERE created_at >= NOW() - INTERVAL '1 hour' "
                    "GROUP BY kind"
                )
            )
            per_kind: dict[str, int] = {
                row["kind"]: int(row["cnt"]) for row in res.mappings()
            }
            res2 = await conn.execute(
                text(
                    "SELECT COUNT(*) AS cnt "
                    "FROM governance_audit_events "
                    "WHERE created_at >= NOW() - INTERVAL '5 minutes'"
                )
            )
            five_min_row = res2.first()
            five_min_count = int(five_min_row[0]) if five_min_row else 0
        return EventStatsResponse(
            total_last_hour=sum(per_kind.values()),
            per_kind_counts=per_kind,
            events_per_minute=five_min_count / 5.0,
        )
    except Exception:
        _logger.warning("console.event_stats_failed", exc_info=False)
        raise HTTPException(503, "Event stats temporarily unavailable.") from None

# ---------------------------------------------------------------------------
# New endpoint: Gate rich context
# ---------------------------------------------------------------------------
@app.get(
    "/api/gates/{request_id}/context",
    dependencies=[Depends(authenticate)],
    response_model=GateContextResponse,
)
async def gate_context(request_id: UUID) -> GateContextResponse:
    """Rich context for an approval decision.

    Returns gate record, agent presence, recent agent events (last 10),
    agent cost today, and risk level derived from payload_json.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id, agent_id, kind, action_hash, "
                "created_at, expires_at, resolved_at, resolution, "
                "payload_json, reviewer_id, reviewing_since, rationale "
                "FROM governance_gates_pending WHERE request_id = :rid"
            ),
            {"rid": str(request_id)},
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(404, "Gate request not found.")

        agent_id = row["agent_id"]

        pres_res = await conn.execute(
            text(
                "SELECT agent_id, status, last_heartbeat "
                "FROM governance_agent_presence WHERE agent_id = :aid"
            ),
            {"aid": agent_id},
        )
        pres_row = pres_res.mappings().first()

        events_res = await conn.execute(
            text(
                "SELECT kind, created_at FROM governance_audit_events "
                "WHERE agent_id = :aid ORDER BY created_at DESC LIMIT 10"
            ),
            {"aid": agent_id},
        )
        recent_events = [
            GateRecentEvent(kind=r["kind"], created_at=r["created_at"])
            for r in events_res.mappings()
        ]

        cost_res = await conn.execute(
            text(
                "SELECT usd_used FROM governance_cost_agent_daily "
                "WHERE agent_id = :aid AND day_utc = (NOW() AT TIME ZONE 'UTC')::DATE"
            ),
            {"aid": agent_id},
        )
        cost_row = cost_res.first()

    payload = row["payload_json"] or {}
    risk = payload.get("risk", "UNKNOWN") if isinstance(payload, dict) else "UNKNOWN"

    # Redact by key name (existing) and by value shape (F3).
    redacted_payload = redact_secrets(
        _redact_metadata(payload if isinstance(payload, dict) else {})
    )

    gate_presence: GateAgentPresence | None = None
    if pres_row is not None:
        gate_presence = GateAgentPresence(
            status=pres_row["status"],
            last_heartbeat=pres_row["last_heartbeat"],
        )

    return GateContextResponse(
        request_id=str(row["request_id"]),
        agent_id=agent_id,
        kind=row["kind"],
        action_hash=row["action_hash"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
        reviewer_id=str(row["reviewer_id"]) if row["reviewer_id"] else None,
        reviewing_since=row.get("reviewing_since"),
        rationale=row.get("rationale"),
        payload=redacted_payload,
        risk=risk,
        agent_presence=gate_presence,
        recent_agent_events=recent_events,
        agent_cost_today_usd=float(cost_row[0]) if cost_row else None,
    )


# ---------------------------------------------------------------------------
# New endpoint: Reviewer claim
# ---------------------------------------------------------------------------
@app.post(
    "/api/gates/{request_id}/claim",
    dependencies=[Depends(authenticate)],
    response_model=GateClaimResponse,
)
async def claim_gate(request_id: UUID, request: Request) -> GateClaimResponse:
    """Claim a pending gate for review. Atomic: only succeeds if unclaimed.

    Uses UPDATE ... WHERE reviewer_id IS NULL to prevent double-claim
    across concurrent requests (Security review Item 5).
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        raise HTTPException(403, "Cannot determine reviewer identity.")

    reviewer_uuid: UUID | None = None
    if user_id not in ("dev", "legacy-token"):
        try:
            reviewer_uuid = UUID(user_id)
        except ValueError:
            raise HTTPException(403, "Reviewer identity is not a valid UUID.") from None

    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        check_res = await conn.execute(
            text(
                "SELECT resolved_at, reviewer_id FROM governance_gates_pending "
                "WHERE request_id = :rid"
            ),
            {"rid": str(request_id)},
        )
        check_row = check_res.mappings().first()
        if not check_row:
            raise HTTPException(404, "Gate request not found.")
        if check_row["resolved_at"] is not None:
            raise HTTPException(409, "Gate is already resolved.")

        if reviewer_uuid is not None:
            update_res = await conn.execute(
                text(
                    "UPDATE governance_gates_pending "
                    "SET reviewer_id = :uid, reviewing_since = :now "
                    "WHERE request_id = :rid "
                    "AND reviewer_id IS NULL AND resolved_at IS NULL "
                    "RETURNING request_id"
                ),
                {"uid": str(reviewer_uuid), "now": now, "rid": str(request_id)},
            )
            if not update_res.first():
                raise HTTPException(409, "Gate is already claimed by another reviewer.")

    return GateClaimResponse(
        ok=True,
        request_id=str(request_id),
        reviewer_id=str(reviewer_uuid) if reviewer_uuid else user_id,
        reviewing_since=now,
    )

# ---------------------------------------------------------------------------
# New endpoint: Escalate gate
# ---------------------------------------------------------------------------
class EscalateRequest(BaseModel):
    """Escalate gate request body."""

    model_config = ConfigDict(strict=True)

    escalate_to: str = Field(
        min_length=1, max_length=256, description="User ID or role to escalate to."
    )


@app.post(
    "/api/gates/{request_id}/escalate",
    dependencies=[Depends(authenticate)],
    response_model=GateEscalateResponse,
)
async def escalate_gate(
    request_id: UUID, body: EscalateRequest, request: Request
) -> GateEscalateResponse:
    """Escalate a pending gate to another reviewer.

    Releases the current claim and records escalation metadata in payload_json.
    The resulting UPDATE fires gate_change_notify, broadcasting to SSE clients.

    v0.6.1 S3 P1 #1 — claimant check: a gate claimed by reviewer A can
    only be escalated by reviewer A or by an admin. Without this check
    any authenticated user (including viewers) could iterate pending
    gates and release every active claim, continuously griefing the
    admin review workflow. An audit row ``gates.escalated`` records who
    did the escalation and the reviewer_id that was released.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    user_id = getattr(request.state, "user_id", "unknown")
    user_role = getattr(request.state, "role", None)
    now = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id, agent_id, kind, resolved_at, "
                "payload_json, reviewer_id "
                "FROM governance_gates_pending WHERE request_id = :rid"
            ),
            {"rid": str(request_id)},
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(404, "Gate request not found.")
        if row["resolved_at"] is not None:
            raise HTTPException(409, "Gate is already resolved.")

        # Claimant check. If the gate is claimed by someone OTHER than
        # the caller, only an admin can release the claim. An unclaimed
        # gate (reviewer_id IS NULL) preserves the previous behavior:
        # any authenticated user can escalate it.
        reviewer_id = row.get("reviewer_id")
        if (
            reviewer_id is not None
            and str(reviewer_id) != str(user_id)
            and user_role != "admin"
        ):
            raise HTTPException(
                403,
                "Only the current reviewer or an admin can escalate a "
                "claimed gate.",
            )

        reviewer_id_before = str(reviewer_id) if reviewer_id else None

        existing_payload = row["payload_json"] or {}
        if not isinstance(existing_payload, dict):
            existing_payload = {}
        existing_payload["escalated_by"] = user_id
        existing_payload["escalated_to"] = body.escalate_to
        existing_payload["escalated_at"] = now.isoformat()

        # v0.6.1 DA P1: close the TOCTOU between the SELECT-for-authz and the
        # UPDATE. At default READ COMMITTED isolation a second writer could
        # mutate reviewer_id between the two statements — the authz check
        # would pass on stale state. We pin the UPDATE to the reviewer_id we
        # read (``IS NOT DISTINCT FROM`` handles the NULL-unclaimed case) and
        # use RETURNING to detect the race. A lost race → 409, NOT silent
        # success + a bogus gates.escalated audit row.
        import json as _json
        update_res = await conn.execute(
            text(
                "UPDATE governance_gates_pending "
                "SET payload_json = :payload::jsonb, "
                "    reviewer_id = NULL, reviewing_since = NULL "
                "WHERE request_id = :rid AND resolved_at IS NULL "
                "AND reviewer_id IS NOT DISTINCT FROM :expected_reviewer "
                "RETURNING request_id"
            ),
            {
                "payload": _json.dumps(existing_payload),
                "rid": str(request_id),
                "expected_reviewer": str(reviewer_id) if reviewer_id else None,
            },
        )
        if update_res.first() is None:
            raise HTTPException(
                409,
                "Gate state changed during escalation; retry.",
            )

    # Audit row — best-effort. A failed audit write MUST NOT fail the
    # escalation (consistent with grant_gate/deny_gate).
    if audit_module is not None:
        try:
            # TODO(v0.6.2): MD5 used only as a deterministic 128-bit UUID
            # derivation from ``request_id``; not security-sensitive here
            # (no collision-resistance requirement — request_id is already
            # the primary key). Migrate to UUID5(namespace, request_id)
            # for codebase-wide "no MD5" hygiene.
            session_id = UUID(
                hashlib.md5(str(request_id).encode("utf-8")).hexdigest()
            )
            await audit_module.log(
                AuditEvent(
                    agent_id=row["agent_id"],
                    session_id=session_id,
                    kind="gates.escalated",
                    metadata={
                        "request_id": str(request_id),
                        "gate_kind": row["kind"],
                        "reviewer_id_before": reviewer_id_before,
                        "reviewer_id_after": None,
                        "by_user_id": str(user_id),
                        "role": user_role,
                        "escalated_to": body.escalate_to,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 — audit MUST NOT break the API
            _logger.warning(
                "console.gate_escalated_audit_failed",
                request_id=str(request_id),
                error_type=type(exc).__name__,
            )

    _logger.info(
        "console.gate_escalated",
        request_id=str(request_id),
        escalated_by=user_id,
        escalated_to=body.escalate_to,
        reviewer_id_before=reviewer_id_before,
        role=user_role,
    )
    return GateEscalateResponse(
        ok=True,
        request_id=str(request_id),
        escalated_to=body.escalate_to,
        escalated_at=now,
    )


# ---------------------------------------------------------------------------
# New endpoint: Batch approve
# ---------------------------------------------------------------------------
_BATCH_APPROVE_MAX = 50


class BatchApproveRequest(BaseModel):
    """Batch approve request body."""

    model_config = ConfigDict(strict=True)

    request_ids: list[UUID] = Field(
        min_length=1,
        max_length=_BATCH_APPROVE_MAX,
        description=f"Gate request IDs to approve. Hard cap: {_BATCH_APPROVE_MAX}.",
    )


@app.post(
    "/api/gates/batch-approve",
    dependencies=[Depends(authenticate), require_role("admin")],
    response_model=BatchApproveResponse,
)
async def batch_approve(
    body: BatchApproveRequest, request: Request
) -> BatchApproveResponse:
    """Batch approve up to 50 LOW-risk gates. Admin only.

    Security: hard cap 50, server-side risk re-verify, individual audit events,
    self-approval prevention, claim enforcement per gate.
    """
    if len(body.request_ids) > _BATCH_APPROVE_MAX:
        raise HTTPException(400, f"Batch size cannot exceed {_BATCH_APPROVE_MAX}.")
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    if not AUDIT_SECRET:
        raise HTTPException(500, "GOVERNANCE_AUDIT_SECRET env var required for gate operations.")

    user_id = getattr(request.state, "user_id", None)
    approved: list[str] = []
    failed: list[BatchApproveFailure] = []

    for rid in body.request_ids:
        try:
            async with engine.begin() as conn:
                res = await conn.execute(
                    text(
                        "SELECT request_id, agent_id, kind, "
                        "payload_json, resolved_at, reviewer_id "
                        "FROM governance_gates_pending WHERE request_id = :rid"
                    ),
                    {"rid": str(rid)},
                )
                row = res.mappings().first()
                if not row:
                    failed.append(BatchApproveFailure(request_id=str(rid), reason="not_found"))
                    continue
                if row["resolved_at"] is not None:
                    failed.append(
                        BatchApproveFailure(request_id=str(rid), reason="already_resolved")
                    )
                    continue

                payload = row["payload_json"] or {}
                risk = payload.get("risk", "UNKNOWN") if isinstance(payload, dict) else "UNKNOWN"
                if risk != "LOW":
                    failed.append(
                        BatchApproveFailure(
                            request_id=str(rid),
                            reason=f"risk_{risk}_not_eligible_for_batch",
                        )
                    )
                    continue

                if user_id is not None:
                    try:
                        await _check_self_approval(conn, row["agent_id"], user_id, rid)
                    except HTTPException:
                        failed.append(
                            BatchApproveFailure(
                                request_id=str(rid), reason="self_approval_blocked"
                            )
                        )
                        continue

                reviewer_id = row.get("reviewer_id")
                if reviewer_id is not None and user_id is not None:
                    if str(reviewer_id) != user_id:
                        failed.append(
                            BatchApproveFailure(
                                request_id=str(rid), reason="claimed_by_other_reviewer"
                            )
                        )
                        continue

                now = datetime.now(timezone.utc)
                await conn.execute(
                    text(
                        "UPDATE governance_gates_pending "
                        "SET resolved_at = :now, resolution = 'granted' "
                        "WHERE request_id = :rid AND resolved_at IS NULL"
                    ),
                    {"now": now, "rid": str(rid)},
                )

            if audit_module is not None:
                session_id = UUID(hashlib.md5(str(rid).encode("utf-8")).hexdigest())
                await audit_module.log(
                    AuditEvent(
                        agent_id=row["agent_id"],
                        session_id=session_id,
                        kind="approval.granted.batch",
                        metadata={
                            "request_id": str(rid),
                            "gate_kind": row["kind"],
                            "approved_by": user_id,
                        },
                    )
                )

            approved.append(str(rid))

        except HTTPException:
            raise
        except Exception as exc:
            _logger.warning(
                "console.batch_approve_item_failed",
                request_id=str(rid),
                exc_type=type(exc).__name__,
            )
            failed.append(
                BatchApproveFailure(request_id=str(rid), reason="internal_error")
            )

    return BatchApproveResponse(
        ok=True,
        approved=approved,
        failed=failed,
        approved_count=len(approved),
        failed_count=len(failed),
    )


# ---------------------------------------------------------------------------
# F2 P0: single-event fetch for SSE hydration
# ---------------------------------------------------------------------------
# Common secret-bearing patterns to strip from any string value before we
# serialize it back to the console. This is a belt-and-suspenders layer on
# top of `_redact_metadata` (which redacts by KEY NAME) — here we redact by
# VALUE SHAPE so a stray prefix that lands under a non-obvious key name
# (e.g. `notes="here's my sk-ant-abc123"`) is still caught.
_SECRET_VALUE_RE = re.compile(
    r"("
    r"sk-ant-[A-Za-z0-9_\-]{10,}"   # Anthropic
    r"|sk-[A-Za-z0-9]{20,}"          # OpenAI
    r"|xoxb-[A-Za-z0-9\-]{10,}"      # Slack bot token
    r"|gh[ps]_[A-Za-z0-9]{20,}"      # GitHub PAT
    r"|AKIA[0-9A-Z]{16}"             # AWS access key
    r")"
)


def _scrub_secrets(value: Any) -> Any:
    """Recursively replace secret-shaped substrings with ``[REDACTED]``.

    Runs on dict values and list items; non-str leaves pass through.
    """
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub("[REDACTED]", value)
    if isinstance(value, dict):
        return {k: _scrub_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_secrets(item) for item in value]
    return value


def _user_can_access_agent(role: str, user_id: str, agent_id: str) -> bool:
    """BLOCKER C2: viewer-scoped allowlist check.

    Admin and reviewer roles can access any agent. Viewer is gated on
    the ``GOVERNANCE_CONSOLE_SCOPE_VIEWERS_BY_AGENT=true`` env var:

      * env var unset (default): permissive — viewers see all agents,
        preserving existing single-tenant deployments.
      * env var set: viewers see only agents listed in the
        ``governance_console_user_agents`` join table (when present).

    The full allowlist join table lands in v0.6.1; for v0.6 the check is
    deny-on-no-row-found, allow-on-permissive-default. Documented in the
    blocker writeup as a migration path.
    """
    if role in {"admin", "reviewer"}:
        return True
    scope_viewers = (
        os.environ.get("GOVERNANCE_CONSOLE_SCOPE_VIEWERS_BY_AGENT", "")
        .lower() == "true"
    )
    if not scope_viewers:
        return True
    # Strict mode: deny by default until the v0.6.1 join table is wired.
    return False


from .models.responses import AuditEventView as _AuditEventView_for_route  # noqa: E402  # late-bound alias used only by /api/events/{id} route below


@app.get(
    "/api/events/{event_id}",
    dependencies=[Depends(authenticate)],
    response_model=_AuditEventView_for_route,
)
async def get_audit_event(event_id: str, request: Request) -> dict[str, Any]:
    """Return a single audit-event row for SSE stream hydration.

    The NOTIFY payload emitted by the governance_audit_events trigger
    carries only a minimal envelope (event_id, agent_id, kind, chain_seq,
    created_at) to keep WAL small. The frontend calls this endpoint on
    first access of a stream row to lazy-hydrate the remaining fields.

    Returns 404 if the event does not exist. The response passes through
    both the key-based `_redact_metadata` filter and the value-shape
    `_scrub_secrets` filter before serialization.

    The response shape matches
    :class:`codeatelier_governance.console.models.responses.AuditEventView`.
    That model enforces ``extra='forbid'`` per F3; callers that bypass it
    still get the same field set because this function constructs the dict
    explicitly.
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")

    # Validate event_id is a UUID; refuse anything else up-front so the SQL
    # layer never sees hand-rolled values. Prevents parser-level surprises
    # on non-UUID input and avoids leaking SQLSTATE errors to the client.
    try:
        parsed_id = UUID(event_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "Invalid event_id") from exc

    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT event_id, chain_seq, agent_id, kind, model, "
                "metadata_json, hmac_value, prev_hash, created_at "
                "FROM governance_audit_events "
                "WHERE event_id = :eid"
            ),
            {"eid": str(parsed_id)},
        )
        row = res.mappings().first()

    if row is None:
        raise HTTPException(404, "Event not found")

    # BLOCKER C2: tenant isolation. A viewer (or any non-admin/non-reviewer
    # role) may only fetch events for agents in their accessible list. We
    # collapse the cross-tenant denial into 404 to avoid leaking event-id
    # existence to a caller who shouldn't see it.
    user_id = getattr(request.state, "user_id", "unknown")
    role = getattr(request.state, "role", "viewer")
    if not _user_can_access_agent(role, user_id, str(row["agent_id"])):
        raise HTTPException(404, "Event not found")

    # BLOCKER C2: emit an audit-of-audits event. Anyone reading sensitive
    # audit data leaves a tamper-evident trail of their read. Pseudonymize
    # the user_id with a stable hash so it does not duplicate PII into the
    # audit chain.
    if audit_module is not None:
        try:
            pseudonymous_user = hashlib.sha256(
                f"audit-fetch:{user_id}".encode("utf-8")
            ).hexdigest()[:16]
            audit_session = UUID(
                hashlib.md5(
                    (
                        "audit-fetch:" + str(parsed_id) + ":"
                        + datetime.now(timezone.utc).isoformat()
                    ).encode()
                ).hexdigest()
            )
            await audit_module.log(
                AuditEvent(
                    agent_id=str(row["agent_id"]),
                    session_id=audit_session,
                    kind="pipeline.audit_event_fetched",
                    metadata={
                        "fetched_by": pseudonymous_user,
                        "fetched_event_id": str(parsed_id),
                        "role": role,
                    },
                )
            )
        except Exception:  # noqa: BLE001 — audit failures must not block reads
            _logger.warning(
                "console.audit_event_fetch_log_failed", exc_info=False
            )

    raw_meta = row["metadata_json"] or {}
    # Two-pass scrub: key-name redaction, then value-shape redaction.
    scrubbed_meta = _scrub_secrets(_redact_metadata(raw_meta))

    # `tool` and `request_id` live inside metadata for current schema; surface
    # them at the top level for the typed response, but keep them as str-or-None.
    meta_dict = scrubbed_meta if isinstance(scrubbed_meta, dict) else {}
    tool_val = meta_dict.get("tool")
    req_id_val = meta_dict.get("request_id")

    return {
        "event_id": str(row["event_id"]),
        "chain_seq": int(row["chain_seq"]),
        "agent_id": row["agent_id"],
        "kind": row["kind"],
        "model": row["model"],
        "tool": tool_val if isinstance(tool_val, str) else None,
        "request_id": req_id_val if isinstance(req_id_val, str) else None,
        "metadata": meta_dict,
        "hmac_value": row["hmac_value"],
        "prev_hash": row["prev_hash"],
        "created_at": row["created_at"] if row["created_at"] else None,
    }


# ---------------------------------------------------------------------------
# F9 Wrapper Coverage
# ---------------------------------------------------------------------------
@app.get("/api/coverage", dependencies=[Depends(authenticate)])
async def wrapper_coverage(
    agent_id: str | None = Query(default=None, max_length=256),
    active_window_days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """Return F9 wrapper coverage snapshot.

    Shape matches ``console.models.responses.WrapperCoverageView``.

    Returns 503 when the governance DB is unreachable — the cross-process
    mirror is the entire point of this endpoint, so answering from
    in-memory state alone would be misleading.

    Returns 200 with ``coverage_pct: null`` and
    ``coverage_pct_reason: "no_scope_policies_registered"`` when the
    denominator is zero — explicit discriminator per the F9 design doc
    (DA blocker fix #1).
    """
    if engine is None:
        raise HTTPException(
            503,
            "Console backend is starting up or the governance DB is "
            "unreachable. The wrapper coverage view requires Postgres.",
        )

    async with engine.connect() as conn:
        # Denominator: distinct agent_ids with a scope policy declared.
        total_res = await conn.execute(
            text(
                "SELECT COUNT(DISTINCT agent_id) FROM governance_policies "
                "WHERE policy_type = 'scope'"
            )
        )
        total_row = total_res.first()
        total_wrappers = int(total_row[0]) if total_row and total_row[0] else 0

        window = f"{active_window_days} days"
        params: dict[str, Any] = {"window": window}
        agent_where = ""
        if agent_id is not None:
            agent_where = " AND agent_id = :agent_id"
            params["agent_id"] = agent_id

        # Active wrappers: distinct agents with a heartbeat in the window.
        active_res = await conn.execute(
            text(
                "SELECT COUNT(DISTINCT agent_id) FROM "
                "governance_wrapper_registrations "
                "WHERE last_seen_at >= NOW() - CAST(:window AS INTERVAL)"
                f"{agent_where}"
            ),
            params,
        )
        active_row = active_res.first()
        active_wrappers = (
            int(active_row[0]) if active_row and active_row[0] else 0
        )

        # by_agent breakdown: one row per (agent_id, provider) with the
        # most recent heartbeat.
        by_agent_res = await conn.execute(
            text(
                "SELECT agent_id, provider, "
                "MAX(last_seen_at) AS last_seen_at, "
                "BOOL_OR(last_seen_at >= NOW() - CAST(:window AS INTERVAL)) "
                "AS active "
                "FROM governance_wrapper_registrations "
                + (
                    "WHERE agent_id = :agent_id "
                    if agent_id is not None
                    else ""
                )
                + "GROUP BY agent_id, provider "
                "ORDER BY agent_id, provider"
            ),
            params,
        )
        by_agent = [
            {
                "agent_id": row["agent_id"],
                "provider": row["provider"],
                "active": bool(row["active"]),
                "last_seen_at": (
                    row["last_seen_at"].isoformat()
                    if row["last_seen_at"]
                    else None
                ),
            }
            for row in by_agent_res.mappings()
        ]

        # Unwrapped agents: seen in recent audit events but not in the
        # registry. Helpful for operator drill-down.
        unwrapped_res = await conn.execute(
            text(
                "SELECT DISTINCT e.agent_id "
                "FROM governance_audit_events e "
                "WHERE e.created_at >= NOW() - CAST(:window AS INTERVAL) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM governance_wrapper_registrations r "
                "  WHERE r.agent_id = e.agent_id "
                "  AND r.last_seen_at >= NOW() - CAST(:window AS INTERVAL)"
                ") "
                "ORDER BY e.agent_id LIMIT 200"
            ),
            {"window": window},
        )
        unwrapped = [r["agent_id"] for r in unwrapped_res.mappings()]

    now = datetime.now(timezone.utc)

    if total_wrappers == 0:
        return {
            "as_of": now.isoformat(),
            "active_wrappers": active_wrappers,
            "total_wrappers": 0,
            "coverage_pct": None,
            "coverage_pct_reason": "no_scope_policies_registered",
            "by_agent": by_agent,
            "unwrapped_agents_seen_in_audit": unwrapped,
            "active_window_days": active_window_days,
        }

    if agent_id is not None:
        coverage_pct: float | None = 1.0 if active_wrappers > 0 else 0.0
    else:
        coverage_pct = min(1.0, active_wrappers / total_wrappers)

    return {
        "as_of": now.isoformat(),
        "active_wrappers": active_wrappers,
        "total_wrappers": total_wrappers,
        "coverage_pct": coverage_pct,
        "coverage_pct_reason": "ok",
        "by_agent": by_agent,
        "unwrapped_agents_seen_in_audit": unwrapped,
        "active_window_days": active_window_days,
    }


# ---------------------------------------------------------------------------
# F7 Governance Health Endpoint
# ---------------------------------------------------------------------------
#
# Two response shapes on a single path (``GET /health/governance``):
#
# * Unauthenticated (K8s liveness, anonymous probes): returns the minimal
#   ``{"status": "ok"}`` and nothing else. Per the Cybersec MED ruling in
#   the F7 review, latency numbers and chain-integrity signals leak load
#   patterns and operational state to anyone who can hit the pod, so we
#   hard-cap the anonymous response surface.
# * Authenticated (valid session cookie, legacy bearer token, or dev
#   mode): returns the full ``GovernanceHealthView`` with DB reachability,
#   last chain verification timestamp, append-only grant status, audit
#   write latency percentiles, chain integrity state, and the
#   resolved/unresolved HMAC key fingerprint lists. The ``unresolved``
#   list is the LOUD signal that F6 Track B deferred to F7.
#
# Auth detection is opt-in: we do NOT use the ``authenticate`` dependency
# because this endpoint must answer ``200 {"status": "ok"}`` to unauthed
# callers (K8s liveness compatibility). Instead we inline a lightweight
# session lookup that mirrors ``authenticate`` but returns ``None`` on any
# failure instead of raising 401.
async def _health_authenticated(request: Request) -> bool:
    """Return True iff the request carries valid session/token/dev auth.

    Must not raise. Any failure (bad cookie, expired session, DB down,
    missing token) returns ``False`` — the caller will fall through to
    the unauthenticated minimal response shape. This intentionally does
    NOT mutate ``request.state``; the health endpoint does not need
    ``user_id`` or ``role`` for its branch decision.
    """
    if DEV_MODE:
        return True

    session_cookie = request.cookies.get("governance_session")
    if session_cookie and engine is not None:
        try:
            sid = UUID(session_cookie)
        except ValueError:
            return False
        try:
            async with engine.connect() as conn:
                res = await conn.execute(
                    text(
                        "SELECT s.expires_at, s.revoked "
                        "FROM governance_console_sessions s "
                        "JOIN governance_console_users u "
                        "  ON s.user_id = u.user_id "
                        "WHERE s.session_id = :sid "
                        "AND u.disabled = FALSE"
                    ),
                    {"sid": str(sid)},
                )
                row = res.mappings().first()
        except Exception:
            return False
        if row is None or row["revoked"]:
            return False
        expires = row["expires_at"]
        if expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
            return False
        return True

    auth_header = request.headers.get("Authorization", "")
    if CONSOLE_TOKEN and auth_header == f"Bearer {CONSOLE_TOKEN}":
        return True

    return False


@app.get("/health/governance")
async def governance_health(request: Request) -> dict[str, Any]:
    """Governance health endpoint with two response shapes.

    Unauthenticated callers receive ``{"status": "ok"}`` only — K8s
    liveness probes and anonymous monitors get a liveness signal without
    leaking internal state. Authenticated callers receive the full
    ``GovernanceHealthView`` shape.

    The authenticated view reports DB reachability, last chain
    verification timestamp, append-only grant status, audit write
    latency percentiles, chain integrity state, and the resolved /
    unresolved HMAC key fingerprint lists. A non-empty
    ``chain_keys_unresolved`` list is the LOUD missing-key signal
    deferred from F6 Track B.
    """
    # Local import to avoid colliding with F3's in-place edits to the
    # top-level ``from .models.responses import (...)`` block.
    from .models.responses import GovernanceHealthView

    authed = await _health_authenticated(request)
    if not authed:
        # Minimal shape — K8s liveness only. Do NOT add fields here.
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Authenticated shape. Each probe is wrapped so a single failure
    # cannot knock the whole endpoint offline: if we cannot reach the DB
    # we still return a structured response with ``db_reachable: False``
    # and ``chain_integrity_status: "unverified"``.
    # ------------------------------------------------------------------
    db_reachable = False
    append_only_grants_ok = False
    last_chain_verify_ts: datetime | None = None
    chain_integrity_status = "unverified"
    chain_keys_resolved: list[str] = []
    chain_keys_unresolved: list[str] = []
    audit_write_p50_ms: float | None = None
    audit_write_p95_ms: float | None = None

    if engine is not None:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                db_reachable = True

                # Append-only grants check: confirm that UPDATE/DELETE
                # privileges on the audit table are not granted to the
                # current application role. Defence-in-depth probe —
                # the migration revokes these grants, and this endpoint
                # surfaces drift.
                try:
                    grants_res = await conn.execute(
                        text(
                            "SELECT COUNT(*) FROM "
                            "information_schema.table_privileges "
                            "WHERE table_name = 'governance_audit_events' "
                            "AND privilege_type IN ('UPDATE', 'DELETE') "
                            "AND grantee = CURRENT_USER"
                        )
                    )
                    grants_row = grants_res.first()
                    append_only_grants_ok = (
                        grants_row is not None and int(grants_row[0]) == 0
                    )
                except Exception:
                    append_only_grants_ok = False
        except Exception:
            db_reachable = False

    # Audit module: last verify ts, chain integrity, key fingerprints,
    # latency histogram. All reads are best-effort — a missing attribute
    # means the feature is disabled or still being wired by #19 Track A,
    # not a bug.
    if audit_module is not None:
        try:
            last_chain_verify_ts = getattr(
                audit_module, "last_chain_verify_ts", None
            )
            chain_integrity_status = getattr(
                audit_module, "chain_integrity_status", "unverified"
            )
            chain_keys_resolved = list(
                getattr(audit_module, "chain_keys_resolved", []) or []
            )
            chain_keys_unresolved = list(
                getattr(audit_module, "chain_keys_unresolved", []) or []
            )
            audit_write_p50_ms = getattr(
                audit_module, "write_latency_p50_ms", None
            )
            audit_write_p95_ms = getattr(
                audit_module, "write_latency_p95_ms", None
            )
        except Exception:
            pass

    view = GovernanceHealthView(
        status="ok" if db_reachable else "degraded",
        db_reachable=db_reachable,
        last_chain_verify_ts=last_chain_verify_ts,
        append_only_grants_ok=append_only_grants_ok,
        audit_write_p50_ms=audit_write_p50_ms,
        audit_write_p95_ms=audit_write_p95_ms,
        chain_integrity_status=chain_integrity_status,
        chain_keys_resolved=chain_keys_resolved,
        chain_keys_unresolved=chain_keys_unresolved,
    )
    return view.model_dump(mode="json")


# ---------------------------------------------------------------------------
# F4 Compliance Console Surface
# ---------------------------------------------------------------------------
# Two endpoints back the persistent header pill and the full compliance
# page in the v4 console:
#
#   GET  /api/compliance/report         — returns the Article 12 summary
#   POST /api/compliance/verify-chain   — runs a window-limited HMAC verify
#
# Both share a semaphore (max 2 concurrent) and a 30-second short-circuit
# cache keyed on the verification window. The cache prevents a thundering
# herd when a tab auto-refreshes the header pill while an analyst clicks
# the compliance page; the semaphore prevents a single misbehaving client
# from pinning the chain verifier. Rate limit is imposed at 1 req / 60 s
# per authenticated user — compliance calls are expensive (O(n) over the
# verify window) and should not be treated as cheap polling endpoints.
# ---------------------------------------------------------------------------
_COMPLIANCE_RATE_LIMIT_WINDOW_SECONDS = 60
_COMPLIANCE_RATE_LIMIT_MAX = int(
    os.environ.get("GOVERNANCE_COMPLIANCE_RATE_LIMIT", "1")
)
_compliance_user_times: dict[str, list[float]] = {}

_COMPLIANCE_SEMAPHORE = asyncio.Semaphore(2)
# BLOCKER C3: anonymous callers (no authenticated user_id) share a SINGLE
# slot regardless of request rate. Stops a flood of unauthenticated chain
# verifies from pinning both general slots.
_COMPLIANCE_ANON_SEMAPHORE = asyncio.Semaphore(1)
# BLOCKER C3: anonymous calls share a single global rate-limit bucket
# (not per-user, since there is no user). 1 call per 300 s globally.
_COMPLIANCE_ANON_RATE_WINDOW_SECONDS = 300
_COMPLIANCE_ANON_RATE_MAX = 1
_compliance_anon_times: list[float] = []
# BLOCKER C3: SET LOCAL statement_timeout for chain verification calls.
# 30 s matches the cache TTL — a verify that takes longer is failing
# the cache anyway, and pinning a pool slot for minutes is unacceptable.
_COMPLIANCE_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("GOVERNANCE_CONSOLE_COMPLIANCE_STATEMENT_TIMEOUT_MS", "30000")
)

_COMPLIANCE_CACHE_TTL_SECONDS = 30.0
# Cache key -> (inserted_at_monotonic, payload_dict)
_compliance_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _compliance_rate_limit_check(user_id: str) -> float | None:
    """Sliding-window per-user rate limit for the F4 compliance endpoints.

    Returns ``retry_after`` (seconds) when the caller is over quota, else
    ``None``. Dedicated from the shared ``_check_user_rate_limit`` pool
    so normal polling endpoints don't exhaust the compliance quota and
    vice versa.
    """
    now = time.monotonic()
    times = _compliance_user_times.get(user_id, [])
    times = [
        t for t in times if now - t < _COMPLIANCE_RATE_LIMIT_WINDOW_SECONDS
    ]
    _compliance_user_times[user_id] = times
    if len(times) >= _COMPLIANCE_RATE_LIMIT_MAX:
        oldest = times[0]
        retry = _COMPLIANCE_RATE_LIMIT_WINDOW_SECONDS - (now - oldest)
        return max(1.0, retry)
    times.append(now)
    _compliance_user_times[user_id] = times
    return None


async def _compliance_rate_limit_dep(request: Request) -> None:
    """FastAPI dependency: 1 req/60 s/user for F4 compliance endpoints.

    BLOCKER C3: anonymous callers (no authenticated user_id) used to early-
    return and bypass the limit entirely. They now share a SINGLE global
    bucket capped at 1 call per 300 s. Authenticated callers continue on
    the per-user 1 req / 60 s sliding window.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        # BLOCKER C3: anonymous global bucket.
        now = time.monotonic()
        global _compliance_anon_times
        _compliance_anon_times = [
            t
            for t in _compliance_anon_times
            if now - t < _COMPLIANCE_ANON_RATE_WINDOW_SECONDS
        ]
        if len(_compliance_anon_times) >= _COMPLIANCE_ANON_RATE_MAX:
            oldest = _compliance_anon_times[0]
            retry = max(
                1.0,
                _COMPLIANCE_ANON_RATE_WINDOW_SECONDS - (now - oldest),
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    "Anonymous compliance rate limit exceeded. "
                    "Retry in 300 s."
                ),
                headers={"Retry-After": str(int(retry))},
            )
        _compliance_anon_times.append(now)
        return
    retry_after = _compliance_rate_limit_check(user_id)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Compliance rate limit exceeded. Retry in 60 s.",
            headers={"Retry-After": str(int(retry_after))},
        )


def _compliance_cache_get(key: str) -> dict[str, Any] | None:
    """Return a cached payload for ``key`` if it is still within TTL."""
    entry = _compliance_cache.get(key)
    if entry is None:
        return None
    inserted_at, payload = entry
    if time.monotonic() - inserted_at > _COMPLIANCE_CACHE_TTL_SECONDS:
        _compliance_cache.pop(key, None)
        return None
    return payload


def _compliance_cache_put(key: str, payload: dict[str, Any]) -> None:
    """Insert ``payload`` into the short-circuit cache under ``key``."""
    _compliance_cache[key] = (time.monotonic(), payload)
    # Opportunistic GC: cache holds at most a handful of keys under
    # normal operation, but a buggy client could still flood it.
    if len(_compliance_cache) > 64:
        now = time.monotonic()
        stale = [
            k
            for k, (ts, _) in _compliance_cache.items()
            if now - ts > _COMPLIANCE_CACHE_TTL_SECONDS
        ]
        for k in stale:
            _compliance_cache.pop(k, None)


def _round_to_minute(ts: datetime) -> datetime:
    """Round a datetime down to the minute.

    Cybersec MED (F4): timestamp precision at the millisecond level
    leaks a scraping signal. Every F4 surface exposes chain-verify
    timestamps at minute granularity only.
    """
    return ts.replace(second=0, microsecond=0)


def _map_chain_status(raw: str) -> str:
    """Map backend chain status strings to the F4 surface vocabulary.

    The backend returns ``"verified" | "unverified" | "failed"``. The
    F4 surface exposes ``"verified" | "unverified" | "degraded" | "halted"``
    to align with the governance-health vocabulary. ``"failed"`` becomes
    ``"halted"``; everything else passes through.
    """
    if raw == "failed":
        return "halted"
    if raw in {"verified", "unverified", "degraded", "halted"}:
        return raw
    return "unverified"


@asynccontextmanager
async def _compliance_concurrency_guard(request: Request) -> Any:
    """BLOCKER C3: layered concurrency guard for compliance endpoints.

    Acquires the global compliance semaphore (max 2 concurrent verifies).
    For anonymous callers, ALSO acquires the anonymous-only semaphore
    (max 1 concurrent), guaranteeing that even a flood of unauthenticated
    callers cannot starve authenticated traffic.
    """
    user_id = getattr(request.state, "user_id", None)
    if not user_id:
        async with _COMPLIANCE_ANON_SEMAPHORE:
            async with _COMPLIANCE_SEMAPHORE:
                yield
    else:
        async with _COMPLIANCE_SEMAPHORE:
            yield


@asynccontextmanager
async def _compliance_statement_timeout(conn: Any) -> Any:
    """BLOCKER C3: SET LOCAL statement_timeout for chain verify queries.

    A million-row chain otherwise pins a pool slot for minutes. Same
    pattern as the F4 ``event_stats`` endpoint, but at the configured
    compliance timeout (default 30 s, env-overridable).
    """
    await conn.execute(
        text(f"SET LOCAL statement_timeout = '{_COMPLIANCE_STATEMENT_TIMEOUT_MS}'")
    )
    yield


def _build_report_generator() -> Any:
    """Construct a :class:`ReportGenerator` wired to the current backend.

    Prefers the live ``audit_module`` (so chain verification is available)
    and falls back to the configured ``DATABASE_URL``. Returns ``None``
    when neither is available — callers map that to a 503.
    """
    from ..compliance.report import ReportGenerator

    if audit_module is not None:
        return ReportGenerator(
            database_url=DATABASE_URL or None,
            audit_module=audit_module,
        )
    if DATABASE_URL:
        return ReportGenerator(database_url=DATABASE_URL)
    return None


async def _compliance_report_body(
    request: Request,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    cache_key: str | None = "article12",
) -> "ComplianceReportView":
    """Pure helper: generate an Article 12 report view.

    Shared by the public ``GET /api/compliance/report`` handler and the
    ``POST /api/compliance/export`` bundle endpoint so the two code
    paths cannot drift. When ``cache_key`` is ``None`` the short-circuit
    cache is bypassed (bundle exports want a fresh report scoped to a
    caller-supplied window).

    Returns the strongly-typed :class:`ComplianceReportView`; callers
    that need the JSON shape run ``.model_dump(mode="json")`` once.
    """
    from .models.responses import ComplianceReportView

    if cache_key is not None:
        cached = _compliance_cache_get(cache_key)
        if cached is not None:
            # Cache stores the already-dumped dict; re-validate so the
            # returned view has the correct types (UUID, datetime) — NOT
            # ``model_construct`` which would hand back a view whose
            # ``report_id`` is a ``str``, breaking downstream invariants.
            # strict=False to accept ISO-string datetimes that mode="json"
            # dumped into the cache; the model itself is strict=True which
            # would otherwise reject string-to-datetime coercion.
            return ComplianceReportView.model_validate(cached, strict=False)

    generator = _build_report_generator()
    if generator is None:
        raise HTTPException(
            503,
            "Compliance reports unavailable: no audit backend configured.",
        )

    try:
        async with _compliance_concurrency_guard(request):
            # Double-check the cache after acquiring the semaphore so a
            # burst of callers funnels onto one verify.
            if cache_key is not None:
                cached = _compliance_cache_get(cache_key)
                if cached is not None:
                    return ComplianceReportView.model_validate(cached, strict=False)
            # BLOCKER C3: wall-clock timeout. The report generator opens
            # its own engine for verify; we cannot SET LOCAL on it from
            # here, so we bound the whole call instead. Default 30 s.
            report = await asyncio.wait_for(
                generator.generate_article12(
                    date_from=date_from,
                    date_to=date_to,
                    verify_chain=True,
                ),
                timeout=_COMPLIANCE_STATEMENT_TIMEOUT_MS / 1000.0,
            )
            # DA Wave 4 blocker fix: read from/to seq directly off the
            # already-generated report — the previous implementation ran
            # ``verify_chain`` a second time, doubling O(n) HMAC work per
            # cold cache miss.
            from_seq = report.chain_verified_from_seq
            to_seq = report.chain_verified_to_seq
    except asyncio.TimeoutError:
        raise HTTPException(
            504, "Compliance report generation timed out."
        ) from None
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — degradation, not error path
        _logger.warning(
            "console.compliance_report_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(
            503, "Compliance report temporarily unavailable."
        ) from None

    # Count distinct agents across sessions by asking the report sections.
    total_agents = 0
    for section in report.sections:
        if section.title.lower().startswith("reference"):
            for row in section.data:
                val = row.get("value") if isinstance(row, dict) else None
                if isinstance(val, list):
                    total_agents = max(total_agents, len(val))

    view = ComplianceReportView(
        report_id=report.report_id,
        generated_at=_round_to_minute(report.generated_at),
        chain_integrity_status=_map_chain_status(
            report.chain_integrity_status
        ),  # type: ignore[arg-type]
        chain_verified_from_seq=from_seq,
        chain_verified_to_seq=to_seq,
        # BLOCKER C1: read directly from the generated report. True iff
        # the rotation-aware verifier path was used.
        rotation_aware=report.rotation_aware,
        coverage_pct=report.coverage_pct,
        coverage_pct_reason=report.coverage_pct_reason,
        coverage_caveat=report.coverage_caveat,
        total_events_audited=report.event_count,
        total_agents=total_agents,
    )
    if cache_key is not None:
        _compliance_cache_put(cache_key, view.model_dump(mode="json"))
    return view


async def _compliance_verify_chain_body(
    request: Request,
    *,
    from_seq: int | None = None,
    to_seq: int | None = None,
    cache_key: str | None = None,
) -> "VerifyChainResponse":
    """Pure helper: run a windowed HMAC chain verification.

    Shared by the public ``POST /api/compliance/verify-chain`` handler
    and the ``POST /api/compliance/export`` bundle endpoint. When
    ``cache_key`` is ``None`` the short-circuit cache is bypassed.

    Returns the strongly-typed :class:`VerifyChainResponse`; callers
    that need the JSON shape run ``.model_dump(mode="json")`` once.
    """
    from .models.responses import VerifyChainResponse

    if cache_key is not None:
        cached = _compliance_cache_get(cache_key)
        if cached is not None:
            # Re-validate (not ``model_construct``) so the returned view
            # carries the correct ``datetime`` type on ``verified_at_utc``.
            return VerifyChainResponse.model_validate(cached, strict=False)

    generator = _build_report_generator()
    if generator is None:
        raise HTTPException(
            503,
            "Chain verification unavailable: no audit backend configured.",
        )

    try:
        async with _compliance_concurrency_guard(request):
            if cache_key is not None:
                cached = _compliance_cache_get(cache_key)
                if cached is not None:
                    return VerifyChainResponse.model_validate(cached, strict=False)
            (
                status_raw,
                resolved_from,
                resolved_to,
                rotation_aware,
                unresolved_fingerprints,
            ) = await asyncio.wait_for(
                generator.run_chain_verification_windowed(
                    from_seq=from_seq, to_seq=to_seq,
                ),
                timeout=_COMPLIANCE_STATEMENT_TIMEOUT_MS / 1000.0,
            )
    except asyncio.TimeoutError:
        raise HTTPException(
            504, "Chain verification timed out."
        ) from None
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "console.compliance_verify_chain_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(
            503, "Chain verification temporarily unavailable."
        ) from None

    mapped = _map_chain_status(status_raw)
    if mapped == "verified":
        verified_count = 1
        failed_count = 0
    elif mapped == "halted":
        verified_count = 0
        failed_count = 1
    else:
        verified_count = 0
        failed_count = 0

    view = VerifyChainResponse(
        chain_integrity_status=mapped,  # type: ignore[arg-type]
        from_seq=resolved_from,
        to_seq=resolved_to,
        verified_count=verified_count,
        failed_count=failed_count,
        rotation_aware=rotation_aware,
        unresolved_fingerprints=list(unresolved_fingerprints),
        verified_at_utc=_round_to_minute(datetime.now(timezone.utc)),
    )
    if cache_key is not None:
        _compliance_cache_put(cache_key, view.model_dump(mode="json"))
    return view


@app.get(
    "/api/compliance/report",
    dependencies=[
        Depends(authenticate),
        Depends(_compliance_rate_limit_dep),
    ],
)
async def compliance_report(request: Request) -> dict[str, Any]:
    """F4: Article 12 evidence summary for the v4 compliance page.

    Assembles a :class:`ComplianceReportView` from an Article 12 report
    generated by :class:`ReportGenerator`, including a windowed HMAC
    chain verification (last 1000 events by default — see
    ``ReportGenerator._CHAIN_VERIFY_WINDOW``).

    Concurrency: protected by a module-level semaphore (max 2 concurrent
    verify_chain calls) and a 30-second short-circuit cache keyed on the
    (backend, verify_window) tuple. Rate limited at 1 req/60 s/user via
    the ``_compliance_rate_limit_dep`` dependency.
    """
    view = await _compliance_report_body(request)
    return view.model_dump(mode="json")


@app.post(
    "/api/compliance/verify-chain",
    dependencies=[
        Depends(authenticate),
        Depends(_compliance_rate_limit_dep),
    ],
)
async def compliance_verify_chain(
    request: Request,
    from_seq: int | None = Query(default=None, ge=0),
    to_seq: int | None = Query(default=None, ge=0),
) -> dict[str, Any]:
    """F4: on-demand HMAC chain re-verification for the header pill.

    Accepts optional ``from_seq``/``to_seq`` query parameters and
    defaults to the last 1000 events. Shares the F4 compliance
    semaphore + 30 s cache so a burst of header-pill clicks collapses
    onto a single backend verify.
    """
    view = await _compliance_verify_chain_body(
        request,
        from_seq=from_seq,
        to_seq=to_seq,
        cache_key=f"verify_chain:{from_seq}:{to_seq}",
    )
    return view.model_dump(mode="json")


# ---------------------------------------------------------------------------
# v0.6.1 polish: signed evidence bundle export
# ---------------------------------------------------------------------------
class ComplianceExportRequest(BaseModel):
    """Optional body for ``POST /api/compliance/export``.

    All fields are optional. ``window_start`` / ``window_end`` default to
    ``[now - 7 days, now]`` when omitted; ``tenant_id`` defaults to the
    caller's tenant (``None`` when the console is single-tenant).

    ``strict=False`` is deliberate: the datetime fields must accept
    ISO-8601 strings from JSON bodies. ``extra="forbid"`` still rejects
    unknown keys.
    """

    model_config = ConfigDict(extra="forbid")

    window_start: datetime | None = None
    window_end: datetime | None = None
    tenant_id: str | None = Field(default=None, max_length=256)


@app.post(
    "/api/compliance/export",
    dependencies=[
        Depends(authenticate),
        Depends(_compliance_rate_limit_dep),
    ],
)
async def compliance_export(
    request: Request,
    body: ComplianceExportRequest | None = None,
) -> dict[str, Any]:
    """F4 polish: signed Article 12 evidence bundle.

    Packages the existing ``compliance_report`` + ``compliance_verify_chain``
    outputs into a single JSON bundle with a sha256 content hash and an
    HMAC-SHA256 signature under the current ``AUDIT_SECRET``. Emits a
    ``compliance.bundle_exported`` audit row so the export itself leaves
    an audit-trail footprint.

    If the internal verify_chain call raises or times out the bundle is
    still emitted — ``verify_chain`` is ``None`` and
    ``chain_verification_error`` carries the exception classname. The
    export is evidence, not an enforcement gate.
    """
    from ..audit.keys import fingerprint_key
    from .models.responses import (
        ComplianceBundleResponse,
        ComplianceBundleRotationStatus,
        ComplianceBundleSignature,
        ComplianceBundleWindow,
    )

    body = body or ComplianceExportRequest()
    now = datetime.now(timezone.utc)
    window_end = body.window_end or now
    window_start = (
        body.window_start
        if body.window_start is not None
        else window_end - timedelta(days=7)
    )
    # Fail fast: window validation BEFORE any expensive work.
    if window_start >= window_end:
        raise HTTPException(
            400, "window_start must be strictly before window_end.",
        )
    # DA v0.6.1: reject mixed naive/aware datetimes — tzinfo mismatch
    # between ``window_start`` and ``window_end`` triggers a ``TypeError``
    # inside ``timedelta`` / comparison paths further down. A 400 is
    # strictly kinder than a 500.
    if (window_start.tzinfo is None) != (window_end.tzinfo is None):
        raise HTTPException(
            400,
            "window_start and window_end must both carry timezone info "
            "(ISO-8601 with a UTC offset).",
        )
    # DA v0.6.1: fail fast on missing AUDIT_SECRET. Previously this
    # raised AFTER the expensive report + verify_chain pass had already
    # run — a 503 that pins a pool slot for up to 30 s is worse UX than
    # a 503 that returns immediately.
    if not AUDIT_SECRET:
        raise HTTPException(
            503,
            "Bundle export unavailable: GOVERNANCE_AUDIT_SECRET not set.",
        )
    secret_bytes = AUDIT_SECRET.encode("utf-8")
    active_fingerprint = fingerprint_key(secret_bytes)

    # Reuse the shared report helper. cache_key=None → fresh run, since a
    # caller-supplied window shouldn't collide with the /report cache.
    report_view = await _compliance_report_body(
        request,
        date_from=window_start,
        date_to=window_end,
        cache_key=None,
    )

    verify_view: VerifyChainResponse | None = None
    chain_verification_error: str | None = None
    unresolved_in_window: list[str] = []
    # TODO(v0.7): map window_start/window_end onto from_seq/to_seq so
    # the verify_chain call is SCOPED to the bundle's export window.
    # Today we pass None/None, which verifies the default "last 1000
    # events" regardless of the caller's window — fine for evidence
    # (the verify result is just a checkpoint on the live chain) but a
    # reviewer can reasonably expect "verify MY window". Fix requires a
    # seq-from-time helper on the store.
    try:
        verify_view = await _compliance_verify_chain_body(
            request,
            from_seq=None,
            to_seq=None,
            cache_key=None,
        )
    except HTTPException as exc:
        chain_verification_error = f"HTTP {exc.status_code}: {exc.detail}"
    except Exception as exc:  # noqa: BLE001 — export must not fail whole
        chain_verification_error = type(exc).__name__
        _logger.warning(
            "console.compliance_export_verify_failed",
            error_type=type(exc).__name__,
            error=str(exc),
        )

    if verify_view is not None:
        unresolved_in_window = list(verify_view.unresolved_fingerprints)

    event_count = int(report_view.total_events_audited)

    window_model = ComplianceBundleWindow(start=window_start, end=window_end)
    rotation_status = ComplianceBundleRotationStatus(
        active_fingerprint=active_fingerprint,
        known_fingerprints_in_window=unresolved_in_window,
    )

    # Two-step content binding (v0.6.1: algorithm-pinned):
    #   1. ``bundle_hash`` = sha256 over the canonical body with ONLY
    #      ``bundle_hash`` removed AND ``bundle_signature.signature``
    #      cleared to empty string. The hash therefore commits to
    #      ``bundle_signature.algorithm`` and ``bundle_signature.key_fingerprint``,
    #      closing a version-confusion attack where an attacker with an
    #      old secret could re-label an HMAC bundle as Ed25519 (or vice
    #      versa) and a verifier without strict algorithm pinning would
    #      accept it.
    #   2. ``bundle_signature.signature`` = HMAC-SHA256 over the same
    #      canonical body (``bundle_signature.signature`` cleared, every
    #      other field — including ``bundle_hash`` and
    #      ``bundle_signature.algorithm`` — present). You can't sign
    #      yourself, so only the ``signature`` hex itself is blanked
    #      before hashing.
    #
    # Verifier recipe (mirrored in tests/console/test_compliance_export.py):
    #   signed_form = body with bundle_signature.signature = ""
    #   hashed_form = signed_form with bundle_hash deleted
    #   bundle_hash_ok = sha256(canonical(hashed_form))
    #   signature_ok   = HMAC(secret, canonical(signed_form))
    #
    # This is a behavior change from v0.6.0 — bundles created under the
    # old scheme (body without the whole bundle_signature sub-object) will
    # not re-verify. v0.6.0 shipped ~1 hour before v0.6.1, no production
    # bundles in the wild. CHANGELOG calls this out.
    #
    # We build the response Pydantic model first with placeholder hash/
    # signature, then compute the real values off its ``model_dump(mode=
    # "json")`` output so the hashing input is byte-identical to the
    # JSON bytes the client sees. This avoids a whole class of drift
    # bugs where ``datetime.isoformat()`` and Pydantic's ``+00:00`` /
    # ``Z`` encoding disagree.
    placeholder_hash = "0" * 64
    placeholder_signature = ComplianceBundleSignature(
        key_fingerprint=active_fingerprint,
        signature="0" * 64,
    )
    draft = ComplianceBundleResponse(
        bundle_version="1.0",
        generated_at=_round_to_minute(now),
        tenant_id=body.tenant_id,
        window=window_model,
        report=report_view,
        verify_chain=verify_view,
        chain_verification_error=chain_verification_error,
        rotation_status=rotation_status,
        event_count=event_count,
        bundle_hash=placeholder_hash,
        bundle_signature=placeholder_signature,
    )
    draft_dump: dict[str, Any] = draft.model_dump(mode="json")

    # Canonical form for SIGNING: whole bundle present, with
    # ``bundle_signature.signature`` blanked (can't sign yourself).
    # ``bundle_signature.algorithm`` and ``bundle_signature.key_fingerprint``
    # ARE included, so the signature binds them.
    body_for_signing = dict(draft_dump)
    sig_envelope_for_signing = dict(draft_dump["bundle_signature"])
    sig_envelope_for_signing["signature"] = ""
    body_for_signing["bundle_signature"] = sig_envelope_for_signing

    # Canonical form for HASHING: same as signing form, with
    # ``bundle_hash`` removed (the field that is about to hold the
    # digest). ``bundle_signature`` envelope is otherwise identical.
    body_for_hashing = {
        k: v for k, v in body_for_signing.items() if k != "bundle_hash"
    }

    canonical_for_hashing = canonical_json(body_for_hashing)
    bundle_hash = hashlib.sha256(
        canonical_for_hashing.encode("utf-8")
    ).hexdigest()

    # Fold the freshly-computed ``bundle_hash`` into the signing form so
    # the signature also commits to it.
    body_for_signing["bundle_hash"] = bundle_hash
    canonical_for_signing = canonical_json(body_for_signing)
    signature_hex = hmac.new(
        secret_bytes, canonical_for_signing.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    bundle_signature = ComplianceBundleSignature(
        key_fingerprint=active_fingerprint,
        signature=signature_hex,
    )

    response = draft.model_copy(
        update={
            "bundle_hash": bundle_hash,
            "bundle_signature": bundle_signature,
        }
    )

    # Audit row: compliance.bundle_exported. Best-effort — a failed audit
    # write MUST NOT fail the export (audit has its own non-breaking
    # guarantee inside AuditModule.log, but we still guard here so the
    # API response is always well-formed).
    if audit_module is not None:
        try:
            user_id = getattr(request.state, "user_id", None) or "anonymous"
            await audit_module.log(
                AuditEvent(
                    agent_id="governance-console",
                    kind="compliance.bundle_exported",
                    metadata={
                        "bundle_hash": bundle_hash,
                        "window_start": window_start.isoformat(),
                        "window_end": window_end.isoformat(),
                        "event_count": event_count,
                        "requesting_user": str(user_id),
                        "tenant_id": body.tenant_id,
                    },
                )
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "console.compliance_export_audit_failed", exc_info=False,
            )

    return response.model_dump(mode="json")
