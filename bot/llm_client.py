"""
LLM Client — routes all AI calls through 9Router (OpenAI-compatible).
Falls back to direct OpenAI only if router unreachable AND OPENAI_API_KEY set.

Model policy:
  chat / fast / tiktok_chat / telegram_chat / search_summary -> cx/gpt-5.5
  reasoning / coding                                         -> cc/claude-opus-4-7
  critic                                                     -> cx/gpt-5.3-codex
  vision                                                     -> openai/gpt-4o
  cheap (explicit fallback only)                             -> openai/gpt-4o-mini

ENV overrides (all optional):
  LLM_CHAT_MODEL, LLM_FAST_MODEL, LLM_TIKTOK_MODEL, LLM_TELEGRAM_MODEL,
  LLM_SEARCH_MODEL, LLM_REASONING_MODEL, LLM_CODING_MODEL,
  LLM_CRITIC_MODEL, LLM_VISION_MODEL, LLM_FALLBACK_MODEL
"""
import asyncio
import json
import os
import re
import uuid
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")

ROUTER_BASE_URL = os.getenv("MODEL_ROUTER_BASE_URL", "").rstrip("/")
ROUTER_API_KEY  = os.getenv("MODEL_ROUTER_API_KEY", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")

# ── Role → ENV key ────────────────────────────────────────────────────────────
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
    "cheap":          "LLM_FALLBACK_MODEL",
}

# ── Role → hardcoded default (used when ENV is unset) ────────────────────────
ROLE_MODEL_DEFAULT: dict[str, str] = {
    "chat":           "cx/gpt-5.5",
    "fast":           "cx/gpt-5.5",
    "tiktok_chat":    "cx/gpt-5.5",
    "telegram_chat":  "cx/gpt-5.5",
    "search_summary": "cx/gpt-5.5",
    "reasoning":      "cc/claude-opus-4-7",
    "coding":         "cc/claude-opus-4-7",
    "critic":         "cx/gpt-5.3-codex",
    "vision":         "openai/gpt-4o",
    "cheap":          "openai/gpt-4o-mini",
}

# ── Preference chains (ordered fallback when default is unavailable) ──────────
_CHAT_PREF = [
    "cx/gpt-5.5", "cx/gpt-5.4", "openai/gpt-5.4",
    "cx/gpt-5.2", "openai/gpt-5.2", "openai/gpt-5",
    "openai/gpt-4o", "openai/gpt-4o-mini",
]
_CODING_PREF = [
    "cc/claude-opus-4-7", "cc/claude-opus-4-6", "cc/claude-sonnet-4-6",
    "cx/gpt-5.3-codex", "cx/gpt-5.2-codex", "cx/gpt-5.5",
]
_REASONING_PREF = [
    "cc/claude-opus-4-7", "openai/o3-pro", "openai/o3",
    "cc/claude-sonnet-4-6", "openai/o4-mini", "cx/gpt-5.5",
]

_ROLE_PREFERENCES: dict[str, list[str]] = {
    "chat":           _CHAT_PREF,
    "fast":           _CHAT_PREF,
    "tiktok_chat":    _CHAT_PREF,
    "telegram_chat":  _CHAT_PREF,
    "search_summary": _CHAT_PREF,
    "coding":         _CODING_PREF,
    "critic":         ["cx/gpt-5.3-codex", "cx/gpt-5.2-codex"] + _CODING_PREF,
    "reasoning":      _REASONING_PREF,
    "vision":         ["openai/gpt-4o", "cx/gpt-5.5"],
    "cheap":          ["openai/gpt-4o-mini", "openai/gpt-4.1-mini", "openai/gpt-4.1-nano"],
}

# ── Runtime caches ────────────────────────────────────────────────────────────
_available_models: Optional[set[str]] = None   # fetched once from /v1/models
_resolved_cache:   dict[str, str]     = {}     # role -> resolved model id


def _log(msg: str) -> None:
    import sys
    print(f"[llm] {msg}", flush=True)


# ── Model availability & resolution ──────────────────────────────────────────

async def _fetch_available_models() -> set[str]:
    """Fetch available model IDs from router. Cached after first successful call."""
    global _available_models
    if _available_models is not None:
        return _available_models
    if not (ROUTER_BASE_URL and ROUTER_API_KEY):
        _available_models = set()
        return _available_models
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{ROUTER_BASE_URL}/models",
                headers={"Authorization": f"Bearer {ROUTER_API_KEY}"},
            )
            data = r.json().get("data", [])
        _available_models = {m["id"] for m in data}
        _log(f"model_cache loaded count={len(_available_models)}")
    except Exception as e:
        _log(f"model_cache fetch error: {e}")
        _available_models = set()
    return _available_models


async def resolve_model(role: str) -> str:
    """
    Resolve the best available model for a role.
    Priority: ENV override → preference chain (filtered by available) → hardcoded default.
    Caches result per role.
    """
    if role in _resolved_cache:
        return _resolved_cache[role]

    # 1. ENV override
    env_key  = ROLE_MODEL_ENV.get(role, "LLM_CHAT_MODEL")
    from_env = os.getenv(env_key, "").strip()
    if from_env:
        _resolved_cache[role] = from_env
        return from_env

    # 2. Preference chain against available models
    available = await _fetch_available_models()
    if available:
        preferred_default = ROLE_MODEL_DEFAULT.get(role, "cx/gpt-5.5")
        for candidate in _ROLE_PREFERENCES.get(role, _CHAT_PREF):
            if candidate in available:
                if candidate != preferred_default:
                    _log(
                        f"model_policy requested={preferred_default} "
                        f"selected={candidate} role={role} reason=model_not_found"
                    )
                _resolved_cache[role] = candidate
                return candidate

    # 3. Hardcoded default (no availability check — router may be unreachable)
    default = ROLE_MODEL_DEFAULT.get(role, "cx/gpt-5.5")
    _resolved_cache[role] = default
    return default


def get_all_role_models() -> dict[str, str]:
    """Return resolved model for each role (from cache; empty string if not yet resolved)."""
    return {role: _resolved_cache.get(role, ROLE_MODEL_DEFAULT.get(role, "?"))
            for role in ROLE_MODEL_DEFAULT}


def invalidate_model_cache() -> None:
    """Force re-fetch on next resolve (e.g. after router restart)."""
    global _available_models
    _available_models = None
    _resolved_cache.clear()


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
                # If router returns model error and we have a fallback key, retry direct
                if use_router and OPENAI_API_KEY and r.status_code in (400, 404):
                    fallback_model = "gpt-4o-mini"
                    _log(f"fallback reason=router_error_{r.status_code} model={fallback_model} request_id={req_id}")
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
                        _log(f"fallback_ok model={fallback_model} chars={len(content or '')} request_id={req_id}")
                        return {"content": content, "model": fallback_model, "error": False}
                return {"content": None, "error": True, "error_detail": f"HTTP {r.status_code}"}

            # Parse — 9Router sometimes appends "\ndata: [DONE]" even with stream=False
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

        # Resolve active models for all key roles
        role_models: dict[str, str] = {}
        for role in ("chat", "tiktok_chat", "telegram_chat", "search_summary",
                     "reasoning", "coding", "critic", "cheap"):
            role_models[role] = await resolve_model(role)

        chat_model = role_models["chat"]
        test = await complete_text(
            "Trả lời đúng một dòng: ROUTER_OK",
            role="chat", max_tokens=10, timeout=12, source="router_test",
        )
        return {
            "reachable":    True,
            "provider":     "9router",
            "model_count":  len(models),
            "role_models":  role_models,
            "chat_model":   chat_model,
            "fast_model":   role_models.get("chat", chat_model),
            "test_pass":    not test.get("error"),
            "test_reply":   (test.get("content") or "")[:40],
        }
    except Exception as e:
        return {"reachable": False, "reason": str(e)[:100]}


async def list_models_top(n: int = 20) -> list[str]:
    """Return up to n model IDs from the router."""
    available = await _fetch_available_models()
    return sorted(available)[:n]
