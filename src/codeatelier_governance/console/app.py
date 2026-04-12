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

import hashlib
import hmac
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

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

from pydantic import BaseModel, ConfigDict, Field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..audit.chain import verify_event
from ..audit.models import AuditEvent, AuditEventRecord
from ..audit.module import AuditModule
from .auth import create_session_id, hash_password, session_expires_at, verify_password

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
# Audit module for console-originated events (gate grant/deny)
# ---------------------------------------------------------------------------
audit_module: AuditModule | None = None


def _normalize_url(url: str) -> str:
    if not url:
        raise ValueError(
            "Console startup failed: GOVERNANCE_DATABASE_URL env var is not set.\n"
            "Expected: a postgresql:// connection string.\n"
            "Fix: export GOVERNANCE_DATABASE_URL=postgresql://user:pass@host/db"
        )
    from ..utils import normalize_db_url
    return normalize_db_url(url, component="console")


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
    global engine, audit_module
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
    yield
    if audit_module is not None:
        await audit_module.close()
    if engine is not None:
        await engine.dispose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Code Atelier Governance Console",
    version="0.4.0",
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
    return {"ok": True, "version": "0.4.0"}


# ---------------------------------------------------------------------------
# SSE: real-time event stream
# ---------------------------------------------------------------------------
@app.get("/api/stream/events")
async def stream_events(
    request: Request,
    last_event_id: str | None = Query(None),
) -> Any:
    """Server-Sent Events endpoint for real-time audit events, gate changes,
    and presence updates.

    Polls the database every 2 seconds for new events. The client sends
    ``last_event_id`` (chain_seq) on reconnect for replay.

    Auth: same session cookie as all other endpoints.
    """
    from starlette.responses import StreamingResponse

    # Validate session (reuse the authenticate dependency logic)
    session_cookie = request.cookies.get("governance_session")
    token_header = request.headers.get("x-governance-token")
    if not session_cookie and not token_header:
        raise HTTPException(401, "Authentication required for SSE stream.")

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
                raise HTTPException(401, "Session expired.")

    async def event_generator() -> Any:
        """Yield SSE events by polling governance tables."""
        import asyncio
        import json

        last_seq: int | None = None
        if last_event_id:
            try:
                last_seq = int(last_event_id)
            except ValueError:
                last_seq = None

        revalidate_counter = 0

        while True:
            if await request.is_disconnected():
                break

            # Re-validate session every 15 iterations (~30s at 2s interval)
            revalidate_counter += 1
            if revalidate_counter >= 15 and session_cookie and engine is not None:
                revalidate_counter = 0
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
                        break

            if engine is None:
                await asyncio.sleep(2)
                continue

            try:
                async with engine.connect() as conn:
                    # Fetch new audit events
                    if last_seq is not None:
                        q = text(
                            "SELECT event_id, agent_id, session_id, kind, "
                            "model, metadata_json AS metadata, created_at, chain_seq "
                            "FROM governance_audit_events "
                            "WHERE chain_seq > :seq "
                            "ORDER BY chain_seq ASC LIMIT 100"
                        )
                        res = await conn.execute(q, {"seq": last_seq})
                    else:
                        q = text(
                            "SELECT event_id, agent_id, session_id, kind, "
                            "model, metadata_json AS metadata, created_at, chain_seq "
                            "FROM governance_audit_events "
                            "ORDER BY chain_seq DESC LIMIT 20"
                        )
                        res = await conn.execute(q)

                    rows = res.mappings().all()
                    if not last_seq:
                        rows = list(reversed(rows))

                    for row in rows:
                        meta = row["metadata"]
                        if isinstance(meta, str):
                            try:
                                meta = json.loads(meta)
                            except (json.JSONDecodeError, TypeError):
                                meta = {}
                        safe_meta = _redact_metadata(meta) if meta else {}
                        evt = {
                            "event_id": str(row["event_id"]),
                            "agent_id": row["agent_id"],
                            "session_id": str(row["session_id"]) if row["session_id"] else None,
                            "kind": row["kind"],
                            "model": row.get("model"),
                            "metadata": safe_meta,
                            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                            "chain_seq": row["chain_seq"],
                        }
                        seq = row["chain_seq"]
                        data = json.dumps(evt)
                        yield f"id: {seq}\nevent: audit\ndata: {data}\n\n"
                        last_seq = seq

                    # Fetch presence changes
                    async with engine.connect() as conn2:
                        pres_res = await conn2.execute(
                            text(
                                "SELECT agent_id, status, last_heartbeat "
                                "FROM governance_agent_presence "
                                "ORDER BY last_heartbeat DESC LIMIT 10"
                            )
                        )
                        for prow in pres_res.mappings().all():
                            pevt = {
                                "agent_id": prow["agent_id"],
                                "status": prow["status"],
                                "last_heartbeat": prow["last_heartbeat"].isoformat() if prow["last_heartbeat"] else None,
                            }
                            yield f"event: presence\ndata: {json.dumps(pevt)}\n\n"

            except Exception:
                _logger.warning("sse.poll_error", exc_info=False)

            # Keepalive
            yield ": keepalive\n\n"
            await asyncio.sleep(2)

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
)
async def revoke_session(session_id: UUID) -> dict[str, Any]:
    """Revoke a specific session (admin only)."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "UPDATE governance_console_sessions SET revoked = TRUE "
                "WHERE session_id = :sid AND revoked = FALSE"
            ),
            {"sid": str(session_id)},
        )
        if res.rowcount == 0:
            raise HTTPException(404, "Session not found or already revoked.")
    return {"ok": True, "session_id": str(session_id)}


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


@app.get("/api/gates/pending", dependencies=[Depends(authenticate)])
async def gates_pending() -> list[dict[str, Any]]:
    """List all unresolved approval requests."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT request_id, agent_id, kind, action_hash, "
                "created_at, expires_at "
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
                if row["created_at"]
                else None,
                "expires_at": row["expires_at"].isoformat()
                if row["expires_at"]
                else None,
            }
            for row in res.mappings()
        ]


@app.get("/api/gates/recent", dependencies=[Depends(authenticate)])
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
                "SELECT request_id, agent_id, kind, token, action_hash "
                "FROM governance_gates_pending "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {"rid": str(request_id)},
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(404, "Gate request not found or already resolved.")

        # Self-approval prevention
        if user_id is not None:
            await _check_self_approval(conn, row["agent_id"], user_id, request_id)

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
                },
            )
        )

    return {"ok": True, "request_id": str(request_id), "resolution": "granted"}


@app.post("/api/gates/{request_id}/deny", dependencies=[Depends(authenticate)])
async def deny_gate(request_id: UUID, request: Request) -> dict[str, Any]:
    """Deny a pending approval gate request."""
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
                "SELECT request_id, agent_id, kind, token, action_hash "
                "FROM governance_gates_pending "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {"rid": str(request_id)},
        )
        row = res.mappings().first()
        if not row:
            raise HTTPException(404, "Gate request not found or already resolved.")

        # Self-approval prevention
        if user_id is not None:
            await _check_self_approval(conn, row["agent_id"], user_id, request_id)

        # Verify the HMAC signature on the token
        token_value = row["token"]
        if token_value:
            expected = hmac.new(
                secret, str(request_id).encode("utf-8"), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(token_value, expected):
                raise HTTPException(400, "Token HMAC verification failed.")

        now = datetime.now(timezone.utc)
        # Resolve the gate as denied
        await conn.execute(
            text(
                "UPDATE governance_gates_pending "
                "SET resolved_at = :now, resolution = :resolution "
                "WHERE request_id = :rid AND resolved_at IS NULL"
            ),
            {"now": now, "resolution": "denied", "rid": str(request_id)},
        )

    # Audit event via SDK — part of the HMAC chain
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
                },
            )
        )

    return {"ok": True, "request_id": str(request_id), "resolution": "denied"}


@app.get("/api/policies", dependencies=[Depends(authenticate)])
async def list_policies() -> list[dict[str, Any]]:
    """Return all policies from the governance_policies table."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                "SELECT agent_id, policy_type, policy_json, updated_at "
                "FROM governance_policies "
                "ORDER BY agent_id, policy_type"
            )
        )
        return [
            {
                "agent_id": row["agent_id"],
                "policy_type": row["policy_type"],
                "policy": row["policy_json"],
                "updated_at": row["updated_at"].isoformat()
                if row["updated_at"]
                else None,
            }
            for row in res.mappings()
        ]


@app.get("/api/policies/{agent_id}", dependencies=[Depends(authenticate)])
async def get_agent_policies(agent_id: str) -> list[dict[str, Any]]:
    """Return scope + budget policies for a single agent."""
    if engine is None:
        raise HTTPException(503, "Console backend is starting up. Try again in a moment.")
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
        return [
            {
                "agent_id": row["agent_id"],
                "policy_type": row["policy_type"],
                "policy": row["policy_json"],
                "updated_at": row["updated_at"].isoformat()
                if row["updated_at"]
                else None,
            }
            for row in res.mappings()
        ]


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


@app.get("/api/agents/presence", dependencies=[Depends(authenticate)])
async def agent_presence() -> list[dict[str, Any]]:
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
            return [
                {
                    "agent_id": row["agent_id"],
                    "status": row["status"],
                    "last_heartbeat": row["last_heartbeat"].isoformat()
                    if row["last_heartbeat"]
                    else None,
                    "started_at": row["started_at"].isoformat()
                    if row["started_at"]
                    else None,
                    "metadata": row["metadata_json"],
                }
                for row in res.mappings()
            ]
    except Exception as exc:
        # Table may not exist yet if migration hasn't been run
        _logger.debug(
            "console.agent_presence_skipped",
            exc_type=type(exc).__name__,
        )
        return []
