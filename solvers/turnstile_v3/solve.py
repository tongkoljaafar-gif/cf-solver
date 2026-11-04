# ============================================
# DITZZZAI
# powered by Ditzzz
# ============================================
"""Turnstile v3 solver — thin adapter over engine.solve()."""
from __future__ import annotations

import asyncio

from solvers.turnstile_v3.engine import solve as _solve
from solvers.turnstile_v3.engine import render_widget  # re-export

_locks: dict[str, asyncio.Lock] = {}


def _lock_for(backend: str) -> asyncio.Lock:
    if backend not in _locks:
        _locks[backend] = asyncio.Lock()
    return _locks[backend]


async def solve_turnstile(sitekey: str, url: str, action: str | None = None,
                          cdata: str | None = None, pagedata: str | None = None,
                          proxy: str | None = None, backend: str = "patchright",
                          method: str = "cdp", timeout_s: int = 60,
                          headless: bool = False, **_) -> dict:
    """Solve a real Turnstile widget and return {token, solved, method, ...}.

    backend : 'patchright' (default, Chromium) | 'camoufox' (Firefox, better for CF)
    method  : 'cdp' (Fetch.fulfillRequest — keeps the origin, default) | 'real-page'
    proxy   : 'user:pass@host:port' — residential strongly recommended
    headless: False on a box with Xvfb; True only with camoufox
    """
    async with _lock_for(backend):
        return await _solve(
            url=url, sitekey=sitekey, action=action, cdata=cdata,
            pagedata=pagedata, proxy=proxy, backend=backend,
            method=method, timeout_s=timeout_s, headless=headless)
