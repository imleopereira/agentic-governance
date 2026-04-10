"""Entry point for ``python -m codeatelier_governance.console``."""
from __future__ import annotations

import os
import sys


def main() -> None:
    try:
        import uvicorn
    except ImportError:
        print(
            "The governance console requires uvicorn. "
            "Install with: pip install codeatelier-governance[console]",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.environ.get("GOVERNANCE_DATABASE_URL"):
        print(
            "ERROR: GOVERNANCE_DATABASE_URL env var is required.\n"
            "Fix: export GOVERNANCE_DATABASE_URL=postgresql://...",
            file=sys.stderr,
        )
        sys.exit(1)

    host = os.environ.get("GOVERNANCE_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("GOVERNANCE_CONSOLE_PORT", "8766"))
    workers = int(os.environ.get("GOVERNANCE_CONSOLE_WORKERS", "1"))
    log_level = os.environ.get("GOVERNANCE_CONSOLE_LOG_LEVEL", "info")

    print(f"Starting Governance Console at http://{host}:{port}")
    print(f"API docs: http://{host}:{port}/docs")
    if not os.environ.get("GOVERNANCE_CONSOLE_TOKEN"):
        print(
            "WARNING: GOVERNANCE_CONSOLE_TOKEN not set — running without auth. "
            "Do NOT expose on a public network without a reverse proxy.",
            file=sys.stderr,
        )

    uvicorn.run(
        "codeatelier_governance.console.app:app",
        host=host,
        port=port,
        workers=workers,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
