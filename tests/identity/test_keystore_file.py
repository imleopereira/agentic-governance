"""FileKeyStore — round-trip, permission enforcement, missing-file behavior."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from codeatelier_governance.identity.keystore import (
    FileKeyStore,
    KeyStoreError,
)


def test_bootstrap_generates_and_persists_0600(tmp_path: Path) -> None:
    key_path = tmp_path / "sub" / "agent.key"
    store = FileKeyStore("agent-a", path=key_path)
    store.bootstrap_if_allowed(allow_bootstrap=True)
    assert key_path.exists()
    if os.name == "posix":
        mode = stat.S_IMODE(key_path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # Round-trip.
    priv = store.load_private_key()
    pub = store.load_public_key()
    sig = priv.sign(b"hello")
    pub.verify(sig, b"hello")


def test_missing_file_raises(tmp_path: Path) -> None:
    store = FileKeyStore("agent-a", path=tmp_path / "nope.key")
    with pytest.raises(KeyStoreError, match="not found"):
        store.load_private_key()


def test_bootstrap_disallowed_raises(tmp_path: Path) -> None:
    store = FileKeyStore("agent-a", path=tmp_path / "nope.key")
    with pytest.raises(KeyStoreError, match="allow_bootstrap=False"):
        store.bootstrap_if_allowed(allow_bootstrap=False)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only permission check")
def test_world_readable_key_file_rejected(tmp_path: Path) -> None:
    key_path = tmp_path / "bad.key"
    store = FileKeyStore("agent-a", path=key_path)
    store.bootstrap_if_allowed(allow_bootstrap=True)
    os.chmod(key_path, 0o644)
    with pytest.raises(KeyStoreError, match="insecure mode"):
        store.load_private_key()
