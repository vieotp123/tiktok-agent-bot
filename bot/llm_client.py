"""
LLM Client — routes all AI calls through 9Router (OpenAI-compatible).
Falls back to direct OpenAI only if router unreachable AND OPENAI_API_KEY set.

Model policy:
  chat / fast / tiktok_chat / telegram_chat / search_summary
      → cx/gpt-5.5 (first available from chat preference chain)
  coding / reasoning
      → cc/claude-sonnet-4-7 (if available)
        cc/claude-sonnet-4-6 (currently available ✅)
        cc/claude-opus-4-7   (currently available ✅)
        cc/claude-opus-4-6   (currently available ✅)
        cx/gpt-5.3-codex → cx/gpt-5.5 → openai/gpt-4.1 (fallback chain)
  critic
      → cx/gpt-5.3-codex → cc/claude-sonnet-4-6 → openai/gpt-4.1
  cheap / fallback
      → openai/gpt-4o-mini → openai/gpt-4.1-nano

ENV overrides (all optional):
  LLM_CHAT_MODEL, LLM_FAST_MODEL, LLM_TIKTOK_MODEL, LLM_TELEGRAM_MODEL,
  LLM_SEARCH_MODEL, LLM_REASONING_MODEL, LLM_CODING_MODEL,
  LLM_CRITIC_MODEL, LLM_VISION_MODEL, LLM_CHEAP_MODEL, LLM_FALLBACK_MODEL

ENV override validation:
  If the env-specified model IS available in router /models → use it.
  If NOT available in router /models → log warning, fall back to preference chain.

Model cache:
  Available models are fetched from router /models and cached for 5 minutes.
  Call invalidate_model_cache() to force refresh.
"""
import asyncio
import json
import os
import re
import time
import uuid
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")

ROUTER_BASE_URL = os.getenv("MODEL_ROUTER_BASE_URL", "").rstrip("/")
ROUTER_API_KEY  = os.getenv("MODEL_ROUTER_API_KEY", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")

# ── Role → ENV override key ───────────────────────────────────────────────────
ROLE_MODEL_ENV: dict[str, str] = {
    "chat":           "LLM_CHAT_MODEL",
    "fast":           "LLM_FAST_MODEL",
    "tiktok_chat":    "LLM_TIKTOK_MODEL",
    "telegram_chat":  "LLM_TELEGRAM_MODEL",
    "search_summary": "LLM_SEARCH_MODEL",
    "reasoning":      "LLM_REASONING_MODEL",
    "coding":         "LLM_CODING_MODEL",
    "critic":         "LLM_CRITIC_MODEL",
    "vision":         "LLM_VISION_MODEL",
    "cheap":          "LLM_CHEAP_MODEL",
    "fallback":       "LLM_FALLBACK_MODEL",
}

# ── Role → hardcoded default (first model in each preference chain) ───────────
ROLE_MODEL_DEFAULT: dict[str, str] = {
    "chat":           "cx/gpt-5.5",
    "fast":           "cx/gpt-5.5",
    "tiktok_chat":    "cx/gpt-5.5",
    "telegram_chat":  "cx/gpt-5.5",
    "search_summary": "cx/gpt-5.5",
    "reasoning":      "cc/claude-sonnet-4-7",
    "coding":         "cc/claude-sonnet-4-7",
    "critic":         "cx/gpt-5.3-codex",
    "vision":         "openai/gpt-4o",
    "cheap":          "openai/gpt-4o-mini",
    "fallback":       "openai/gpt-4o-mini",
}

# ── Preference chains (ordered — first available wins) ───────────────────────
#
# CHAT: prefer cx/gpt-5.5, then other gpt-5 variants, then gpt-4o as last resort
_CHAT_PREF = [
    "cx/gpt-5.5",
    "openai/gpt-5.5",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
    "openai/gpt-5",
    "cx/gpt-5.4",
    "cx/gpt-5.2",
    "openai/gpt-5.2",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
]

# CODING: Claude Sonnet/Opus first, then codex, then gpt fallbacks
_CODING_PREF = [
    "cc/claude-sonnet-4-7",     # ideal — not yet in router (2026-05-02)
    "cc/claude-sonnet-4-6",     # ✅ confirmed available
    "cc/claude-opus-4-7",       # ✅ confirmed available
    "cc/claude-opus-4-6",       # ✅ confirmed available
    "cc/claude-sonnet-4-5-20250929",
    "cc/claude-opus-4-5-20251101",
    "cx/gpt-5.3-codex",
    "cx/gpt-5.3-codex-high",
    "cx/gpt-5.2-codex",
    "cx/gpt-5.5",
    "openai/gpt-4.1",
    "openai/gpt-4o-mini",
]

# REASONING: same Claude preference, then o3 for deep reasoning
_REASONING_PREF = [
    "cc/claude-sonnet-4-7",     # ideal — not yet in router
    "cc/claude-sonnet-4-6",     # ✅ confirmed available
    "cc/claude-opus-4-7",       # ✅ confirmed available
    "cc/claude-opus-4-6",       # ✅ confirmed available
    "cc/claude-sonnet-4-5-20250929",
    "cc/claude-opus-4-5-20251101",
    "cx/gpt-5.3-codex",
    "cx/gpt-5.5",
    "openai/o3-pro",
    "openai/o3",
    "openai/o4-mini",
    "openai/gpt-4.1",
    "openai/gpt-4o-mini",
]

# CRITIC: codex first (best for code review), then Claude, then gpt fallback
_CRITIC_PREF = [
    "cx/gpt-5.3-codex",
    "cx/gpt-5.3-codex-high",
    "cx/gpt-5.2-codex",
    "cc/claude-sonnet-4-7",
    "cc/claude-sonnet-4-6",
    "cc/claude-opus-4-7",
    "openai/gpt-4.1",
    "cx/gpt-5.5",
    "openai/gpt-4o-mini",
]

_ROLE_PREFERENCES: dict[str, list[str]] = {
    "chat":           _CHAT_PREF,
    "fast":           _CHAT_PREF,
    "tiktok_chat":    _CHAT_PREF,
    "telegram_chat":  _CHAT_PREF,
    "search_summary": _CHAT_PREF,
    "coding":         _CODING_PREF,
    "reasoning":      _REASONING_PREF,
    "critic":         _CRITIC_PREF,
    "vision":         ["openai/gpt-4o", "cx/gpt-5.5", "openai/gpt-4o-mini"],
    "cheap":          ["openai/gpt-4o-mini", "openai/gpt-4.1-nano", "openai/gpt-4.1-mini"],
    "fallback":       ["openai/gpt-4o-mini", "openai/gpt-4.1-nano"],
}

# ── Model cache (5-minute TTL) ────────────────────────────────────────────────
_available_models:     Optional[set[str]] = None
_available_models_ts:  float              = 0.0   # epoch seconds of last fetch
_MODEL_CACHE_TTL:      float              = 300.0  # 5 minutes
_resolved_cache:       dict[str, str]     = {}     # role → resolved model id


def _log(msg: str) -> None:
    import sys
    print(f"[llm] {msg}", flush=True)


def _log_policy(role: str, selected: str, source: str,
                requested: str = "", reason: str = "") -> None:
    """Structured model-policy log line."""
    if reason:
        print(
            f"[model_policy] role={role} requested={requested}"
            f" selected={selected} reason={reason}",
            flush=True,
        )
    else:
        print(f"[model_policy] role={role} selected={selected} source={source}",
              flush=True)


# ── Model availability & resolution ──────────────────────────────────────────

async def list_router_models_cached() -> set[str]:
    """
    Fetch available model IDs from router. Cached for 5 minutes.
    Returns empty set if router unreachable.
    """
    global _available_models, _available_models_ts

    now = time.monotonic()
    if _available_models is not None and (now - _available_models_ts) < _MODEL_CACHE_TTL:
        return _available_models   # cache hit

    if not (ROUTER_BASE_URL and ROUTER_API_KEY):
        _available_models = set()
        _available_models_ts = now
        return _available_models

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{ROUTER_BASE_URL}/models",
                headers={"Authorization": f"Bearer {ROUTER_API_KEY}"},
            )
            data = r.json().get("data", [])
        _available_models = {m["id"] for m in data}
        _available_models_ts = now
        _log(f"model_cache loaded count={len(_available_models)}")
    except Exception as e:
        _log(f"model_cache fetch error: {e}")
        if _available_models is None:
            _available_models = set()
        _available_models_ts = now   # don't hammer on failure
    return _available_models


async def model_available(model_id: str) -> bool:
    """Return True if model_id is listed in the router's /models."""
    available = await list_router_models_cached()
    return model_id in available


async def select_model(role: str) -> str:
    """
    Select the best available model for a role.

    Priority:
      1. ENV override  — if the model is available in /models → use it
                         if NOT available → log warning, fall to pref chain
      2. Preference chain — walk in order, return first available
      3. Hardcoded default — if router unreachable or all prefs missing

    Result is NOT cached here (cache is at resolve_model level).
    """
    # 1. ENV override
    env_key   = ROLE_MODEL_ENV.get(role, "")
    from_env  = os.getenv(env_key, "").strip() if env_key else ""

    if from_env:
        available = await list_router_models_cached()
        if not available or from_env in available:
            # Use ENV model: router not reachable OR model confirmed available
            _log_policy(role, from_env, "env_override")
            return from_env
        else:
            # ENV model not in /models — warn and fall through to pref chain
            _log_policy(
                role, "",
                source="",
                requested=from_env,
                reason="env_model_not_in_router_fallback_to_pref_chain",
            )

    # 2. Preference chain — first model available in /models
    available = await list_router_models_cached()
    if available:
        preferred_default = ROLE_MODEL_DEFAULT.get(role, "cx/gpt-5.5")
        for candidate in _ROLE_PREFERENCES.get(role, _CHAT_PREF):
            if candidate in available:
                if candidate != preferred_default:
                    _log_policy(
                        role, candidate,
                        source="preference",
                        requested=preferred_default,
                        reason="preferred_default_not_available",
                    )
                else:
                    _log_policy(role, candidate, "preference")
                return candidate

    # 3. Hardcoded default (router unreachable)
    default = ROLE_MODEL_DEFAULT.get(role, "cx/gpt-5.5")
    _log_policy(role, default, "hardcoded_default")
    return default


async def resolve_model(role: str) -> str:
    """
    Select the best model for a role (cached per role, respects 5-min model cache).
    The per-role cache is invalidated when the model cache is refreshed.
    """
    # If model list cache has expired, invalidate per-role cache too
    global _available_models_ts
    if _available_models is not None:
        age = time.monotonic() - _available_models_ts
        if age > _MODEL_CACHE_TTL and role in _resolved_cache:
            del _resolved_cache[role]

    if role in _resolved_cache:
        return _resolved_cache[role]

    model = await select_model(role)
    _resolved_cache[role] = model
    return model


async def get_role_models() -> dict[str, str]:
    """Return resolved model for all roles. Used by /router_status and /models."""
    roles = list(ROLE_MODEL_DEFAULT.keys())
    result = {}
    for role in roles:
        result[role] = await resolve_model(role)
    return result


def get_all_role_models() -> dict[str, str]:
    """Return cached resolved model per role (from cache; may be stale)."""
    return {role: _resolved_cache.get(role, ROLE_MODEL_DEFAULT.get(role, "?"))
            for role in ROLE_MODEL_DEFAULT}


def invalidate_model_cache() -> None:
    """Force re-fetch of /models on next call. Clears per-role cache too."""
    global _available_models, _available_models_ts
    _available_models    = None
    _available_models_ts = 0.0
    _resolved_cache.clear()
    _log("model_cache invalidated")


# ── Core completion call ──────────────────────────────────────────────────────

async def complete(
    messages:    list[dict],
    role:        str   = "chat",
    temperature: float = 0.7,
    max_tokens:  int   = 600,
    timeout:     float = 30,
    source:      str   = "backend",
) -> dict:
    """
    Call LLM via 9Router (preferred) or direct OpenAI (fallback).

    Returns:
        {"content": str,  "model": str, "error": False}
      | {"content": None, "error": True, "error_detail": str}

    Every call logs:
        [llm] provider=9router role=<role> model=<model> source=<source> request_id=<id>
    """
    req_id = str(uuid.uuid4())[:8]

    use_router = bool(ROUTER_BASE_URL and ROUTER_API_KEY)
    if use_router:
        base_url = ROUTER_BASE_URL
        api_key  = ROUTER_API_KEY
        provider = "9router"
        model    = await resolve_model(role)
    else:
        base_url = "https://api.openai.com/v1"
        api_key  = OPENAI_API_KEY
        provider = "openai-direct"
        model    = ROLE_MODEL_DEFAULT.get(role, "openai/gpt-4o-mini").split("/")[-1]
        _log(f"fallback reason=no_router_configured role={role}")

    if not api_key:
        return {"content": None, "error": True, "error_detail": "no API key configured"}

    _log(f"provider={provider} role={role} model={model} source={source} request_id={req_id}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    payload = {
        "model":       model,
        "messages":    messages,
        "max_tokens":  max_tokens,
        "temperature": temperature,
        "stream":      False,
    }

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )

            if r.status_code != 200:
                detail = r.text[:200]
                _log(f"HTTP {r.status_code} request_id={req_id}: {detail}")
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                # Router model error → try direct OpenAI fallback
                if use_router and OPENAI_API_KEY and r.status_code in (400, 404):
                    fallback_model = "gpt-4o-mini"
                    _log(f"fallback reason=router_error_{r.status_code}"
                         f" model={fallback_model} request_id={req_id}")
                    payload["model"] = fallback_model
                    async with httpx.AsyncClient(timeout=timeout) as c2:
                        r2 = await c2.post(
                            "https://api.openai.com/v1/chat/completions",
                            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                                     "Content-Type": "application/json"},
                            json=payload,
                        )
                    if r2.status_code == 200:
                        content = r2.json()["choices"][0]["message"]["content"]
                        _log(f"fallback_ok model={fallback_model}"
                             f" chars={len(content or '')} request_id={req_id}")
                        return {"content": content, "model": fallback_model, "error": False}
                return {"content": None, "error": True,
                        "error_detail": f"HTTP {r.status_code}"}

            # Parse — 9Router sometimes appends "\ndata: [DONE]" with stream=False
            text = r.text.strip()
            text = re.sub(r'\ndata:\s*\[DONE\]\s*$', '', text).strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r'^\{.*\}', text, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
                else:
                    data = json.loads(text.split("\n")[0])

            content    = data["choices"][0]["message"]["content"]
            used_model = data.get("model", model)
            _log(f"ok model={used_model} chars={len(content or '')} request_id={req_id}")
            return {"content": content, "model": used_model, "error": False}

        except asyncio.TimeoutError:
            _log(f"timeout attempt={attempt} request_id={req_id}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return {"content": None, "error": True, "error_detail": "timeout"}
        except Exception as e:
            _log(f"error attempt={attempt} request_id={req_id}: {e}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return {"content": None, "error": True, "error_detail": str(e)[:120]}

    return {"content": None, "error": True, "error_detail": "max retries"}


async def complete_text(prompt: str, role: str = "chat", **kwargs) -> dict:
    return await complete([{"role": "user", "content": prompt}], role=role, **kwargs)


# ── Router status ─────────────────────────────────────────────────────────────

async def router_status() -> dict:
    """Check router connectivity and return a rich status dict."""
    if not (ROUTER_BASE_URL and ROUTER_API_KEY):
        return {"reachable": False, "reason": "no router configured"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{ROUTER_BASE_URL}/models",
                headers={"Authorization": f"Bearer {ROUTER_API_KEY}"},
            )
            models = r.json().get("data", [])

        # Resolve active models for all roles (re-uses cache)
        role_models = await get_role_models()
        chat_model  = role_models.get("chat", "cx/gpt-5.5")

        test = await complete_text(
            "Trả lời đúng một dòng: ROUTER_OK",
            role="chat", max_tokens=10, timeout=12, source="router_test",
        )
        return {
            "reachable":   True,
            "provider":    "9router",
            "model_count": len(models),
            "role_models": role_models,
            "chat_model":  chat_model,
            "fast_model":  role_models.get("fast", chat_model),
            "test_pass":   not test.get("error"),
            "test_reply":  (test.get("content") or "")[:40],
        }
    except Exception as e:
        return {"reachable": False, "reason": str(e)[:100]}


async def list_models_top(n: int = 20) -> list[str]:
    """Return up to n model IDs from the router."""
    available = await list_router_models_cached()
    return sorted(available)[:n]


async def list_models_relevant() -> dict[str, list[str]]:
    """
    Return available models grouped by family — used by /models Telegram command.
    Groups: claude, codex, gpt5, gpt4, reasoning, cheap
    """
    available = await list_router_models_cached()
    groups: dict[str, list[str]] = {
        "claude":    [],
        "codex":     [],
        "gpt5":      [],
        "gpt4":      [],
        "reasoning": [],
        "cheap":     [],
    }
    for m in sorted(available):
        ml = m.lower()
        if "claude" in ml or "sonnet" in ml or "opus" in ml or "haiku" in ml:
            groups["claude"].append(m)
        elif "codex" in ml:
            groups["codex"].append(m)
        elif "gpt-5" in ml or "gpt5" in ml:
            groups["gpt5"].append(m)
        elif "o3" in ml or "o4" in ml:
            groups["reasoning"].append(m)
        elif "gpt-4" in ml or "gpt4" in ml:
            groups["gpt4"].append(m)
        if "mini" in ml or "nano" in ml:
            groups["cheap"].append(m)
    return groups
