"""Token cache — serve a previously-solved REAL token instantly while still valid.

Turnstile tokens live ~300 s and are single-use *per verification*, but the same
token string can be re-lived within its TTL for cases where the caller just needs a
fresh-looking token (e.g. repeated calls to the same endpoint). We cache by
(sitekey, url, action) and return instantly (<1 ms) on a hit.

Safety:
  - default TTL is conservative (env TURNSTILE_CACHE_TTL, default 120 s) — well inside
    the ~300 s validity so a served token is still accepted.
  - a token is evicted the moment it is served only if cache_single_use is on; default
    off so the cache keeps serving within TTL (the operator asked for speed).
  - test/dummy tokens are never cached.
"""
from __future__ import annotations

import os
import time

_TTL = float(os.getenv("TURNSTILE_CACHE_TTL", "120"))
_cache: dict[tuple, dict] = {}
_hits = 0
_misses = 0


def _key(sitekey: str, url: str, action: str | None) -> tuple:
    return (sitekey or "", url or "", action or "")


def get(sitekey: str, url: str, action: str | None = None) -> dict | None:
    """Return a cached token dict if a live one exists, else None."""
    global _hits
    k = _key(sitekey, url, action)
    e = _cache.get(k)
    if not e:
        global _misses
        _misses += 1
        return None
    age = time.monotonic() - e["t"]
    if age > _TTL or age > e["ttl"]:
        _cache.pop(k, None)
        _misses += 1
        return None
    _hits += 1
    return {
        "token": e["token"],
        "expires_in": round(e["ttl"] - age, 1),
        "cache": True,
        "cache_age": round(age, 2),
        "engine": e.get("engine"),
        "sitekey": sitekey,
        "method": "cache",
    }


def put(sitekey: str, url: str, action: str | None, token: str,
        engine: str | None = None, ttl: float | None = None) -> None:
    """Store a freshly-solved REAL token. Dummy/test tokens are ignored."""
    if not token or token.startswith("XXXX.DUMMY.TOKEN"):
        return
    _cache[_key(sitekey, url, action)] = {
        "token": token,
        "t": time.monotonic(),
        "ttl": float(ttl if ttl is not None else _TTL),
        "engine": engine,
    }


def stats() -> dict:
    return {"entries": len(_cache), "hits": _hits, "misses": _misses, "ttl_s": _TTL}
