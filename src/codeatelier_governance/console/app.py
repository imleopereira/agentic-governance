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
import unicodedata
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
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

from ..audit.chain import verify_event
from ..audit.models import AuditEvent, AuditEventRecord
from ..audit.module import AuditModule
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


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
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
        reviewer_id = row.get("reviewer_id")
        if reviewer_id is not None and user_id is not None:
            if str(reviewer_id) != user_id:
                raise HTTPException(
                    403,
                    "Gate is claimed by another reviewer. "
                    "Wait for the reviewer to act or for the claim to expire.",
                )

        # Verify the HMAC signature on the token
        token_value = row["token"]
        if token_value:
            expected = hmac.new(
                secret, str(request_id).encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(token_value, expected):
                raise HTTPException(400, "Token HMAC verification failed.")

        now = datetime.now(timezone.utc)
        # Resolve the gate as granted
        await conn.execute(
            text(
                "UPDATE governance_gates_pending "
                "SET resolved_at = :now, resolution = :resolution "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {"now": now, "resolution": "granted", "rid": str(request_id)},
        )

    # Audit event via SDK — part of the HMAC chain
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

        reviewer_id = row.get("reviewer_id")
        if reviewer_id is not None and user_id is not None:
            if str(reviewer_id) != user_id:
                raise HTTPException(
                    403,
                    "Gate is claimed by another reviewer. "
                    "Wait for the reviewer to act or for the claim to expire.",
                )

        token_value = row["token"]
        if token_value:
            expected = hmac.new(
                secret, str(request_id).encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(token_value, expected):
                raise HTTPException(400, "Token HMAC verification failed.")

        now = datetime.now(timezone.utc)
        await conn.execute(
            text(
                "UPDATE governance_gates_pending "
                "SET resolved_at = :now, resolution = :resolution, "
                "rationale = :rationale "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {
                "now": now,
                "resolution": "denied",
                "rationale": body.rationale,
                "rid": str(request_id),
            },
        )

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

    policies = [
        PolicyRow(
            agent_id=row["agent_id"],
            policy_type=row["policy_type"],
            policy=redact_secrets(row["policy_json"] or {}),
            updated_at=row["updated_at"],
        )
        for row in rows
    ]
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

    policies = [
        PolicyRow(
            agent_id=row["agent_id"],
            policy_type=row["policy_type"],
            policy=redact_secrets(row["policy_json"] or {}),
            updated_at=row["updated_at"],
        )
        for row in rows
    ]
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
# New endpoint: Agent kill switch
# ---------------------------------------------------------------------------
# C0 controls to strip from `reason` (post-escape) so that an operator can't
# sneak raw terminal / log-injection bytes into the audit chain. Note `\x09`
# (tab), `\x0a` (LF), `\x0d` (CR) are EXCLUDED here because they are already
# converted to their printable backslash forms in the escape step above.
_C0_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KILL_REASON_MAX = 512


class KillRequest(BaseModel):
    """Kill-switch request body.

    Security (F2 P0, Cybersec HIGH): the `reason` field flows all the way
    into the HMAC-chained audit row. An unescaped newline would let an
    operator forge a follow-on "event" visually in chain exports. An
    unbounded length would let them DoS a reviewer's UI. A zalgo bomb
    (combining marks) would expand post-decode if we capped bytes instead
    of Unicode codepoints. The validator below normalizes, length-caps,
    escapes, then strips C0 controls — in that order.
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
        # 1. NFC normalize FIRST so zalgo / combining-mark expansions can't
        #    bypass the length cap by being applied post-decode.
        value = unicodedata.normalize("NFC", value)
        # 2. Cap RAW input (in Unicode codepoints, not bytes) BEFORE escaping.
        #    Escape sequences like `\n` → `\\n` double the length; capping
        #    after escape would let a 512-char stream of newlines still
        #    produce a 1024-char audit field.
        if len(value) > _KILL_REASON_MAX:
            value = value[:_KILL_REASON_MAX]
        # 3. Escape order: backslash MUST be first so that `\n` in the input
        #    (which we escape to `\\n`) can't collide with an already-escaped
        #    `\\n` from a later pass (`\\n` → `\\\\n`).
        value = (
            value.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        # 4. Strip remaining C0 controls (\x00-\x08, \x0b-\x1f, \x7f). ANSI
        #    escape sequences like `\x1b[31m` begin with \x1b and are killed
        #    at the first byte; the rest of the escape becomes harmless text.
        value = _C0_CONTROL_RE.sub("", value)
        return value


@app.post(
    "/api/agents/{agent_id}/kill",
    dependencies=[Depends(authenticate), require_role("admin")],
)
async def kill_agent(
    agent_id: str, body: KillRequest, request: Request
) -> dict[str, Any]:
    """Kill (halt) an agent -- admin only. Audit-logged. Requires reason.

    Sets the agent's presence status to unresponsive and records kill
    metadata in metadata_json. Process termination is the host's responsibility.
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

        kill_meta = json.dumps({
            "_killed_by": user_id,
            "_killed_at": now.isoformat(),
            "_kill_reason": body.reason,
        })
        await conn.execute(
            text(
                "UPDATE governance_agent_presence "
                "SET status = 'unresponsive', "
                "    metadata_json = COALESCE(metadata_json, '{}'::jsonb) "
                "                    || :meta::jsonb "
                "WHERE agent_id = :aid"
            ),
            {"aid": agent_id, "meta": kill_meta},
        )

    if audit_module is not None:
        session_id = UUID(
            hashlib.md5(("kill:" + agent_id + ":" + now.isoformat()).encode()).hexdigest()
        )
        await audit_module.log(
            AuditEvent(
                agent_id=agent_id,
                session_id=session_id,
                kind="agent.killed",
                metadata={
                    "killed_by": user_id,
                    "reason": body.reason,
                    "killed_at": now.isoformat(),
                },
            )
        )

    _logger.info("console.agent_killed", agent_id=agent_id, killed_by=user_id)
    return {"ok": True, "agent_id": agent_id, "action": "killed", "killed_at": now.isoformat()}


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
    """
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    user_id = getattr(request.state, "user_id", "unknown")
    now = datetime.now(timezone.utc)

    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id, resolved_at, payload_json "
                "FROM governance_gates_pending WHERE request_id = :rid"
            ),
            {"rid": str(request_id)},
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(404, "Gate request not found.")
        if row["resolved_at"] is not None:
            raise HTTPException(409, "Gate is already resolved.")

        existing_payload = row["payload_json"] or {}
        if not isinstance(existing_payload, dict):
            existing_payload = {}
        existing_payload["escalated_by"] = user_id
        existing_payload["escalated_to"] = body.escalate_to
        existing_payload["escalated_at"] = now.isoformat()

        import json as _json
        await conn.execute(
            text(
                "UPDATE governance_gates_pending "
                "SET payload_json = :payload::jsonb, "
                "    reviewer_id = NULL, reviewing_since = NULL "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {"payload": _json.dumps(existing_payload), "rid": str(request_id)},
        )

    _logger.info(
        "console.gate_escalated",
        request_id=str(request_id),
        escalated_by=user_id,
        escalated_to=body.escalate_to,
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


@app.get(
    "/api/events/{event_id}",
    dependencies=[Depends(authenticate)],
)
async def get_audit_event(event_id: str) -> dict[str, Any]:
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
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
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
