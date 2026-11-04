"""Persistent browser instances shared across solves (biggest speed win).

Every solve used to cold-launch a browser (~4-6 s of the total latency). Keeping a
single warm Camoufox (and on-demand Patchright) instance alive in the process and
creating a fresh page per solve cuts per-solve time dramatically — browser startup
happens once, then each solve only opens a tab.

Because the server already serializes solves with an asyncio lock, a single shared
instance is safe (one page at a time). If a solve crashes the instance we tear it
down so the next call cold-starts cleanly.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_lock = asyncio.Lock()
_camo = None
_camo_ok = False
_patch = None          # playwright object
_patch_browser = None


async def get_camoufox():
    """Return a warm browser instance (the .browser attr after __aenter__)."""
    global _camo, _camo_ok
    from camoufox import AsyncCamoufox
    if _camo is None:
        _camo = AsyncCamoufox(headless=True)
        await _camo.__aenter__()
    return _camo.browser  # type: ignore[return-value]


async def close_camoufox():
    global _camo, _camo_ok
    if _camo is not None:
        try:
            await _camo.__aexit__(None, None, None)
        except Exception as e:
            log.warning("camoufox close: %s", e)
    _camo = None


async def get_patchright():
    """Return (browser) for a warm Patchright chromium instance."""
    global _patch, _patch_browser
    from patchright.async_api import async_playwright
    if _patch is None:
        _patch = await async_playwright().start()
        _patch_browser = await _patch.chromium.launch(headless=True)
    return _patch_browser  # type: ignore[return-value]


async def close_patchright():
    global _patch, _patch_browser
    for target in (_patch_browser, _patch):
        if target is not None:
            try:
                await target.close()
            except Exception:
                pass
    _patch = None
    _patch_browser = None


async def reset():
    """Tear down all warm instances (e.g. after a crash or on demand)."""
    async with _lock:
        await close_camoufox()
        await close_patchright()
