"""Internal exceptions for the platform bridge.

These exceptions are **strictly internal** to the platform subsystem and
MUST NOT propagate to the host application. The bridge is a best-effort
dual-write; any error is logged (with tokens scrubbed) and the forward
is dropped.  The host's audit.log() call MUST complete regardless.

Why a separate module?
    The platform forward is a fire-and-forget task on a background
    worker. The worker catches every exception at the top, but typed
    errors let us branch on response class (4xx vs 5xx, retryable vs
    terminal) without resorting to string matching on httpx exceptions.

None of these inherit from ``GovernanceError`` because they are not
part of the user-visible SDK contract. If a host-app ``except
GovernanceError`` block caught them, it would defeat the non-propagation
invariant.
"""
from __future__ import annotations


class PlatformBridgeError(Exception):
    """Base class for every platform-bridge internal exception.

    Callers should never catch this broadly — the worker handles that.
    Subclasses exist to let the worker decide whether to retry or drop.
    """


class PlatformConfigError(PlatformBridgeError):
    """Raised at SDK init when platform config is invalid.

    The *only* platform exception that is allowed to surface at SDK
    construction time (because it indicates a misconfigured deployment,
    not a runtime fault). Once the SDK is running, every bridge error
    is contained inside the worker.
    """


class PlatformRetryableError(PlatformBridgeError):
    """5xx, 429, connection error, timeout — safe to retry.

    The worker applies exponential backoff (1s → 2s → 4s → 8s) with a
    ~15s cumulative cap per event. After the cap the event is dropped
    and a cumulative-drop counter is incremented.
    """


class PlatformTerminalError(PlatformBridgeError):
    """4xx other than 429 — drop the event, do not retry.

    Examples: 400 payload_contains_secret, 413 payload_too_large,
    401 invalid token. 401 additionally disables the bridge for the
    remainder of the process (see client.py).
    """


class PlatformAuthError(PlatformTerminalError):
    """401 invalid token.

    Handled specially: log WARN once (with sha256-prefix(token, 8) as
    identifier — NEVER the token itself) and set the bridge to
    disabled for the process lifetime. The host app keeps working on
    local-only audit.
    """


class PlatformQueueFullError(PlatformBridgeError):
    """Enqueue failed because the forward queue is at max_queue_size.

    The client drops the oldest queued event (not the new one) so the
    freshest events have the best chance of reaching the platform, and
    logs WARN with the cumulative drop count. Never raised to the host
    — forward() swallows it.
    """
