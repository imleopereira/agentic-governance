"""CONSTRAINT #6 — bootstrap is opt-in.

``allow_bootstrap=False`` (the default) MUST refuse to generate a key
when none exists.  ``allow_bootstrap=True`` generates and persists.
This closes the first-runner-wins TOFU race identified by Cybersec.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeatelier_governance.identity.keystore import (
    FileKeyStore,
    KeyStoreError,
)


def test_file_bootstrap_refused_by_default(tmp_path: Path) -> None:
    store = FileKeyStore("agent", path=tmp_path / "k.key")
    with pytest.raises(KeyStoreError, match="allow_bootstrap=False"):
        store.bootstrap_if_allowed(allow_bootstrap=False)
    assert not (tmp_path / "k.key").exists()


def test_file_bootstrap_with_opt_in_persists(tmp_path: Path) -> None:
    store = FileKeyStore("agent", path=tmp_path / "k.key")
    store.bootstrap_if_allowed(allow_bootstrap=True)
    assert (tmp_path / "k.key").exists()
    priv = store.load_private_key()
    assert priv is not None


def test_bootstrap_is_idempotent_when_key_exists(tmp_path: Path) -> None:
    key_path = tmp_path / "k.key"
    store = FileKeyStore("agent", path=key_path)
    store.bootstrap_if_allowed(allow_bootstrap=True)
    first = key_path.read_bytes()
    # Second call with allow_bootstrap=False must NOT raise (file exists).
    store.bootstrap_if_allowed(allow_bootstrap=False)
    assert key_path.read_bytes() == first
