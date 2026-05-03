"""
Intent stats — log every admin NL intent + a short reason, then surface
"Bro hay dùng X" suggestions after N uses.

Design rules:
  - Deterministic, no LLM calls. Same fail-soft style as audit_log.
  - Persistence: data/intent_stats.json (gitignored). Atomic write via
    tmp + replace. Small file (one entry per intent name) — no log
    rotation needed.
  - record_intent() never raises on its own: callers wrap loosely but
    we still swallow IO errors so a flaky disk never breaks the
    Telegram routing path.
  - Suggestion fires ONCE per intent (when it crosses the threshold),
    then is suppressed by the `suggested` flag so we don't spam the
    admin every message after.
  - Excluded from suggestion: chat / unknown / ambiguous. Those are
    fall-throughs by definition; recommending them is meaningless.

Public API:
    record_intent(name, reason="", raw_text="") -> None
    record_and_check(name, reason="", raw_text="", threshold=DEFAULT_THRESHOLD)
        -> Optional[str]   # the Vietnamese tip if newly threshold-crossed
    top_intents(n=5)       -> list[dict]
    stats()                -> dict
    reset()                -> None  # primarily for tests
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_STATE_FILE = Path("/opt/tiktok-bot/data/intent_stats.json")

DEFAULT_THRESHOLD = 5
RECENT_TEXT_CAP   = 5
RAW_TEXT_TRIM     = 80

# Intents that should never trigger a "you use X a lot" suggestion —
# they are catch-alls or disambiguation prompts.
_NO_SUGGEST = {"chat", "unknown", "ambiguous"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_state() -> dict:
    return {
        "by_intent":         {},
        "total_messages":    0,
        "suggested_intents": [],
    }


def _load() -> dict:
    if not _STATE_FILE.exists():
        return _empty_state()
    try:
        d = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return _empty_state()
        out = _empty_state()
        out.update(d)
        if not isinstance(out.get("by_intent"), dict):
            out["by_intent"] = {}
        if not isinstance(out.get("suggested_intents"), list):
            out["suggested_intents"] = []
        return out
    except Exception:
        return _empty_state()


def _save(d: dict) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp + replace inside the same dir so we don't
        # leave a half-written JSON if the process is killed.
        fd, tmp = tempfile.mkstemp(prefix="intent_stats_",
                                   suffix=".tmp",
                                   dir=str(_STATE_FILE.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=2, ensure_ascii=False)
            os.replace(tmp, _STATE_FILE)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
    except Exception:
        pass


def record_intent(name: str,
                  reason: str = "",
                  raw_text: str = "") -> None:
    """Bump the counter for `name` and remember a short reason.

    Always increments total_messages. Tracks first/last seen, last
    reason, and the last few raw-text samples (capped at
    RECENT_TEXT_CAP). Never raises.
    """
    if not name:
        return
    try:
        d = _load()
        d["total_messages"] = int(d.get("total_messages") or 0) + 1
        bucket = d["by_intent"].setdefault(name, {
            "count":         0,
            "first_used_at": _now_iso(),
            "last_used_at":  "",
            "last_reason":   "",
            "recent_texts":  [],
        })
        bucket["count"]        = int(bucket.get("count") or 0) + 1
        bucket["last_used_at"] = _now_iso()
        if reason:
            bucket["last_reason"] = str(reason)[:120]
        if raw_text:
            samples = list(bucket.get("recent_texts") or [])
            samples.append(str(raw_text)[:RAW_TEXT_TRIM])
            bucket["recent_texts"] = samples[-RECENT_TEXT_CAP:]
        _save(d)
    except Exception:
        pass


def _suggestion_text_vi(name: str, count: int) -> str:
    return (f"💡 Bro hay dùng <b>{name}</b> ({count} lần). "
            f"Em ghim làm shortcut nhé — gõ <code>/skill_pin {name}</code> "
            f"để thêm vào menu nhanh.")


def record_and_check(name: str,
                     reason: str = "",
                     raw_text: str = "",
                     threshold: int = DEFAULT_THRESHOLD) -> Optional[str]:
    """Record the intent and return a suggestion message if `name` just
    crossed the use threshold for the first time. Returns None
    otherwise (most calls).
    """
    if not name:
        return None
    record_intent(name, reason=reason, raw_text=raw_text)
    if name in _NO_SUGGEST:
        return None
    try:
        d = _load()
        suggested = list(d.get("suggested_intents") or [])
        if name in suggested:
            return None
        bucket = (d.get("by_intent") or {}).get(name) or {}
        count = int(bucket.get("count") or 0)
        if count < int(threshold):
            return None
        suggested.append(name)
        d["suggested_intents"] = suggested
        _save(d)
        return _suggestion_text_vi(name, count)
    except Exception:
        return None


def top_intents(n: int = 5) -> list[dict]:
    """Return the top-N intents by count, sorted desc.

    Each row: {name, count, last_used_at, last_reason}. Excludes
    intents with count == 0 (shouldn't happen, but defensive).
    """
    d = _load()
    rows = []
    for name, bucket in (d.get("by_intent") or {}).items():
        c = int(bucket.get("count") or 0)
        if c <= 0:
            continue
        rows.append({
            "name":         name,
            "count":        c,
            "last_used_at": bucket.get("last_used_at") or "",
            "last_reason":  (bucket.get("last_reason") or "")[:80],
        })
    rows.sort(key=lambda r: (-r["count"], r["name"]))
    return rows[: max(1, int(n))]


def stats() -> dict:
    """Return the full state dict (defensive copy)."""
    return _load()


def reset() -> None:
    """Wipe all intent stats — primarily for tests. Always safe."""
    try:
        if _STATE_FILE.exists():
            _STATE_FILE.unlink()
    except Exception:
        pass
