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
    description="[FUTURE] Tạo nội dung bài đăng / caption.",
    risk_level="medium",
    enabled=False,
    handler="content_factory_handler",
    examples=["viết caption TikTok cho sản phẩm X"],
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
