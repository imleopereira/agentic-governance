"""F6 Track B: bounded LRU cache for fingerprint -> key resolution.

The DA flagged unbounded cache growth as a blocker. This test pins the
max size (64) and verifies basic eviction semantics.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from codeatelier_governance.audit import keys as keys_mod
from codeatelier_governance.audit.keys import (
    KeyResolution,
    _LRU_MAX_ENTRIES,
    cache_info,
    clear_key_cache,
    fingerprint_key,
    resolve_key,
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    clear_key_cache()
    yield
    clear_key_cache()


def test_lru_max_is_bounded_at_64() -> None:
    assert _LRU_MAX_ENTRIES == 64
    info = cache_info()
    assert info.maxsize == 64


def test_resolve_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    key = b"example-key-bytes-32-chars-long!!"
    fp = fingerprint_key(key)
    monkeypatch.setenv("TEST_F6_KEY_1", key.decode("utf-8"))
    uri_map = {fp: "env://TEST_F6_KEY_1"}
    result = resolve_key(fp, uri_map)
    assert result.status is KeyResolution.RESOLVED
    assert result.material == key


def test_missing_env_returns_unavailable() -> None:
    fp = fingerprint_key(b"zzzz")
    result = resolve_key(fp, {fp: "env://NONEXISTENT_VAR_FOR_F6_TEST"})
    assert result.status is KeyResolution.UNAVAILABLE
    assert result.material is None


def test_missing_fingerprint_in_map_returns_unavailable() -> None:
    fp = fingerprint_key(b"zzzz")
    result = resolve_key(fp, {})
    assert result.status is KeyResolution.UNAVAILABLE


def test_fingerprint_mismatch_returns_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If URI resolves to bytes whose fingerprint != expected, refuse."""
    real_key = b"real-key" * 4
    other_key = b"other-ky" * 4
    fp_real = fingerprint_key(real_key)
    monkeypatch.setenv("TEST_F6_MISMATCH", other_key.decode("utf-8"))
    result = resolve_key(fp_real, {fp_real: "env://TEST_F6_MISMATCH"})
    assert result.status is KeyResolution.UNAVAILABLE


def test_lru_eviction_at_maxsize(monkeypatch: pytest.MonkeyPatch) -> None:
    """After 64 distinct entries, the LRU must start evicting.

    The cache is bounded by ``functools.lru_cache(maxsize=64)``, so the
    65th insertion evicts the least-recently-used entry. We verify by
    inspecting ``cache_info().currsize``.
    """
    clear_key_cache()
    for i in range(100):
        k = f"key-bytes-for-eviction-test-{i:03d}".encode("utf-8")
        fp = fingerprint_key(k)
        monkeypatch.setenv(f"TEST_F6_EVICT_{i}", k.decode("utf-8"))
        resolve_key(fp, {fp: f"env://TEST_F6_EVICT_{i}"})
    info = cache_info()
    assert info.currsize <= _LRU_MAX_ENTRIES
    assert info.currsize == _LRU_MAX_ENTRIES  # saturated


def test_clear_cache_resets_state(monkeypatch: pytest.MonkeyPatch) -> None:
    k = b"clearable-key-bytes-000000000000"
    fp = fingerprint_key(k)
    monkeypatch.setenv("TEST_F6_CLEAR", k.decode("utf-8"))
    resolve_key(fp, {fp: "env://TEST_F6_CLEAR"})
    assert cache_info().currsize >= 1
    clear_key_cache()
    assert cache_info().currsize == 0
