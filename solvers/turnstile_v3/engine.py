# ============================================
# DITZZZAI
# powered by Ditzzz
# ============================================
"""Turnstile v3 — CDP-fulfil engine with shadow-root / OOPIF checkbox discovery.

Why this exists (each point is a fix for a concrete failure seen in this repo):

1. `page.route(...).fulfill()` (v1/v2) replaces the target document, so the widget
   renders at a *synthetic* origin. Turnstile tolerates it most of the time, but
   reCAPTCHA/hCaptcha do not, and Cloudflare is tightening the same check.
   `Fetch.fulfillRequest` via CDP does the same substitution at the network layer
   while the real navigation completes, which keeps the origin/cookie jar intact.

2. `for fr in page.frames` (v1/v2) only sees frame documents. Modern Turnstile
   renders inside a **closed shadow root** attached to an OOPIF, so the checkbox
   node is invisible to Playwright's frame list. We walk `DOM.getDocument(
   depth=-1, pierce=True)` and recurse shadow roots + OOPIF targets (adapted from
   sarperavci/CloudflareBypassForScraping core/challenge.py).

3. Cloudflare added `chlPageData` for invisible widgets; a solve missing it hangs
   until timeout. We pass it when supplied.

4. Test sitekeys (1x/2x/3x...) always return `XXXX.DUMMY.TOKEN.XXXX`. Reporting
   that as `solved=True` is a lie that cost us a whole debugging round, so the
   result is tagged `test_key` and `solved` is False for them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

WIDGET_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Verify</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
<style>html,body{margin:0;width:100%;height:100%;display:flex;align-items:center;
justify-content:center;background:#f7f7f7;font-family:-apple-system,Segoe UI,Roboto,sans-serif}
#tc{min-height:70px;display:flex;align-items:center;justify-content:center}</style>
</head><body>
<div id="tc"></div>
<script>
window.__tsToken=null; window.__tsError=null; window.__tsRendered=false;
function init(){
  if (typeof turnstile==='undefined'){ setTimeout(init,50); return; }
  try{
    const p={sitekey:__SITEKEY__,
      callback:t=>{window.__tsToken=t;},
      'error-callback':e=>{window.__tsError=String(e||'error');},
      'expired-callback':()=>{window.__tsError='expired';},
      'timeout-callback':()=>{window.__tsError='timeout';}};
    const a=__ACTION__, c=__CDATA__, d=__PAGEDATA__;
    if(a) p.action=a;
    if(c) p.cData=c;
    if(d) p.chlPageData=d;
    window.__tsWidget=turnstile.render('#tc',p);
    window.__tsRendered=true;
  }catch(e){ window.__tsError=String(e&&e.message?e.message:e); }
}
document.readyState==='loading'
  ? document.addEventListener('DOMContentLoaded',init) : init();
</script></body></html>"""


def render_widget(sitekey: str, action=None, cdata=None, pagedata=None) -> str:
    return (WIDGET_HTML
            .replace("__SITEKEY__", json.dumps(sitekey))
            .replace("__ACTION__", json.dumps(action))
            .replace("__CDATA__", json.dumps(cdata))
            .replace("__PAGEDATA__", json.dumps(pagedata)))


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


# ── shadow-root / OOPIF checkbox discovery (CBF4S technique) ──────────

_FIND_CB_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  function walk(root, depth) {
    if (!root || depth > 6) return;
    let els;
    try { els = root.querySelectorAll('*'); } catch (e) { return; }
    for (const el of els) {
      if (el.tagName === 'INPUT' && el.type === 'checkbox') out.push(el);
      if (el.shadowRoot && !seen.has(el.shadowRoot)) {   // open shadow roots
        seen.add(el.shadowRoot);
        walk(el.shadowRoot, depth + 1);
      }
    }
  }
  walk(document, 0);
  // top document needs its own scan (querySelectorAll('*') covers it, but the
  // loop above starts at document which has no shadowRoot)
  for (const el of document.querySelectorAll('input[type=checkbox]')) out.push(el);
  return out.map(e => {
    const r = e.getBoundingClientRect();
    return {checked: e.getAttribute('aria-checked'), disabled: !!e.disabled,
            x: r.x + r.width / 2, y: r.y + r.height / 2,
            w: r.width, h: r.height};
  });
}
"""


def _frames_deep(page):
    """Return [(frame, is_cloudflare)] for all frames incl. OOPIF challenge frames."""
    out = []
    for fr in page.frames:
        u = fr.url or ""
        out.append((fr, "challenges.cloudflare.com" in u))
    return out


async def find_checkbox(page):
    """Locate the Turnstile checkbox across shadow roots and cross-origin frames.

    Returns dict {x, y, frame} with page-absolute coordinates, or None.
    """
    # 1) same-document + open shadow roots
    try:
        boxes = await page.evaluate(_FIND_CB_JS)
        for b in boxes or []:
            if b.get("w", 0) >= 4 and b.get("checked") != "true":
                return {"x": b["x"], "y": b["y"], "frame": page.main_frame}
    except Exception:
        pass

    # 2) every frame (Playwright already exposes OOPIFs in .frames)
    for fr, is_cf in _frames_deep(page):
        try:
            el = await fr.frame_element()
            box = await el.bounding_box()
        except Exception:
            continue
        if not box or box.get("width", 0) < 20:
            continue
        # probe inside the frame for the real checkbox rect
        try:
            inner = await fr.evaluate(r"""
                () => {
                  const c = document.querySelector('input[type=checkbox]')
                         || document.querySelector('#challenge-stage input')
                         || document.querySelector('label');
                  if (!c) return null;
                  const r = c.getBoundingClientRect();
                  return {x: r.x + r.width/2, y: r.y + r.height/2,
                          w: r.width, h: r.height,
                          checked: c.getAttribute('aria-checked')};
                }
            """)
        except Exception:
            inner = None
        if inner and inner.get("checked") != "true":
            return {"x": box["x"] + inner["x"], "y": box["y"] + inner["y"], "frame": fr}
        if is_cf:
            # Cloudflare OOPIF: checkbox sits ~30px from the left edge, vertically centred
            return {"x": box["x"] + 30, "y": box["y"] + box["height"] / 2, "frame": fr}
    return None


async def click_checkbox(page, attempts: int = 25) -> bool:
    for _ in range(attempts):
        loc = await find_checkbox(page)
        if loc:
            try:
                await page.mouse.click(loc["x"], loc["y"])
            except Exception:
                pass
            return True
        await asyncio.sleep(1)
    return False


async def wait_token(page, timeout_s: int = 60) -> tuple[str, str]:
    """Poll the injected widget for a token or a Turnstile-side error."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = await page.evaluate(
                "() => ({t: window.__tsToken || "
                "(document.querySelector('[name=cf-turnstile-response]')||{}).value || '',"
                " e: window.__tsError || ''})")
            if r.get("t"):
                return r["t"], ""
            if r.get("e"):
                return "", r["e"]
        except Exception:
            pass
        await asyncio.sleep(0.7)
    return "", "timeout"


# ── CDP-fulfil: substitute the document without losing the origin ─────

async def solve_cdp(sitekey: str, url: str, action=None, cdata=None,
                    pagedata=None, proxy=None, backend="patchright",
                    timeout_s=60, headless=False) -> dict:
    """Serve the widget at the target origin via Fetch.fulfillRequest."""
    t0 = time.monotonic()
    html = render_widget(sitekey, action, cdata, pagedata)
    body_b64 = base64.b64encode(html.encode()).decode()
    origin = _origin(url).rstrip("/")

    browser, ctx, page, closer = await _launch(backend, proxy, headless)
    try:
        cdp = await ctx.new_cdp_session(page)
        await cdp.send("Fetch.enable",
                       {"patterns": [{"requestStage": "Request",
                                      "resourceType": "Document"}]})

        def on_paused(ev):
            req = ev.get("request", {})
            rid = ev.get("requestId")
            if not rid:
                return
            u = (req.get("url") or "").rstrip("/")
            if "challenges.cloudflare.com" in u:
                asyncio.create_task(cdp.send("Fetch.continueRequest", {"requestId": rid}))
                return
            if u.startswith(origin):
                asyncio.create_task(cdp.send("Fetch.fulfillRequest", {
                    "requestId": rid, "responseCode": 200,
                    "responseHeaders": [
                        {"name": "Content-Type", "value": "text/html; charset=utf-8"},
                        {"name": "Cache-Control", "value": "no-cache, no-store"},
                    ], "body": body_b64}))
            else:
                asyncio.create_task(cdp.send("Fetch.continueRequest", {"requestId": rid}))

        cdp.on("Fetch.requestPaused", on_paused)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log.info("nav raised (%s) — navigating anyway", type(e).__name__)

        try:
            await cdp.send("Fetch.disable")
        except Exception:
            pass

        await asyncio.sleep(2)
        token, err = await wait_token(page, timeout_s)
        if not token:
            clicked = await click_checkbox(page)
            log.info("checkbox clicked=%s", clicked)
            token, err = await wait_token(page, max(10, timeout_s // 2))

        return _finish(token, err, sitekey, t0, "cdp-fetch")
    finally:
        await closer()


async def solve_realpage(sitekey: str, url: str, action=None, cdata=None,
                         pagedata=None, proxy=None, backend="patchright",
                         timeout_s=60, headless=False) -> dict:
    """Load the genuine page, inject the widget, click through shadow roots."""
    t0 = time.monotonic()
    browser, ctx, page, closer = await _launch(backend, proxy, headless)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(1.5)
        await page.evaluate(
            """(args) => {
                 const [k, a, c, d] = args;
                 let host = document.getElementById('__dz_ts');
                 if (!host) {
                   host = document.createElement('div');
                   host.id = '__dz_ts';
                   host.style.cssText = 'position:fixed;top:8px;left:8px;z-index:2147483647';
                   document.body.prepend(host);
                 }
                 const boot = () => {
                   if (typeof turnstile === 'undefined') { setTimeout(boot, 60); return; }
                   const p = {sitekey: k,
                     callback: t => { window.__tsToken = t; },
                     'error-callback': e => { window.__tsError = String(e || 'error'); }};
                   if (a) p.action = a;
                   if (c) p.cData = c;
                   if (d) p.chlPageData = d;
                   turnstile.render(host, p);
                   window.__tsRendered = true;
                 };
                 if (typeof turnstile === 'undefined') {
                   const s = document.createElement('script');
                   s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
                   s.async = true;
                   s.onload = boot;
                   document.head.appendChild(s);
                 } else { boot(); }
               }""",
            [sitekey, action, cdata, pagedata])
        await asyncio.sleep(3)
        token, err = await wait_token(page, timeout_s)
        if not token:
            clicked = await click_checkbox(page)
            log.info("checkbox clicked=%s", clicked)
            token, err = await wait_token(page, max(10, timeout_s // 2))

        result = _finish(token, err, sitekey, t0, "real-page")
        if token:
            try:
                result["cookies"] = await ctx.cookies()
            except Exception:
                pass
        return result
    finally:
        await closer()


# ── engine plumbing ──────────────────────────────────────────────────

async def _launch(backend: str, proxy, headless: bool):
    """Return (browser, context, page, closer)."""
    proxy_cfg = None
    if proxy:
        if isinstance(proxy, dict):
            # already split — preferred: Playwright takes username/password as
            # separate fields, so no percent-encoding hazards on chars like ~ +
            server = proxy["server"].replace("http://", "").replace("https://", "")
            proxy_cfg = {"server": f"http://{server}"}
            if proxy.get("username"):
                proxy_cfg["username"] = proxy["username"]
            if proxy.get("password"):
                proxy_cfg["password"] = proxy["password"]
        elif "@" in proxy:
            from urllib.parse import unquote
            scheme_free = proxy.split("://", 1)[-1]
            auth, server = scheme_free.rsplit("@", 1)
            u, p = auth.split(":", 1)
            proxy_cfg = {"server": f"http://{server}",
                         "username": unquote(u), "password": unquote(p)}
        else:
            proxy_cfg = {"server": f"http://{proxy}"}

    if backend == "camoufox":
        from camoufox.async_api import AsyncCamoufox
        cm = AsyncCamoufox(headless=headless, proxy=proxy_cfg, geoip=True)
        browser = await cm.__aenter__()
        ctx = await browser.new_context()
        page = await ctx.new_page()

        async def closer():
            try:
                await ctx.close()
            except Exception:
                pass
            await cm.__aexit__(None, None, None)
        return browser, ctx, page, closer

    from patchright.async_api import async_playwright
    pw = await async_playwright().start()
    args = ["--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check"]
    browser = await pw.chromium.launch(headless=headless, args=args, proxy=proxy_cfg)
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US", timezone_id="America/New_York")
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = await ctx.new_page()

    async def closer():
        for t in (ctx, browser):
            try:
                await t.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass
    return browser, ctx, page, closer


def _finish(token: str, err: str, sitekey: str, t0: float, method: str) -> dict:
    """Normalise the result and refuse to call a dummy/test token a success."""
    token = token or ""
    test_key = sitekey.lower().startswith(("1x", "2x", "3x"))
    dummy = token.startswith("XXXX.DUMMY") or token == "XXXX.DUMMY.TOKEN.XXXX"
    real = bool(token) and not test_key and not dummy
    out = {
        "token": token,
        "solved": real,
        "method": method,
        "sitekey": sitekey,
        "elapsed": round(time.monotonic() - t0, 1),
    }
    if test_key:
        out["test_key"] = True
        out["note"] = "Cloudflare TEST sitekey (1x/2x/3x) — dummy token, cannot pass siteverify"
    elif dummy:
        out["test_key"] = True
        out["note"] = "dummy token — sitekey is a CF test key or widget was stubbed"
    if err:
        out["error"] = err
    return out


async def solve(url: str, sitekey: str, action=None, cdata=None, pagedata=None,
                proxy=None, backend="patchright", method="cdp",
                timeout_s=60, headless=False) -> dict:
    """Entry point. method='cdp' (origin-preserving) | 'real-page'."""
    if method == "real-page":
        return await solve_realpage(sitekey, url, action, cdata, pagedata,
                                    proxy, backend, timeout_s, headless)
    return await solve_cdp(sitekey, url, action, cdata, pagedata,
                           proxy, backend, timeout_s, headless)
