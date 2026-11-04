"""Auto-extract a Cloudflare Turnstile sitekey from a target page via curl_cffi.

Scans the raw HTML + inline <script> bodies (no browser needed) for any of the
common ways a sitekey appears:

  - <div class="cf-turnstile" data-sitekey="0x4...">
  - <div data-sitekey="0x4..." data-callback="...">
  - window.getSiteKey / sitekey / site_key / cfSiteKey assignments
  - turnstile.render(el, { sitekey: '0x4...' })
  - JSON blobs containing "sitekey": "0x4..."

Cloudflare Turnstile sitekeys: real keys start `0x` + hex; the official TEST keys
start with `1x`/`2x`/`3x` + hex (ever-pass/ever-block/invisible). So the shared
shape is `<hex-digit>x<hex...>` (optionally `:<cloudflare-suffix>`). We target
that signature to tell Turnstile apart from reCAPTCHA/hCaptcha keys.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

log = logging.getLogger(__name__)

# A Turnstile sitekey: `0x` + base62-ish body (letters AND digits — real keys
# contain non-hex chars like Q/R/G), optional `:suffix`. Test keys use 1x/2x/3x.
SITEKEY_PAT = r"(?:0x[0-9a-zA-Z]{10,40}|[123]x0{15,}[0-9a-fA-F]{2,8})(?::[0-9a-zA-Z_-]+)?"
_SSEK_RE = re.compile(
    rf"data-sitekey\s*=\s*[\"']({SITEKEY_PAT})[\"']",
    re.IGNORECASE,
)
# JS object literal / assignment forms: sitekey: '<key>', sitekey = "<key>",
# window.sitekey / CF.turnstile.sitekey / etc.
_JS_RE = re.compile(
    rf"(?:sitekey|site_key|cfSiteKey|turnstileSiteKey)\s*[:=]\s*[\"']({SITEKEY_PAT})[\"']",
    re.IGNORECASE,
)
# A bare key of Turnstile length used as a last resort inside turnstile.render().
_RENDER_RE = re.compile(
    rf"turnstile\.render\s*\([^)]*?({SITEKEY_PAT})[^)]*?\)",
    re.IGNORECASE,
)
# Generic Turnstile-looking key inside double/single-quoted strings in scripts.
_GENERIC_RE = re.compile(rf"[\"']({SITEKEY_PAT})[\"']")


def _candidates(html: str) -> list[str]:
    found: list[str] = []
    for m in _SSEK_RE.finditer(html):
        found.append(m.group(1))
    for m in _JS_RE.finditer(html):
        found.append(m.group(1))
    for m in _RENDER_RE.finditer(html):
        found.append(m.group(1))
    for m in _GENERIC_RE.finditer(html):
        found.append(m.group(1))
    return found


def extract_sitekey_from_html(html: str) -> str | None:
    """Return the most likely turnstile sitekey from raw HTML, or None."""
    seen: list[str] = []
    for key in _candidates(html):
        if key not in seen:
            seen.append(key)
        if len(seen) >= 5:          # enough evidence, stop scanning
            break
    if not seen:
        return None
    # Prefer keys that look "real": turnstile keys are usually 31 hex + suffix.
    # Fall back to the first candidate that is at least 13 chars.
    long = [k for k in seen if len(k.split(":")[0]) >= 24]
    pick = (long or seen)[0]
    log.info("Sitekey candidates: %s → selected %s", seen[:5], pick)
    return pick


def _is_http(url: str) -> bool:
    return urlsplit(url).scheme in ("http", "https")


def fetch_page(url: str, proxy: str | None = None,
               timeout: float = 30.0) -> tuple[str | None, str | None]:
    """GET a page with curl_cffi (browser impersonation). Returns (html, error).

    Uses a real Chrome TLS/JA3 fingerprint so Cloudflare doesn't block the fetch
    before we even get to the widget.
    """
    try:
        from curl_cffi import requests as curl_req
    except Exception as e:                      # pragma: no cover
        return None, f"curl_cffi unavailable: {e}"

    kwargs: dict = {
        "timeout": timeout,
        "impersonate": "chrome",                # realistic TLS/JA3/HTTP2 fingerprint
        "headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "allow_redirects": True,
    }
    if proxy:
        kwargs["proxy"] = proxy

    try:
        resp = curl_req.get(url, **kwargs)
    except Exception as e:
        return None, f"request failed: {e}"
    if resp.status_code >= 400:
        return None, f"HTTP {resp.status_code}"
    return resp.text, None


_URL_KEY_CACHE: dict[str, str] = {}


def get_sitekey(url: str, proxy: str | None = None,
                timeout: float = 30.0) -> dict:
    """Extract a turnstile sitekey from a URL. Returns {sitekey, page_url, source}.

    Results are memoized per URL (sitekeys rarely change) so a cached-token lookup
    path doesn't re-fetch the page just to learn the sitekey again.
    """
    ck = f"{url}|{proxy or ''}"
    if ck in _URL_KEY_CACHE:
        return {"sitekey": _URL_KEY_CACHE[ck], "page_url": url, "source": "cache"}
    html, err = fetch_page(url, proxy=proxy, timeout=timeout)
    if err or html is None:
        return {"sitekey": None, "page_url": url, "error": err}
    key = extract_sitekey_from_html(html)
    if key:
        _URL_KEY_CACHE[ck] = key
    return {"sitekey": key, "page_url": url, "source": "html" if key else None}
