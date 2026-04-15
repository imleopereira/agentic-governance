"""Three key-source backends for F6 agent identity (constraint #2).

All three implement the ``KeyStore`` protocol:

    - ``load_private_key()``           -> Ed25519PrivateKey
    - ``load_public_key()``            -> Ed25519PublicKey
    - ``bootstrap_if_allowed(flag)``   -> None  (constraint #6)

Backends:

    ``FileKeyStore``       local dev default; ``0600`` on Unix.
    ``EnvKeyStore``        twelve-factor / k8s / Lambda; base64 private key.
    ``EphemeralKeyStore``  in-memory; zero-config test default (constraint #3/#5).

Constraint #1 (graceful degradation) is implemented at the *caller* of these
backends — the signing hook in the audit write path catches ``KeyStoreError``
and writes ``signature_status='unsigned_local_failure'``.  The backends
themselves raise loudly so the caller can make the degradation decision.
"""
from __future__ import annotations

import base64
import os
import re
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


class KeyStoreError(Exception):
    """Raised when a keystore cannot load or bootstrap a key.

    This is the single exception type the audit signing hook catches for
    the graceful-degradation path.  Subclassing it keeps the degradation
    contract narrow: arbitrary exceptions from a keystore implementation
    are a bug, not a degradation.
    """


@runtime_checkable
class KeyStore(Protocol):
    """Common interface across file/env/ephemeral backends."""

    def load_private_key(self) -> ed25519.Ed25519PrivateKey: ...
    def load_public_key(self) -> ed25519.Ed25519PublicKey: ...
    def bootstrap_if_allowed(self, allow_bootstrap: bool) -> None: ...


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^A-Z0-9]+")


def _agent_id_to_slug(agent_id: str) -> str:
    """Upper-snake slug for env-var naming.  Idempotent and injective enough
    for the common case; collisions between agents like ``foo-bar`` and
    ``foo_bar`` are an operator-facing concern documented in the config doc."""
    return _SLUG_RE.sub("_", agent_id.upper()).strip("_")


def _default_file_path(agent_id: str) -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "codeatelier-governance" / "agents" / f"{agent_id}.key"


def _serialize_private_pem(key: ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _load_private_pem(data: bytes) -> ed25519.Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except ValueError as exc:
        raise KeyStoreError(f"invalid Ed25519 PEM: {exc}") from exc
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise KeyStoreError(
            f"expected Ed25519 private key, got {type(key).__name__}"
        )
    return key


# ----------------------------------------------------------------------------
# FileKeyStore
# ----------------------------------------------------------------------------


class FileKeyStore:
    """PEM key file on local disk.  Enforces ``0600`` on POSIX systems.

    Not used in AWS Lambda or ephemeral containers — prefer ``EnvKeyStore``
    there.  The design doc calls out that unreadable / missing key files must
    NOT crash the host (constraint #1); this class still raises
    ``KeyStoreError`` on failure and the audit hook catches it.
    """

    def __init__(self, agent_id: str, path: Path | str | None = None) -> None:
        self.agent_id = agent_id
        self.path: Path = Path(path) if path else _default_file_path(agent_id)

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_private_key(self) -> ed25519.Ed25519PrivateKey:
        if not self.path.exists():
            raise KeyStoreError(f"key file not found: {self.path}")
        # Refuse world/group readable keys on POSIX.
        if os.name == "posix":
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & 0o077:
                raise KeyStoreError(
                    f"key file {self.path} has insecure mode {oct(mode)}; "
                    f"expected 0600"
                )
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise KeyStoreError(f"cannot read key file {self.path}: {exc}") from exc
        return _load_private_pem(data)

    def load_public_key(self) -> ed25519.Ed25519PublicKey:
        return self.load_private_key().public_key()

    def bootstrap_if_allowed(self, allow_bootstrap: bool) -> None:
        """Generate a new keypair and persist if allowed.

        Raises ``KeyStoreError`` unless ``allow_bootstrap=True`` — see
        constraint #6.  Safe to call when a key already exists (no-op).
        """
        if self.path.exists():
            return
        if not allow_bootstrap:
            raise KeyStoreError(
                f"no key file at {self.path} and allow_bootstrap=False. "
                f"Pre-provision the key out-of-band or set "
                f"AgentIdentityConfig(allow_bootstrap=True) in dev."
            )
        self._ensure_parent()
        key = ed25519.Ed25519PrivateKey.generate()
        pem = _serialize_private_pem(key)
        # Write + chmod atomically-ish: create with restrictive mode.
        fd = os.open(
            str(self.path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)
        if os.name == "posix":
            os.chmod(self.path, 0o600)


# ----------------------------------------------------------------------------
# EnvKeyStore
# ----------------------------------------------------------------------------


class EnvKeyStore:
    """Private key from an environment variable, base64-encoded PEM.

    The value is the base64 of the raw PKCS#8 PEM bytes — makes it
    paste-safe into k8s secrets and Lambda env vars.
    """

    def __init__(self, agent_id: str, var_name: str | None = None) -> None:
        self.agent_id = agent_id
        self.var_name = var_name or f"CODEATELIER_AGENT_KEY_{_agent_id_to_slug(agent_id)}"

    def _load(self) -> ed25519.Ed25519PrivateKey:
        raw = os.environ.get(self.var_name)
        if not raw:
            raise KeyStoreError(
                f"environment variable {self.var_name} is not set; "
                f"expected base64-encoded Ed25519 PEM"
            )
        try:
            pem = base64.b64decode(raw, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise KeyStoreError(
                f"{self.var_name}: value is not valid base64 ({exc})"
            ) from exc
        return _load_private_pem(pem)

    def load_private_key(self) -> ed25519.Ed25519PrivateKey:
        return self._load()

    def load_public_key(self) -> ed25519.Ed25519PublicKey:
        return self._load().public_key()

    def bootstrap_if_allowed(self, allow_bootstrap: bool) -> None:
        """Env backend cannot bootstrap in-place.

        Bootstrapping an env var would require mutating the process
        environment, which is (a) invisible to the host's secret-management
        layer and (b) would not survive a restart.  We raise if the env var
        is missing regardless of the flag; operators must provision the
        secret externally.
        """
        if os.environ.get(self.var_name):
            return
        if not allow_bootstrap:
            raise KeyStoreError(
                f"env var {self.var_name} missing and allow_bootstrap=False"
            )
        # With allow_bootstrap=True we generate a fresh key and inject it
        # into os.environ for the remainder of the process lifetime.  Local
        # dev convenience only — a warning is emitted by the caller.
        key = ed25519.Ed25519PrivateKey.generate()
        pem = _serialize_private_pem(key)
        os.environ[self.var_name] = base64.b64encode(pem).decode("ascii")


# ----------------------------------------------------------------------------
# EphemeralKeyStore
# ----------------------------------------------------------------------------


class EphemeralKeyStore:
    """In-memory keypair generated per process.

    Constraint #3 / #5: zero-config test mode.  ``GovernanceSDK()`` with
    no arguments picks this backend when ``CODEATELIER_TEST_MODE=1`` or
    when the identity feature is enabled with ``key_source='ephemeral'``.

    Two instances of ``EphemeralKeyStore`` generate DIFFERENT keys.  There
    is no filesystem or environment-variable access.
    """

    def __init__(self, agent_id: str | None = None) -> None:
        self.agent_id = agent_id
        self._key: ed25519.Ed25519PrivateKey = ed25519.Ed25519PrivateKey.generate()

    def load_private_key(self) -> ed25519.Ed25519PrivateKey:
        return self._key

    def load_public_key(self) -> ed25519.Ed25519PublicKey:
        return self._key.public_key()

    def bootstrap_if_allowed(self, allow_bootstrap: bool) -> None:  # pragma: no cover
        """No-op: the keypair was generated at ``__init__`` time."""
        return None


# ----------------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------------


def build_keystore(
    agent_id: str,
    key_source: str,
    key_uri: str | None = None,
) -> KeyStore:
    """Dispatch to the requested backend by name."""
    if key_source == "file":
        return FileKeyStore(agent_id, key_uri)
    if key_source == "env":
        return EnvKeyStore(agent_id, key_uri)
    if key_source == "ephemeral":
        return EphemeralKeyStore(agent_id)
    raise KeyStoreError(f"unknown key_source {key_source!r}")


def is_test_mode() -> bool:
    """Detect implicit test mode for zero-config SDK instantiation.

    Recognized markers (any one):
      * ``CODEATELIER_TEST_MODE=1``
      * ``PYTEST_CURRENT_TEST`` set (pytest sets this automatically per test).
    """
    return bool(
        os.environ.get("CODEATELIER_TEST_MODE") == "1"
        or os.environ.get("PYTEST_CURRENT_TEST")
    )
