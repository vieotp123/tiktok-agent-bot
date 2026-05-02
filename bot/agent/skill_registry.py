"""
Skill Registry — all capabilities the agent can invoke.

risk_level:
  low    → execute immediately
  medium → execute + log (no prompt)
  high   → create pending_action, require /confirm_action <id>

Tool Registry v2 additions:
  - `discover_skills()` — re-imports the registry module so a deploy
    that adds a new built-in `register(Skill(...))` line picks the
    skill up at startup without any code-side wiring.
  - `compute_skill_stats(...)` — derives runs / successes /
    last_used / stale from the audit log, no extra DB.
  - `set_skill_enabled(name, enabled)` — persists an admin override
    in `data/skill_overrides.json` (gitignored). Overrides are
    re-applied on every import so the toggle survives a restart.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

OVERRIDES_FILE = Path("/opt/tiktok-bot/data/skill_overrides.json")
STALE_DAYS_DEFAULT = 14


@dataclass
class Skill:
    name: str
    description: str
    risk_level: str          # low / medium / high
    enabled: bool
    handler: str             # function/tool identifier string
    examples: list[str] = field(default_factory=list)


_REGISTRY: dict[str, Skill] = {}


def register(skill: Skill) -> None:
    _REGISTRY[skill.name] = skill


def get_skill(name: str) -> Skill | None:
    return _REGISTRY.get(name)


def list_skills(enabled_only: bool = False) -> list[Skill]:
    skills = list(_REGISTRY.values())
    if enabled_only:
        skills = [s for s in skills if s.enabled]
    return skills


# ── Tool Registry v2 — overrides, discovery, stats ──────────────────────────


def _load_overrides() -> dict[str, bool]:
    try:
        if OVERRIDES_FILE.exists():
            data = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                return {str(k): bool(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save_overrides(data: dict[str, bool]) -> None:
    OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def apply_overrides() -> int:
    """Apply persisted enable/disable overrides to the in-memory registry.
    Returns how many skills were touched."""
    overrides = _load_overrides()
    n = 0
    for name, enabled in overrides.items():
        s = _REGISTRY.get(name)
        if s is not None and s.enabled != bool(enabled):
            s.enabled = bool(enabled)
            n += 1
    return n


def set_skill_enabled(name: str, enabled: bool) -> bool:
    """Persist + apply an admin override. Returns True if skill exists."""
    s = _REGISTRY.get(name)
    if s is None:
        return False
    s.enabled = bool(enabled)
    overrides = _load_overrides()
    overrides[name] = bool(enabled)
    _save_overrides(overrides)
    return True


# Default modules consulted by `discover_skills()` to verify each
# `Skill.handler` string actually points to a real callable. Order
# matters only insofar as the first match wins.
_HANDLER_LOOKUP_MODULES = (
    "bot.tools",
    "bot.agent.runner",
    "bot.telegram_bot",
    "bot.seo_research",
    "bot.content_factory",
    "bot.ocr",
    "backend.server",
)


def _handler_exists(handler: str) -> bool:
    if not handler:
        return False
    import importlib
    for mod_name in _HANDLER_LOOKUP_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if callable(getattr(mod, handler, None)):
            return True
    return False


def discover_skills() -> list[dict]:
    """Auto-discover the skills registered with this module.

    Returns one dict per skill with the live `Skill` plus:
      - `handler_found`: bool — does the `handler` string resolve to a
        callable in one of the known modules? Useful so /skills can flag
        a skill whose handler was renamed or removed without its
        registry entry being cleaned up.

    The registry is populated when this module is imported (every
    `register(Skill(...))` call below executes at import time), so
    "discover at startup" is satisfied by Python's import system. This
    helper just re-applies the persisted admin overrides and returns
    the live, enriched list.
    """
    apply_overrides()
    rows: list[dict] = []
    for s in _REGISTRY.values():
        rows.append({
            "skill":         s,
            "handler_found": _handler_exists(s.handler),
        })
    return rows


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except Exception:
        return None


def _action_to_skill(action: str) -> str:
    """Map an audit `action` field back to a skill name.
    Handles `run_task:<skill>` prefix and bare `<skill>`."""
    if not action:
        return ""
    if action.startswith("run_task:"):
        return action.split(":", 1)[1]
    return action


def compute_skill_stats(stale_days: int = STALE_DAYS_DEFAULT) -> dict[str, dict]:
    """Walk the audit log once, return per-skill stats:

        {
          "<skill_name>": {
            "runs": int,
            "successes": int,
            "success_rate": float,   # 0.0–1.0
            "last_used":  "<ISO ts>" | "",
            "stale":      bool,      # last_used older than `stale_days`
          }
        }

    Skills with zero runs are still present in the output but `runs=0`
    and `stale=True`. The audit log path is read lazily so this works
    in environments where the file does not yet exist."""
    from bot.agent.audit_log import AUDIT_FILE  # local import: avoid cycle

    known = {s.name for s in _REGISTRY.values()}
    stats: dict[str, dict] = {
        name: {"runs": 0, "successes": 0, "success_rate": 0.0,
               "last_used": "", "stale": True}
        for name in known
    }

    try:
        if AUDIT_FILE.exists():
            with open(AUDIT_FILE, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    skill = _action_to_skill(e.get("action", ""))
                    if skill not in known:
                        continue
                    s = stats[skill]
                    s["runs"] += 1
                    if e.get("status") in ("ok", "done"):
                        s["successes"] += 1
                    ts = e.get("timestamp", "")
                    if ts and ts > s["last_used"]:
                        s["last_used"] = ts
    except Exception:
        # Best-effort; bad audit lines must never break /skills.
        pass

    cutoff = datetime.now(timezone.utc).timestamp() - stale_days * 86400
    for s in stats.values():
        if s["runs"]:
            s["success_rate"] = round(s["successes"] / s["runs"], 3)
        last = _parse_ts(s["last_used"])
        s["stale"] = (last is None) or (last.timestamp() < cutoff)
    return stats


# Note: `apply_overrides()` is invoked at the end of this module
# (after all built-in `register(Skill(...))` calls below), so the
# persisted admin toggles are applied to a fully-populated registry.


# ── Register built-in skills ─────────────────────────────────────────────────

register(Skill(
    name="chat",
    description="Trả lời câu hỏi thông thường qua LLM (9Router/gpt-4o-mini).",
    risk_level="low",
    enabled=True,
    handler="call_llm",
    examples=["Ổn chưa", "kể chuyện hay nghe", "mày là ai"],
))

register(Skill(
    name="btc_price",
    description="Lấy giá Bitcoin realtime từ CoinGecko (USD/JPY/VND).",
    risk_level="low",
    enabled=True,
    handler="get_btc_price",
    examples=["giá btc hôm nay", "bitcoin bao nhiêu", "btc tăng chưa"],
))

register(Skill(
    name="search_web",
    description="Tìm kiếm web qua DuckDuckGo, tóm tắt kết quả bằng LLM.",
    risk_level="low",
    enabled=True,
    handler="search_web",
    examples=[
        "tìm thông tin mới nhất về Sơn Tùng",
        "search AI 2025",
        "mới nhất về eSIM Nhật",
    ],
))

register(Skill(
    name="router_status",
    description="Kiểm tra kết nối và trạng thái 9Router LLM.",
    risk_level="low",
    enabled=True,
    handler="router_status",
    examples=["/router_status"],
))

register(Skill(
    name="status",
    description="Xem trạng thái systemd services: tiktok-bot, backend, telegram.",
    risk_level="low",
    enabled=True,
    handler="handle_status",
    examples=["/status"],
))

register(Skill(
    name="models",
    description="Liệt kê các LLM model đang cấu hình trong .env.",
    risk_level="low",
    enabled=True,
    handler="handle_models",
    examples=["/models"],
))

register(Skill(
    name="send_tiktok_dm",
    description="Gửi tin nhắn vào TikTok DM — cần xác nhận trước khi gửi.",
    risk_level="high",
    enabled=True,
    handler="send_message",
    examples=["gửi DM 'Xin chào' vào chat"],
))

register(Skill(
    name="restart_service",
    description="Restart systemd service — cần xác nhận, ảnh hưởng production.",
    risk_level="high",
    enabled=True,
    handler="systemctl_restart",
    examples=["restart tiktok-bot", "restart tiktok-backend"],
))

register(Skill(
    name="file_summary",
    description="Tóm tắt nội dung file upload qua Telegram bằng LLM.",
    risk_level="low",
    enabled=True,
    handler="_summarize_via_backend",
    examples=["/file <path>", "tóm tắt file vừa upload"],
))

register(Skill(
    name="task_runner",
    description="Chạy task mới trong task queue (search/chat/btc).",
    risk_level="medium",
    enabled=True,
    handler="run_task",
    examples=["/task tìm mới nhất về AI 2026", "/task giá btc"],
))

register(Skill(
    name="agent_blueprint",
    description="Xem tóm tắt kiến trúc agent platform.",
    risk_level="low",
    enabled=True,
    handler="handle_agent_blueprint",
    examples=["/agent_blueprint"],
))

register(Skill(
    name="workers",
    description="Xem danh sách workers đang đăng ký.",
    risk_level="low",
    enabled=True,
    handler="handle_workers",
    examples=["/workers"],
))

register(Skill(
    name="memory_search",
    description="Tìm kiếm trong semantic memory.",
    risk_level="low",
    enabled=True,
    handler="handle_memory_search",
    examples=["/memory_search Python 3.13"],
))

register(Skill(
    name="lessons",
    description="Xem episodic lessons từ các lần chạy skill trước.",
    risk_level="low",
    enabled=True,
    handler="handle_lessons",
    examples=["/lessons", "/lessons search_web"],
))

register(Skill(
    name="audit_recent",
    description="Xem 10 audit entries gần nhất.",
    risk_level="low",
    enabled=True,
    handler="handle_audit_recent",
    examples=["/audit_recent"],
))

# ── Future skills (disabled — placeholders for roadmap) ──────────────────────

register(Skill(
    name="sales_consult",
    description="Tư vấn eSIM Nhật Bản dựa trên product DB. Không bao giờ bịa giá.",
    risk_level="low",
    enabled=True,
    handler="sales_consult_handler",
    examples=[
        "có eSIM Nhật nhận SMS không?",
        "gói nào phát wifi được?",
        "có gói Nhật nào gia hạn được không?",
    ],
))

register(Skill(
    name="product_lookup",
    description="Tra cứu danh mục sản phẩm eSIM trong product DB.",
    risk_level="low",
    enabled=True,
    handler="product_lookup_handler",
    examples=["xem các gói eSIM Nhật", "có gói nào của Docomo không"],
))

register(Skill(
    name="seo_research",
    description="Nghiên cứu từ khóa SEO (Google Autocomplete + DDG, "
                "miễn phí, không cần API key). v0 read-only.",
    risk_level="low",
    enabled=True,
    handler="research_keyword",
    examples=[
        "seo research eSIM Nhật",
        "nghiên cứu từ khóa esim",
        "/seo eSIM Nhật",
    ],
))

register(Skill(
    name="content_factory",
    description="Caption writer (cx/gpt-5.5) + image-brief stub. "
                "Trả draft TikTok post — không tự đăng. v0 read-only.",
    risk_level="medium",
    enabled=True,
    handler="draft_post",
    examples=[
        "viết caption TikTok cho gói eSIM Nhật 7 ngày",
        "draft caption cho sản phẩm X",
        "/caption eSIM Nhật 5GB",
    ],
))

register(Skill(
    name="browser_search",
    description="[FUTURE] Tự động hóa trình duyệt với Playwright.",
    risk_level="medium",
    enabled=False,
    handler="browser_search_handler",
    examples=["mở trang web X và lấy giá"],
))

register(Skill(
    name="ocr_image",
    description="Trích text từ ảnh đã upload qua Telegram (vision LLM, "
                "audit-logged, redaction trước khi reply).",
    risk_level="low",
    enabled=True,
    handler="handle_ocr",
    examples=[
        "/ocr <file_id>",
        "đọc text trong ảnh <file_id>",
        "phân tích ảnh <file_id>",
    ],
))

register(Skill(
    name="ocr_remote",
    description="[FUTURE] OCR remote screenshot (browser worker).",
    risk_level="medium",
    enabled=False,
    handler="ocr_remote_handler",
    examples=["đọc text trong screenshot remote"],
))

register(Skill(
    name="image_generate",
    description="[FUTURE] Tạo ảnh từ prompt.",
    risk_level="medium",
    enabled=False,
    handler="image_gen_handler",
    examples=["tạo ảnh banner cho sản phẩm X"],
))

register(Skill(
    name="git_commit",
    description="[FUTURE] Commit code lên GitHub — cần xác nhận.",
    risk_level="high",
    enabled=False,
    handler="git_commit_handler",
    examples=["commit thay đổi với message '...'"],
))

register(Skill(
    name="deploy",
    description="[FUTURE] Trigger deployment — cần xác nhận.",
    risk_level="high",
    enabled=False,
    handler="deploy_handler",
    examples=["deploy branch dev-agent"],
))


# Apply persisted admin overrides AFTER all built-in skills are registered.
apply_overrides()
