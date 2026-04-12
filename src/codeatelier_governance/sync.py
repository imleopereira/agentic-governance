"""Synchronous wrapper for the async GovernanceSDK.

Follows the httpx pattern: ``GovernanceSDKSync`` wraps ``GovernanceSDK`` with
a dedicated background thread running an asyncio event loop.  Every async
method is exposed as a blocking sync call via
``asyncio.run_coroutine_threadsafe``.

Usage::

    from codeatelier_governance import GovernanceSDKSync

    with GovernanceSDKSync(database_url="postgresql://...") as sdk:
        sdk.audit.log(AuditEvent(agent_id="a", kind="tool.call"))
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any

from .sdk import GovernanceSDK


class _SyncModuleProxy:
    """Proxy that wraps an async module, exposing sync counterparts.

    Sync methods (like ``register``) are called directly.  Async methods
    are submitted to the background event loop and block until complete.
    Exceptions propagate naturally through ``Future.result()``.
    """

    __slots__ = ("_module", "_loop")

    def __init__(self, module: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._module = module
        self._loop = loop

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._module, name)

        if not callable(attr):
            return attr

        if inspect.iscoroutinefunction(attr):

            def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                if not self._loop.is_running():
                    raise RuntimeError(
                        "GovernanceSDKSync background loop has stopped"
                    )
                future = asyncio.run_coroutine_threadsafe(
                    attr(*args, **kwargs), self._loop,
                )
                return future.result()

            # Preserve the original name for debugging / repr
            _sync_wrapper.__name__ = name
            _sync_wrapper.__qualname__ = f"_SyncModuleProxy.{name}"
            _sync_wrapper.__doc__ = attr.__doc__
            return _sync_wrapper

        # Non-coroutine callable (e.g. register) — return directly
        return attr


class GovernanceSDKSync:
    """Synchronous wrapper around :class:`GovernanceSDK`.

    Spins up a daemon thread with a dedicated asyncio event loop.  All
    async module methods (``audit.log``, ``cost.track``, etc.) are exposed
    as blocking sync calls that submit coroutines to that loop.

    Supports context-manager usage::

        with GovernanceSDKSync(database_url=...) as sdk:
            sdk.audit.log(event)

    The background thread is a daemon thread so it does not prevent
    interpreter shutdown.
    """

    def __init__(
        self,
        database_url: str | None = None,
        api_key: str | None = None,
        audit_secret: bytes | None = None,
        **kwargs: Any,
    ) -> None:
        # Create the background event loop and thread FIRST so the
        # GovernanceSDK __init__ (which may lazily create SQLAlchemy
        # engines) can't interfere with an existing loop.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="governance-sync-loop",
            daemon=True,
        )
        self._thread.start()
        self._closed = False

        # Create the async SDK instance (no I/O happens in __init__)
        self._async_sdk = GovernanceSDK(
            database_url=database_url,
            api_key=api_key,
            audit_secret=audit_secret,
            **kwargs,
        )

        # Expose sub-modules through sync proxies
        self.audit = _SyncModuleProxy(self._async_sdk.audit, self._loop)
        self.scope = _SyncModuleProxy(self._async_sdk.scope, self._loop)
        self.cost = _SyncModuleProxy(self._async_sdk.cost, self._loop)
        self.gates = _SyncModuleProxy(self._async_sdk.gates, self._loop)
        self.loop = _SyncModuleProxy(self._async_sdk.loop, self._loop)
        self.presence = _SyncModuleProxy(self._async_sdk.presence, self._loop)
        self.contracts = _SyncModuleProxy(self._async_sdk.contracts, self._loop)

    def start(self) -> None:
        """Start the underlying async SDK (audit flusher, hot-reload, etc.)."""
        if not self._loop.is_running():
            raise RuntimeError("GovernanceSDKSync background loop has stopped")
        future = asyncio.run_coroutine_threadsafe(
            self._async_sdk.start(), self._loop,
        )
        future.result()

    def close(self) -> None:
        """Drain in-flight events and stop the background loop.

        Idempotent: calling ``close()`` multiple times is safe.
        """
        if self._closed:
            return
        self._closed = True

        if self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._async_sdk.close(), self._loop,
            )
            future.result(timeout=10)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)

    def __enter__(self) -> GovernanceSDKSync:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def config(self) -> Any:
        """Expose the underlying SDK config for inspection."""
        return self._async_sdk.config
