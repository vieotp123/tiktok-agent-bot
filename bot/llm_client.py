"""
LLM Client — routes all AI calls through 9Router (OpenAI-compatible).
Falls back to direct OpenAI only if router unreachable AND OPENAI_API_KEY set.
"""
import os
import asyncio
import httpx
from typing import Optional
from dotenv import load_dotenv

load_dotenv("/opt/tiktok-bot/.env")

ROUTER_BASE_URL = os.getenv("MODEL_ROUTER_BASE_URL", "").rstrip("/")
ROUTER_API_KEY  = os.getenv("MODEL_ROUTER_API_KEY", "")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")

ROLE_MODEL_ENV = {
    "fast":      "LLM_FAST_MODEL",
    "cheap":     "LLM_CHEAP_MODEL",
    "reasoning": "LLM_REASONING_MODEL",
    "coding":    "LLM_CODING_MODEL",
    "vision":    "LLM_VISION_MODEL",
}

ROLE_MODEL_DEFAULT = {
    "fast":      "openai/gpt-4o-mini",
    "cheap":     "openai/gpt-4.1-nano",
    "reasoning": "openai/o4-mini",
    "coding":    "openai/gpt-4.1",
    "vision":    "openai/gpt-4o",
}

_fallback_model: Optional[str] = None


def _get_model(role: str) -> str:
    env_key = ROLE_MODEL_ENV.get(role, "LLM_FAST_MODEL")
    from_env = os.getenv(env_key, "").strip()
    if from_env:
        return from_env
    return ROLE_MODEL_DEFAULT.get(role, ROLE_MODEL_DEFAULT["fast"])


def _log(msg: str) -> None:
    import sys
    print(f"[llm] {msg}", flush=True)


async def _fetch_first_model() -> Optional[str]:
    """Fetch first available model from router /v1/models."""
    if not ROUTER_BASE_URL or not ROUTER_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{ROUTER_BASE_URL}/models",
                headers={"Authorization": f"Bearer {ROUTER_API_KEY}"},
            )
            data = r.json()
            models = data.get("data", [])
            if models:
                return models[0]["id"]
    except Exception as e:
        _log(f"fetch_models error: {e}")
    return None


async def complete(
    messages: list[dict],
    role: str = "fast",
    temperature: float = 0.7,
    max_tokens: int = 600,
    timeout: float = 30,
) -> dict:
    """
    Call LLM via 9Router.
    Returns: {"content": str, "model": str, "error": False}
          or {"content": None, "error": True, "error_detail": str}
    """
    global _fallback_model

    # Choose provider + key
    use_router = bool(ROUTER_BASE_URL and ROUTER_API_KEY)
    base_url = ROUTER_BASE_URL if use_router else "https://api.openai.com/v1"
    api_key  = ROUTER_API_KEY  if use_router else OPENAI_API_KEY

    if not api_key:
        return {"content": None, "error": True, "error_detail": "no API key configured"}

    model = _get_model(role)
    provider = "9router" if use_router else "openai-direct"
    _log(f"provider={provider} role={role} model={model}")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
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
                _log(f"HTTP {r.status_code}: {detail}")
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                return {"content": None, "error": True, "error_detail": f"HTTP {r.status_code}"}

            # Parse — 9Router appends "\ndata: [DONE]" even with stream=false
            import json, re
            text = r.text.strip()
            # Strip trailing SSE artifacts
            text = re.sub(r'\ndata:\s*\[DONE\]\s*$', '', text).strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                # Try first complete JSON object
                m = re.search(r'^\{.*\}', text, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
                else:
                    data = json.loads(text.split("\n")[0])

            content = data["choices"][0]["message"]["content"]
            used_model = data.get("model", model)
            _log(f"ok model={used_model} chars={len(content or '')}")
            return {"content": content, "model": used_model, "error": False}

        except asyncio.TimeoutError:
            _log(f"timeout attempt={attempt}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return {"content": None, "error": True, "error_detail": "timeout"}
        except Exception as e:
            _log(f"error attempt={attempt}: {e}")
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return {"content": None, "error": True, "error_detail": str(e)[:120]}

    return {"content": None, "error": True, "error_detail": "max retries"}


async def complete_text(prompt: str, role: str = "fast", **kwargs) -> dict:
    return await complete([{"role": "user", "content": prompt}], role=role, **kwargs)


async def router_status() -> dict:
    """Check router connectivity and return status dict."""
    if not ROUTER_BASE_URL or not ROUTER_API_KEY:
        return {"reachable": False, "reason": "no router configured"}
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{ROUTER_BASE_URL}/models",
                headers={"Authorization": f"Bearer {ROUTER_API_KEY}"},
            )
            models = r.json().get("data", [])
        test = await complete_text("ROUTER_OK", role="fast", max_tokens=5, timeout=10)
        return {
            "reachable": True,
            "model_count": len(models),
            "fast_model": _get_model("fast"),
            "test_pass": not test.get("error"),
            "test_reply": (test.get("content") or "")[:30],
        }
    except Exception as e:
        return {"reachable": False, "reason": str(e)[:100]}
