"""In-memory solver stats & request history for the web UI.

Tracks per-request latency and outcome so the dashboard can show a live solver
model (total requests, success rate, avg/median response time) plus a history
list. Purely in-memory; survives for the life of the process.
"""
from __future__ import annotations

import time
from collections import deque

_MAX_HISTORY = 200
_STARTED_AT = time.time()

# history: list of dicts (newest appended), plus a running summary
_history: deque = deque(maxlen=_MAX_HISTORY)
_summary = {
    "total": 0,
    "ok": 0,
    "fail": 0,
    "total_ms": 0.0,
    "max_ms": 0.0,
    "started_at": _STARTED_AT,
}


def record(req_type: str, elapsed_s: float, solved: bool,
           engine: str | None = None, url: str | None = None,
           token: str | None = None, error: str | None = None,
           backend: str | None = None, cache: bool = False) -> dict:
    """Record one solve attempt. Returns the created history entry."""
    ms = round(elapsed_s * 1000, 1)
    entry = {
        "ts": time.time(),
        "time": time.strftime("%H:%M:%S"),
        "type": req_type,
        "engine": engine or backend or "-",
        "ms": ms,
        "solved": bool(solved),
        "cache": bool(cache),
        "url": (url or "")[:60],
        "token": bool(token),
        "error": (error or "")[:120],
    }
    _history.append(entry)
    s = _summary
    s["total"] += 1
    s["total_ms"] += ms
    if ms > s["max_ms"]:
        s["max_ms"] = ms
    if solved:
        s["ok"] += 1
    else:
        s["fail"] += 1
    return entry


def snapshot() -> dict:
    """Return the current summary + per-model breakdown + full history (newest first)."""
    s = _summary
    n = s["total"]
    return {
        "total": n,
        "ok": s["ok"],
        "fail": s["fail"],
        "success_rate": round(s["ok"] / n * 100, 1) if n else 0.0,
        "avg_ms": round(s["total_ms"] / n, 1) if n else 0.0,
        "max_ms": s["max_ms"],
        "uptime_s": round(time.time() - _STARTED_AT, 1),
        "models": _by_model(),
        "history": list(reversed(_history)),
    }


def _by_model() -> list[dict]:
    """Aggregate history into per-model buckets (keyed by type+engine)."""
    buckets: dict[str, dict] = {}
    for h in _history:
        key = f"{h['type']}|{h['engine']}"
        b = buckets.setdefault(key, {
            "type": h["type"], "engine": h["engine"],
            "total": 0, "ok": 0, "fail": 0, "total_ms": 0.0, "max_ms": 0.0,
        })
        b["total"] += 1
        b["total_ms"] += h["ms"]
        if h["ms"] > b["max_ms"]:
            b["max_ms"] = h["ms"]
        if h["solved"]:
            b["ok"] += 1
        else:
            b["fail"] += 1
    out = []
    for b in buckets.values():
        t = b["total"]
        out.append({
            "type": b["type"],
            "engine": b["engine"],
            "total": t,
            "ok": b["ok"],
            "fail": b["fail"],
            "success_rate": round(b["ok"] / t * 100, 1) if t else 0.0,
            "avg_ms": round(b["total_ms"] / t, 1) if t else 0.0,
            "max_ms": b["max_ms"],
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out
