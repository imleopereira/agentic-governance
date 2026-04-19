"""Platform audit-ingest bridge.

v0.7.0 wires the SDK to dual-write audit events to the Code Atelier
platform's ingest endpoint (``POST /api/v1/ingest/events``) without
breaking the host app when the platform is unreachable.

Public surface:
    PlatformConfig  -- dataclass with URL, token, queue/retry tuning.
    PlatformClient  -- fire-and-forget async HTTP forwarder.

Everything else in this package (errors, wire helpers) is internal.
The SDK instantiates and owns the PlatformClient — applications do
not construct it directly.

The bridge requires the ``[platform]`` extra (``httpx``). If the
extra is missing but the caller configured a platform_ingest_url +
token, GovernanceSDK.__init__ raises ImportError with the pip
install instruction.
"""
from .client import PlatformClient
from .config import PlatformConfig

__all__ = [
    "PlatformClient",
    "PlatformConfig",
]
