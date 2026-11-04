"""Turnstile solver using Camoufox or Patchright as the browser engine.

This is a drop-in replacement for `solvers/turnstile/solve.py` that swaps the
CloakBrowser engine for Camoufox (stealth Firefox) or Patchright (patched
Chromium). It also adds `auto` sitekey extraction via curl_cffi when the caller
only knows the target URL.

Two solving strategies, mirroring the original:

1. route-extract (fake page) — serve a local HTML with the widget at the target
   origin and read the hidden [name=cf-turnstile-response] token.
2. real-page — load the actual site, optionally inject the widget, click the
   checkbox inside the cross-origin Cloudflare iframe, harvest the token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from solvers.turnstile_v2.sitekey import get_sitekey, SITEKEY_PAT
from solvers.turnstile_v2 import browser_pool, token_cache

log = logging.getLogger(__name__)
_solve_lock = asyncio.Lock()
_TEMPLATE_PATH = Path(__file__).parent / "template.html"
HTML_TEMPLATE = _TEMPLATE_PATH.read_text()

TURNSTILE_KEY_RE = SITEKEY_PAT


# ── Route-extract (fake page) ─────────────────────────────────────────

def _build_page_data(sitekey: str, action: str | None, cdata: str | None) -> str:
    div = (f'<div class="cf-turnstile" data-sitekey="{sitekey}"'
           + (f' data-action="{action}"' if action else "")
           + (f' data-cdata="{cdata}"' if cdata else "")
           + '></div>')
    return HTML_TEMPLATE.replace("<!-- cf turnstile -->", div)


async def _wait_for_token(page, max_attempts: int = 20) -> str:
    """Token harvest shared by both engines (same locator API)."""
    for _ in range(max_attempts):
        try:
            val = await page.input_value("[name=cf-turnstile-response]")
        except Exception:
            val = ""
        if val == "":
            try:
                await page.click("//div[@class='cf-turnstile']", timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(1)
            continue
        el = await page.query_selector("[name=cf-turnstile-response]")
        if el:
            return await el.get_attribute("value")
        await asyncio.sleep(1)
    raise TimeoutError("Token not received via route-extract")


def _route_glob(url: str) -> str:
    """Glob that matches the target with or without trailing slash."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.path in ("", "/"):
        return f"{parts.scheme}://{parts.netloc}/**"
    return url


# ── Camoufox backend ──────────────────────────────────────────────────

async def solve_with_camoufox(sitekey: str, url: str, action: str | None = None,
                              cdata: str | None = None, proxy: str | None = None,
                              real_page: bool = False) -> dict:
    page_data = _build_page_data(sitekey, action, cdata)
    browser = await browser_pool.get_camoufox()
    page = await browser.new_page()
    try:
        if not real_page:
            target = url
            await page.route(_route_glob(target),
                             lambda r: r.fulfill(body=page_data, status=200))
            await page.goto(target, wait_until="domcontentloaded")
            token = await _wait_for_token(page)
        else:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await _inject_widget(page, sitekey)
            await asyncio.sleep(3)
            await _click_checkbox(page)
            token = await _wait_for_token(page, max_attempts=30)
        cookies = await page.context.cookies()
        return {"token": token, "cookies": cookies, "engine": "camoufox",
                "method": "real-page" if real_page else "route"}
    finally:
        await page.close()


# ── Patchright backend ────────────────────────────────────────────────

async def solve_with_patchright(sitekey: str, url: str, action: str | None = None,
                                cdata: str | None = None, proxy: str | None = None,
                                real_page: bool = False) -> dict:
    page_data = _build_page_data(sitekey, action, cdata)
    browser = await browser_pool.get_patchright()
    ctx = await browser.new_context()
    page = await ctx.new_page()
    try:
        if not real_page:
            target = url
            await page.route(_route_glob(target),
                             lambda r: r.fulfill(body=page_data, status=200))
            await page.goto(target, wait_until="domcontentloaded")
            token = await _wait_for_token(page)
        else:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await _inject_widget(page, sitekey)
            await asyncio.sleep(3)
            await _click_checkbox(page)
            token = await _wait_for_token(page, max_attempts=30)
        cookies = await ctx.cookies()
        return {"token": token, "cookies": cookies, "engine": "patchright",
                "method": "real-page" if real_page else "route"}
    finally:
        await page.close()
        await ctx.close()


# ── Shared real-page helpers ──────────────────────────────────────────

_WIDGET_INJECT_JS = (
    "(k) => {"
    "  const d = document.createElement('div');"
    "  d.className = 'cf-turnstile';"
    "  d.setAttribute('data-sitekey', k);"
    "  document.body.prepend(d);"
    "}"
)


async def _inject_widget(page, sitekey: str) -> None:
    try:
        await page.evaluate(_WIDGET_INJECT_JS, sitekey)
    except Exception as e:
        log.warning("widget inject failed: %s", e)


async def _click_checkbox(page, attempts: int = 25) -> bool:
    for _ in range(attempts):
        for fr in page.frames:
            if "challenges.cloudflare.com" in (fr.url or ""):
                for sel in ("input[type=checkbox]", "label", "body"):
                    try:
                        await fr.click(sel, timeout=2000)
                        return True
                    except Exception:
                        continue
        await asyncio.sleep(1)
    return False


# ── Public orchestrator ───────────────────────────────────────────────

async def solve_turnstile(url: str, sitekey: str | None = None,
                          action: str | None = None, cdata: str | None = None,
                          proxy: str | None = None,
                          backend: str = "camoufox",
                          real_page: bool = False,
                          timeout_s: int = 60) -> dict:
    """Solve a Turnstile. If `sitekey` is omitted it is auto-extracted from `url`.

    backend: "camoufox" (default) | "patchright"
    """
    t0 = time.monotonic()
    key = sitekey
    grabbed = None

    # If no sitekey given, we must resolve it before we can look up the cache.
    if not key:
        g = await asyncio.to_thread(get_sitekey, url, proxy)
        key = g.get("sitekey")
        grabbed = g if not key else {"sitekey": key, "page_url": url}
        if not key:
            return {"token": "", "error": g.get("error") or "no sitekey found on page",
                    "grabbed": g, "elapsed": round(time.monotonic() - t0, 1)}
        log.info("Auto-grabbed sitekey %s from %s", key, url)

    # Cache hit: serve a still-valid real token instantly (sub-ms).
    cached = token_cache.get(key, url, action)
    if cached:
        cached["elapsed"] = round(time.monotonic() - t0, 3)
        cached["grabbed"] = grabbed
        return cached

    try:
        if backend == "patchright":
            r = await solve_with_patchright(key, url, action, cdata, proxy, real_page)
        else:
            r = await solve_with_camoufox(key, url, action, cdata, proxy, real_page)
    except TimeoutError as e:
        r = {"token": "", "error": str(e), "engine": backend, "method": "route"}
    # A Cloudflare TEST sitekey (1x/2x/3x) always yields a DUMMY token that will
    # NOT pass a real siteverify — surface it clearly instead of reporting success.
    token = r.get("token") or ""
    is_test_key = bool(key) and key.lower().startswith(("1x", "2x", "3x"))
    if is_test_key:
        r["test_key"] = True
        r["note"] = "Cloudflare TEST sitekey — token is a dummy and won't pass real siteverify"
    elif token and token.startswith("XXXX.DUMMY.TOKEN"):
        r["test_key"] = True
        r["note"] = "Got a dummy token — sitekey is a Cloudflare test key or challenge was stub"
    r["sitekey"] = key
    r["grabbed"] = grabbed
    r["elapsed"] = round(time.monotonic() - t0, 1)
    # Cache the freshly-solved REAL token so the next call for the same
    # (sitekey,url,action) returns instantly while it is still valid.
    if token and not r.get("test_key"):
        token_cache.put(key, url, action, token, engine=r.get("engine"))
    return r
