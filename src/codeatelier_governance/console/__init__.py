"""Code Atelier Governance Console — read-only audit + enforcement dashboard.

Ships as an optional extra: ``pip install codeatelier-governance[console]``

The console is a thin FastAPI server that exposes read-only views over the
existing governance Postgres. It does NOT add any new infrastructure —
same database the SDK already writes to. Authentication in v0.2.0 is a
single shared bearer token loaded from ``GOVERNANCE_CONSOLE_TOKEN`` env
var; production deployments should put a reverse proxy (oauth2-proxy,
Tailscale, etc.) in front.

Usage:
    export GOVERNANCE_DATABASE_URL=postgresql://...
    export GOVERNANCE_AUDIT_SECRET=...
    export GOVERNANCE_CONSOLE_TOKEN=my-secret-bearer-token
    governance-console  # or: python -m codeatelier_governance.console
"""
