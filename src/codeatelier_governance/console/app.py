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
    * Session-based auth with httpOnly cookies (v0.2.2+).
"""
from __future__ import annotations

import hashlib
import hmac
import os
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

from pydantic import BaseModel

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ..audit.chain import verify_event
from ..audit.models import AuditEventRecord
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


def _normalize_url(url: str) -> str:
    if not url:
        raise ValueError(
            "GOVERNANCE_DATABASE_URL env var is required for the console."
        )
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    raise ValueError("GOVERNANCE_DATABASE_URL must be a postgresql:// URL.")


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
    global engine
    engine = create_async_engine(
        _normalize_url(DATABASE_URL),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    yield
    if engine is not None:
        await engine.dispose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Code Atelier Governance Console",
    version="0.2.0",
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
    username: str
    password: str


class CreateUserRequest(BaseModel):
    """Create user request body."""
    username: str
    password: str
    role: str = "viewer"


class UpdateUserRequest(BaseModel):
    """Update user request body."""
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
    return {"ok": True, "version": "0.2.2"}


# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------
@app.post("/api/auth/login")
async def login(body: LoginRequest) -> JSONResponse:
    """Authenticate and create a session. Sets an httpOnly cookie."""
    assert engine is not None
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
        raise HTTPException(403, "Account is disabled.")
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
    assert engine is not None
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
    assert engine is not None
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
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(409, f"Username '{body.username}' already exists.")
        raise
    return {"ok": True, "user_id": str(uid), "username": body.username.lower()}


@app.patch(
    "/api/auth/users/{user_id}",
    dependencies=[Depends(authenticate), require_role("admin")],
)
async def update_user(user_id: UUID, body: UpdateUserRequest) -> dict[str, Any]:
    """Update a user's role or disabled status (admin only)."""
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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


@app.post("/api/gates/{request_id}/grant", dependencies=[Depends(authenticate)])
async def grant_gate(request_id: UUID) -> dict[str, Any]:
    """Grant a pending approval gate request."""
    if not AUDIT_SECRET:
        raise HTTPException(
            500,
            "GOVERNANCE_AUDIT_SECRET env var required for gate operations.",
        )
    secret = AUDIT_SECRET.encode("utf-8")
    assert engine is not None
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

        # Insert audit event for the grant action
        event_id = str(uuid4())
        await conn.execute(
            text(
                "INSERT INTO governance_audit_events "
                "(event_id, session_id, agent_id, kind, metadata_json, created_at) "
                "VALUES (:eid, :sid, :aid, :kind, :meta, :now)"
            ),
            {
                "eid": event_id,
                "sid": str(uuid4()),
                "aid": row["agent_id"],
                "kind": "approval.granted",
                "meta": f'{{"request_id": "{request_id}", "gate_kind": "{row["kind"]}"}}',
                "now": now,
            },
        )

    return {"ok": True, "request_id": str(request_id), "resolution": "granted"}


@app.post("/api/gates/{request_id}/deny", dependencies=[Depends(authenticate)])
async def deny_gate(request_id: UUID) -> dict[str, Any]:
    """Deny a pending approval gate request."""
    if not AUDIT_SECRET:
        raise HTTPException(
            500,
            "GOVERNANCE_AUDIT_SECRET env var required for gate operations.",
        )
    secret = AUDIT_SECRET.encode("utf-8")
    assert engine is not None
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

        # Insert audit event for the deny action
        event_id = str(uuid4())
        await conn.execute(
            text(
                "INSERT INTO governance_audit_events "
                "(event_id, session_id, agent_id, kind, metadata_json, created_at) "
                "VALUES (:eid, :sid, :aid, :kind, :meta, :now)"
            ),
            {
                "eid": event_id,
                "sid": str(uuid4()),
                "aid": row["agent_id"],
                "kind": "approval.denied",
                "meta": f'{{"request_id": "{request_id}", "gate_kind": "{row["kind"]}"}}',
                "now": now,
            },
        )

    return {"ok": True, "request_id": str(request_id), "resolution": "denied"}


@app.get("/api/policies", dependencies=[Depends(authenticate)])
async def list_policies() -> list[dict[str, Any]]:
    """Return all policies from the governance_policies table."""
    assert engine is not None
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
    assert engine is not None
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
    assert engine is not None
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

    posture: list[dict[str, Any]] = []
    for agent_id, info in agents.items():
        v_count = violations.get(agent_id, 0)
        b = budgets.get(agent_id, {})
        p_count = pending.get(agent_id, 0)
        e_count = exceeded.get(agent_id, 0)

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
                    "status": "FAIL"
                    if e_count > 0
                    else ("WARN" if float(b.get("usd_used", 0)) > 0 else "PASS"),
                    "usd_today": float(b.get("usd_used", 0)),
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
