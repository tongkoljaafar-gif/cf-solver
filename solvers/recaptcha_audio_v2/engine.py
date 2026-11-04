# ============================================
# DITZZZAI
# powered by Ditzzz
# ============================================
"""reCAPTCHA v2 audio solver — no route interception.

The v1 solver failed for one reason: it served a synthetic page with
`page.route(...).fulfill()`, so the widget rendered at the wrong origin and
`#recaptcha-anchor` never existed. This module never touches the document — it
drives the *real* page, clicks the anchor, falls back to the audio challenge,
and transcribes it.

Audio path adapted from sarperavci/GoogleRecaptchaBypass (the approach the
FuckCaptcha knowledge base recommends): download `#audio-source`, transcode with
ffmpeg, transcribe with a recognizer. We prefer Whisper when installed because
Google's endpoint rate-limits and occasionally returns empty for datacenter IPs;
SpeechRecognition's Google backend stays as the zero-dependency fallback.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

TMP = Path(tempfile.gettempdir())


# ── transcription backends ───────────────────────────────────────────

def _transcribe_speechrecognition(wav: str) -> str:
    import speech_recognition as sr
    r = sr.Recognizer()
    with sr.AudioFile(wav) as src:
        audio = r.record(src)
    return r.recognize_google(audio)


_whisper_model = None


def _transcribe_whisper(wav: str) -> str:
    """faster-whisper if available (better on noisy audio), else openai-whisper."""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
        if _whisper_model is None:
            _whisper_model = WhisperModel("tiny.en", device="cpu", compute_type="int8")
        segs, _ = _whisper_model.transcribe(wav, language="en", beam_size=1)
        return " ".join(s.text for s in segs).strip()
    except ImportError:
        pass
    import whisper
    if _whisper_model is None:
        _whisper_model = whisper.load_model("tiny.en")
    return _whisper_model.transcribe(wav, language="en")["text"].strip()


def transcribe(mp3_bytes: bytes, prefer: str = "auto") -> str:
    """mp3 bytes -> text. Tries Whisper first, falls back to Google SR."""
    tag = random.randrange(1, 10 ** 9)
    mp3 = TMP / f"rc_{tag}.mp3"
    wav = TMP / f"rc_{tag}.wav"
    try:
        mp3.write_bytes(mp3_bytes)
        try:
            from pydub import AudioSegment
            AudioSegment.from_mp3(str(mp3)).export(str(wav), format="wav")
        except Exception:
            # no pydub/ffmpeg python binding — shell out
            os.system(f'ffmpeg -y -loglevel error -i "{mp3}" "{wav}" 2>/dev/null')
        if not wav.exists() or wav.stat().st_size < 200:
            raise RuntimeError("transcode failed (ffmpeg/pydub missing?)")

        errors = []
        order = ["whisper", "google"] if prefer in ("auto", "whisper") else ["google", "whisper"]
        for backend in order:
            try:
                text = (_transcribe_whisper if backend == "whisper"
                        else _transcribe_speechrecognition)(str(wav))
                text = (text or "").strip().lower()
                if text:
                    return text
                errors.append(f"{backend}: empty")
            except Exception as e:
                errors.append(f"{backend}: {type(e).__name__}: {e}")
        raise RuntimeError("all recognizers failed — " + " | ".join(errors))
    finally:
        for p in (mp3, wav):
            try:
                p.unlink()
            except Exception:
                pass


# ── browser driving ──────────────────────────────────────────────────

async def _launch(proxy, headless):
    proxy_cfg = None
    if proxy:
        if "@" in proxy:
            auth, server = proxy.split("@", 1)
            u, p = auth.split(":", 1)
            proxy_cfg = {"server": f"http://{server}", "username": u, "password": p}
        else:
            proxy_cfg = {"server": f"http://{proxy}"}
    try:
        from patchright.async_api import async_playwright
        engine = "patchright"
    except ImportError:
        from playwright.async_api import async_playwright
        engine = "playwright"

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              "--disable-blink-features=AutomationControlled",
              "--no-first-run", "--no-default-browser-check",
              "--autoplay-policy=no-user-gesture-required"],
        proxy=proxy_cfg)
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 900}, locale="en-US",
        timezone_id="America/New_York", accept_downloads=True)
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
    return engine, page, closer


async def _anchor_frame(page, timeout_s=25):
    """Return the reCAPTCHA anchor frame once it renders."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for fr in page.frames:
            u = (fr.url or "").lower()
            # anchor frame: google.com/recaptcha/<key>/anchor (or enterprise)
            if "recaptcha" in u and ("anchor" in u or "enterprise/anchor" in u):
                return fr
            # some builds put the anchor at recaptcha/api2/anchor
            if "api2/anchor" in u:
                return fr
        await asyncio.sleep(0.4)
    return None


async def _is_solved(page) -> str:
    """Return the token if the widget reports solved, else ''."""
    for fr in page.frames:
        try:
            tok = await fr.evaluate(
                "() => { const e=document.getElementById('recaptcha-token');"
                " return e ? e.value : '' }")
            if tok:
                return tok
        except Exception:
            pass
    try:
        tok = await page.evaluate(
            "() => { const e=document.querySelector('[name=g-recaptcha-response]');"
            " return e ? e.value : '' }")
        return tok or ""
    except Exception:
        return ""


async def solve_recaptcha_v2(url: str, sitekey: str | None = None,
                             proxy: str | None = None, headless: bool = False,
                             timeout_s: int = 150, inject: bool = True,
                             pre_actions: list | None = None,
                             prefer_recognizer: str = "auto") -> dict:
    """Open the real page, click the anchor, solve the audio challenge.

    Returns {token, solved, method, elapsed, attempts, error?}.
    """
    t0 = time.monotonic()
    attempts: list[dict] = []
    engine, page, closer = await _launch(proxy, headless)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        if pre_actions:
            for act in pre_actions:
                try:
                    kind = act.get("type")
                    if kind == "click" and act.get("selector"):
                        await page.click(act["selector"], timeout=8000)
                    elif kind == "fill" and act.get("selector"):
                        await page.fill(act["selector"], act.get("value", ""), timeout=8000)
                    await asyncio.sleep(0.7)
                except Exception as e:
                    log.info("pre_action %s failed: %s", act, e)

        if inject and sitekey:
            await page.evaluate(
                """(k) => {
                     if (document.querySelector('[data-dz-rc]')) return;
                     const d = document.createElement('div');
                     d.className = 'g-recaptcha';
                     d.setAttribute('data-sitekey', k);
                     d.setAttribute('data-dz-rc', '1');
                     document.body.prepend(d);
                     const s = document.createElement('script');
                     s.src = 'https://www.google.com/recaptcha/api.js';
                     s.async = true; s.defer = true;
                     document.head.appendChild(s);
                   }""", sitekey)
            await asyncio.sleep(4)

        for round_i in range(3):
            tok = await _is_solved(page)
            if tok:
                return {"token": tok, "solved": True, "method": "passive",
                        "attempts": attempts, "engine": engine,
                        "elapsed": round(time.monotonic() - t0, 1)}

            anchor = await _anchor_frame(page, 40)
            if not anchor:
                frames = [(fr.url or "")[:90] for fr in page.frames]
                return {"token": "", "solved": False,
                        "error": "anchor frame never appeared",
                        "frames": frames,
                        "attempts": attempts, "engine": engine,
                        "elapsed": round(time.monotonic() - t0, 1)}

            # 1) click the checkbox
            clicked = False
            for sel in ("#recaptcha-anchor", ".recaptcha-checkbox-border",
                        ".rc-anchor-content", "label"):
                try:
                    await anchor.click(sel, timeout=4000)
                    clicked = True
                    break
                except Exception:
                    continue
            attempts.append({"round": round_i, "clicked": clicked})
            await asyncio.sleep(2.5)

            tok = await _is_solved(page)
            if tok:
                return {"token": tok, "solved": True, "method": "click",
                        "attempts": attempts, "engine": engine,
                        "elapsed": round(time.monotonic() - t0, 1)}

            # 2) flip to the audio challenge
            bframe = None
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                for fr in page.frames:
                    u = fr.url or ""
                    if "recaptcha" in u and ("bframe" in u or "challenge" in u):
                        bframe = fr
                        break
                if bframe:
                    break
                await asyncio.sleep(0.4)
            if not bframe:
                attempts.append({"round": round_i, "bframe": "not found"})
                await _reload_widget(page)
                continue

            try:
                await bframe.click("#recaptcha-audio-button", timeout=8000)
            except Exception as e:
                attempts.append({"round": round_i, "audio_btn": f"{type(e).__name__}"})
                await _reload_widget(page)
                continue
            await asyncio.sleep(2)
            attempts.append({"round": round_i, "audio": "opened"})

            # 3) pull the audio source and transcribe
            src = ""
            for _ in range(20):
                try:
                    src = await bframe.evaluate(
                        "() => { const a=document.getElementById('audio-source');"
                        " return a ? a.src : '' }")
                except Exception:
                    src = ""
                if src:
                    break
                await asyncio.sleep(0.8)

            if not src:
                attempts.append({"round": round_i, "audio_src": "missing"})
                await _reload_widget(page)
                continue

            try:
                resp = await page.request.get(src)
                audio = await resp.body()
                text = transcribe(audio, prefer=prefer_recognizer)
                attempts.append({"round": round_i, "transcript": text[:60]})
            except Exception as e:
                attempts.append({"round": round_i,
                                 "transcribe": f"{type(e).__name__}: {e}"[:120]})
                await _reload_widget(page)
                continue

            try:
                box = await bframe.query_selector("#audio-response")
                if box:
                    await box.fill(text)
                else:
                    await bframe.fill("#audio-response", text)
                await bframe.click("#recaptcha-verify-button", timeout=8000)
            except Exception as e:
                attempts.append({"round": round_i, "submit": f"{type(e).__name__}"})
                continue
            await asyncio.sleep(3)

            tok = await _is_solved(page)
            if tok:
                return {"token": tok, "solved": True, "method": "audio",
                        "attempts": attempts, "engine": engine,
                        "elapsed": round(time.monotonic() - t0, 1)}

            await _reload_widget(page)

        return {"token": "", "solved": False, "error": "3 rounds exhausted",
                "attempts": attempts, "engine": engine,
                "elapsed": round(time.monotonic() - t0, 1)}
    except Exception as e:
        return {"token": "", "solved": False,
                "error": f"{type(e).__name__}: {e}"[:200],
                "attempts": attempts, "elapsed": round(time.monotonic() - t0, 1)}
    finally:
        await closer()


async def _reload_widget(page):
    """Ask reCAPTCHA to hand out a fresh challenge without reloading the page."""
    for fr in page.frames:
        try:
            ok = await fr.evaluate(
                "() => { if (window.grecaptcha && grecaptcha.reset) {"
                " grecaptcha.reset(); return true } return false }")
            if ok:
                await asyncio.sleep(2)
                return
        except Exception:
            pass
    try:
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)
    except Exception:
        pass
