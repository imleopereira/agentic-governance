"""Entry point for ``python -m codeatelier_governance.console``."""
from __future__ import annotations

import os
import sys

import structlog

_logger = structlog.get_logger(__name__)


def main() -> None:
    try:
        import uvicorn
    except ImportError:
        _logger.error(
            "console.uvicorn_not_installed",
            hint="pip install codeatelier-governance[console]",
        )
        sys.exit(1)

    if not os.environ.get("GOVERNANCE_DATABASE_URL"):
        _logger.error(
            "console.database_url_missing",
            hint="export GOVERNANCE_DATABASE_URL=postgresql://...",
        )
        sys.exit(1)

    host = os.environ.get("GOVERNANCE_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("GOVERNANCE_CONSOLE_PORT", "8766"))
    workers = int(os.environ.get("GOVERNANCE_CONSOLE_WORKERS", "1"))
    log_level = os.environ.get("GOVERNANCE_CONSOLE_LOG_LEVEL", "info")

    _logger.info("console.starting", host=host, port=port, docs=f"http://{host}:{port}/docs")
    if not os.environ.get("GOVERNANCE_CONSOLE_TOKEN"):
        _logger.warning(
            "console.no_auth_token",
            hint="Set GOVERNANCE_CONSOLE_TOKEN or use session auth. Do NOT expose without a reverse proxy.",
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
