"""
Skill Registry — all capabilities the agent can invoke.

risk_level:
  low    → execute immediately
  medium → execute + log (no prompt)
  high   → create pending_action, require /confirm_action <id>
"""
from dataclasses import dataclass, field


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
