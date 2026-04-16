"""POLISH 2: bounded LRU caches negative results.

An operator who starts the process with a missing env var sees
``UNAVAILABLE`` forever for that ``(fingerprint, uri)`` pair until
``clear_key_cache()`` runs — documented footgun. This test pins the
behavior so a future refactor can't silently change it.
"""
from __future__ import annotations

import base64

from codeatelier_governance.audit import keys as _keys


def test_lru_cache_negative_result_survives_env_var_provision(
    monkeypatch,
) -> None:
    _keys.clear_key_cache()
    # Build a real 32-byte key and compute its fingerprint so the
    # URI resolves to the right material.
    key_bytes = b"k" * 32
    fp = _keys.fingerprint_key(key_bytes)
    var = "GOVERNANCE_TEST_NEGCACHE_KEY"
    monkeypatch.delenv(var, raising=False)

    uri_map = {fp: f"env://{var}"}

    # First resolve: env var missing → UNAVAILABLE, result cached.
    res1 = _keys.resolve_key(fp, uri_map)
    assert res1.status is _keys.KeyResolution.UNAVAILABLE

    # Provide the env var mid-process.
    monkeypatch.setenv(var, base64.b64encode(key_bytes).decode("ascii"))

    # Without clear_key_cache(), the cached negative result sticks.
    res2 = _keys.resolve_key(fp, uri_map)
    assert res2.status is _keys.KeyResolution.UNAVAILABLE, (
        "cached negative result should survive env provisioning — "
        "this is the documented footgun"
    )

    _keys.clear_key_cache()

    res3 = _keys.resolve_key(fp, uri_map)
    assert res3.status is _keys.KeyResolution.RESOLVED
    assert res3.material == key_bytes
