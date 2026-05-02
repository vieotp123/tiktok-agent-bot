"""
Business Store — Product catalog + CRM (leads / conversations / consulting / followups)
for the Japan eSIM sales agent.

Database:  data/business.db   (SQLite — DO NOT commit if it has private runtime data)

Tables:
  products         — sellable eSIM products (verified or needs_update)
  leads            — potential customers (one per sender_key per platform)
  conversations    — every inbound/outbound message linked to a lead
  consulting_logs  — every Q&A pair from a consult call (for analytics)
  followups        — scheduled follow-up reminders for leads

Rules:
  - Never invent prices. status="needs_update" means "do not quote to customer".
  - Only products with status="active" should be presented as confirmed.
  - All identifiers prefer (platform, sender_key) for cross-channel CRM.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path("/opt/tiktok-bot/data/business.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    network         TEXT    NOT NULL DEFAULT '',
    country         TEXT    NOT NULL DEFAULT 'JP',
    duration_days   INTEGER NOT NULL DEFAULT 0,
    data_amount     TEXT    NOT NULL DEFAULT '',
    price_jpy       INTEGER NOT NULL DEFAULT 0,
    price_vnd       INTEGER NOT NULL DEFAULT 0,
    supports_sms    INTEGER NOT NULL DEFAULT 0,
    supports_hotspot INTEGER NOT NULL DEFAULT 1,
    renewable       INTEGER NOT NULL DEFAULT 0,
    notes           TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'needs_update',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id            TEXT    PRIMARY KEY,
    platform      TEXT    NOT NULL DEFAULT '',
    sender_key    TEXT    NOT NULL DEFAULT '',
    username      TEXT    NOT NULL DEFAULT '',
    display_name  TEXT    NOT NULL DEFAULT '',
    profile_url   TEXT    NOT NULL DEFAULT '',
    source        TEXT    NOT NULL DEFAULT '',
    need_summary  TEXT    NOT NULL DEFAULT '',
    lead_score    INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'new',
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE(platform, sender_key)
);

CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    lead_id           TEXT NOT NULL DEFAULT '',
    platform          TEXT NOT NULL DEFAULT '',
    sender_key        TEXT NOT NULL DEFAULT '',
    sender_name       TEXT NOT NULL DEFAULT '',
    direction         TEXT NOT NULL DEFAULT 'in',   -- 'in' | 'out'
    message           TEXT NOT NULL DEFAULT '',
    intent            TEXT NOT NULL DEFAULT '',
    product_suggested TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consulting_logs (
    id              TEXT PRIMARY KEY,
    platform        TEXT NOT NULL DEFAULT '',
    sender_key      TEXT NOT NULL DEFAULT '',
    sender_name     TEXT NOT NULL DEFAULT '',
    user_message    TEXT NOT NULL DEFAULT '',
    bot_reply       TEXT NOT NULL DEFAULT '',
    products_used   TEXT NOT NULL DEFAULT '[]',     -- JSON list of product ids
    confidence      REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS followups (
    id          TEXT PRIMARY KEY,
    lead_id     TEXT NOT NULL DEFAULT '',
    remind_at   TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',     -- pending | done | cancelled
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_sender    ON leads(platform, sender_key);
CREATE INDEX IF NOT EXISTS idx_conv_lead       ON conversations(lead_id);
CREATE INDEX IF NOT EXISTS idx_conv_sender     ON conversations(platform, sender_key);
CREATE INDEX IF NOT EXISTS idx_consult_sender  ON consulting_logs(platform, sender_key);
CREATE INDEX IF NOT EXISTS idx_followups_lead  ON followups(lead_id);
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def init_business_db() -> None:
    """Create tables (idempotent)."""
    with _conn():
        pass


# ── Products ──────────────────────────────────────────────────────────────────

def add_product(
    *,
    id: str | None = None,
    name: str,
    network: str = "",
    country: str = "JP",
    duration_days: int = 0,
    data_amount: str = "",
    price_jpy: int = 0,
    price_vnd: int = 0,
    supports_sms: bool = False,
    supports_hotspot: bool = True,
    renewable: bool = False,
    notes: str = "",
    status: str = "needs_update",
) -> str:
    pid = id or _new_id("prod")
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO products "
            "(id, name, network, country, duration_days, data_amount, price_jpy, price_vnd, "
            " supports_sms, supports_hotspot, renewable, notes, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, name[:120], network[:60], country[:8], int(duration_days),
             data_amount[:40], int(price_jpy), int(price_vnd),
             int(bool(supports_sms)), int(bool(supports_hotspot)), int(bool(renewable)),
             notes[:500], status, now, now),
        )
    return pid


def update_product(product_id: str, **fields) -> bool:
    """Update arbitrary product fields. Returns False if id not found."""
    if not fields:
        return False
    allowed = {
        "name", "network", "country", "duration_days", "data_amount",
        "price_jpy", "price_vnd", "supports_sms", "supports_hotspot",
        "renewable", "notes", "status",
    }
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("supports_sms", "supports_hotspot", "renewable"):
            v = int(bool(v))
        sets.append(f"{k}=?")
        params.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(product_id)
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE products SET {', '.join(sets)} WHERE id=?", params
        )
        return cur.rowcount > 0


def list_products(status: str | None = None, limit: int = 50,
                   exclude_disabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM products"
    params: list = []
    where: list[str] = []
    if status:
        where.append("status=?"); params.append(status)
    elif exclude_disabled:
        where.append("status!='disabled'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += (" ORDER BY (status='active') DESC, "
            "(status='needs_update') DESC, name ASC LIMIT ?")
    params.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def verify_product(product_id: str) -> bool:
    """Mark product as active (verified for customer-facing replies)."""
    return update_product(product_id, status="active")


def disable_product(product_id: str) -> bool:
    """Disable product so it is never suggested."""
    return update_product(product_id, status="disabled")


def get_product(product_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    return dict(r) if r else None


def search_products(query: str, *, only_active: bool = False, limit: int = 10) -> list[dict]:
    """Keyword search over product name, network, notes, data_amount."""
    q = f"%{query.lower()}%"
    sql = (
        "SELECT * FROM products "
        "WHERE (LOWER(name) LIKE ? OR LOWER(network) LIKE ? "
        "       OR LOWER(notes) LIKE ? OR LOWER(data_amount) LIKE ?)"
    )
    params: list = [q, q, q, q]
    if only_active:
        sql += " AND status='active'"
    sql += " ORDER BY status='active' DESC, name ASC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── Leads ─────────────────────────────────────────────────────────────────────

def upsert_lead(
    *,
    platform: str,
    sender_key: str,
    username: str = "",
    display_name: str = "",
    profile_url: str = "",
    source: str = "",
    need_summary: str = "",
    lead_score: int | None = None,
    status: str | None = None,
) -> str:
    """Insert or update a lead by (platform, sender_key). Returns lead id.

    Status / score are merged with monotonic guarantees:
      - status only upgrades (new < needs_followup < interested < converted).
        'lost' / 'converted' are sticky and not auto-overwritten by a lower tier.
      - lead_score only goes UP via auto-update; admin can reset via direct SQL.
    """
    now = _now()
    _STATUS_RANK = {
        "new": 0, "needs_followup": 1, "interested": 2,
        "consulting": 1, "converted": 3, "lost": 3,
    }
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id, lead_score, status FROM leads WHERE platform=? AND sender_key=?",
            (platform, sender_key),
        ).fetchone()
        if existing:
            lid = existing["id"]
            sets = ["updated_at=?"]
            params: list = [now]
            for k, v in [
                ("username", username), ("display_name", display_name),
                ("profile_url", profile_url), ("source", source),
                ("need_summary", need_summary),
            ]:
                if v:
                    sets.append(f"{k}=?")
                    params.append(v[:300])
            # Score: only raise, never lower
            if lead_score is not None:
                new_score = max(int(existing["lead_score"] or 0), int(lead_score))
                sets.append("lead_score=?"); params.append(new_score)
            # Status: only upgrade; never overwrite converted/lost from auto path
            if status is not None:
                cur_rank = _STATUS_RANK.get(existing["status"] or "new", 0)
                new_rank = _STATUS_RANK.get(status, 0)
                if existing["status"] in ("converted", "lost"):
                    # Sticky terminal states — leave alone
                    pass
                elif new_rank > cur_rank:
                    sets.append("status=?"); params.append(status)
            params.append(lid)
            conn.execute(f"UPDATE leads SET {', '.join(sets)} WHERE id=?", params)
            return lid

        lid = _new_id("lead")
        conn.execute(
            "INSERT INTO leads "
            "(id, platform, sender_key, username, display_name, profile_url, source, "
            " need_summary, lead_score, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lid, platform, sender_key, username[:120], display_name[:120],
             profile_url[:300], source[:60], need_summary[:300],
             int(lead_score or 0), status or "new", now, now),
        )
    return lid


def list_leads(*, status: str | None = None, platform: str | None = None,
               limit: int = 30) -> list[dict]:
    sql = "SELECT * FROM leads WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"; params.append(status)
    if platform:
        sql += " AND platform=?"; params.append(platform)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_lead(lead_id: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if r is None:
            # Try sender_key match
            r = conn.execute(
                "SELECT * FROM leads WHERE sender_key=? ORDER BY updated_at DESC LIMIT 1",
                (lead_id,),
            ).fetchone()
    return dict(r) if r else None


def get_lead_by_sender(platform: str, sender_key: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute(
            "SELECT * FROM leads WHERE platform=? AND sender_key=?",
            (platform, sender_key),
        ).fetchone()
    return dict(r) if r else None


# ── Conversations ─────────────────────────────────────────────────────────────

def add_conversation(
    *,
    lead_id: str = "",
    platform: str = "",
    sender_key: str = "",
    sender_name: str = "",
    direction: str = "in",
    message: str = "",
    intent: str = "",
    product_suggested: str = "",
) -> str:
    cid = _new_id("conv")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversations "
            "(id, lead_id, platform, sender_key, sender_name, direction, message, "
            " intent, product_suggested, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, lead_id, platform, sender_key, sender_name[:120],
             direction, message[:1000], intent[:60], product_suggested[:120], _now()),
        )
    return cid


def list_conversations(lead_id: str | None = None, sender_key: str | None = None,
                       limit: int = 30) -> list[dict]:
    sql = "SELECT * FROM conversations WHERE 1=1"
    params: list = []
    if lead_id:
        sql += " AND lead_id=?"; params.append(lead_id)
    if sender_key:
        sql += " AND sender_key=?"; params.append(sender_key)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── Consulting logs ───────────────────────────────────────────────────────────

def add_consulting_log(
    *,
    platform: str = "",
    sender_key: str = "",
    sender_name: str = "",
    user_message: str = "",
    bot_reply: str = "",
    products_used: list[str] | None = None,
    confidence: float = 0.0,
) -> str:
    cid = _new_id("cons")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO consulting_logs "
            "(id, platform, sender_key, sender_name, user_message, bot_reply, "
            " products_used, confidence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, platform, sender_key, sender_name[:120],
             user_message[:1000], bot_reply[:2000],
             json.dumps(products_used or []),
             max(0.0, min(1.0, float(confidence))), _now()),
        )
    return cid


def list_consulting_logs(*, sender_key: str | None = None, limit: int = 20) -> list[dict]:
    sql = "SELECT * FROM consulting_logs"
    params: list = []
    if sender_key:
        sql += " WHERE sender_key=?"; params.append(sender_key)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ── Followups ─────────────────────────────────────────────────────────────────

def add_followup(*, lead_id: str = "", remind_at: str = "", note: str = "",
                 status: str = "pending") -> str:
    fid = _new_id("fup")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO followups (id, lead_id, remind_at, note, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (fid, lead_id, remind_at[:32], note[:500], status, _now()),
        )
    return fid


def list_followups(*, status: str | None = "pending", limit: int = 30) -> list[dict]:
    sql = "SELECT * FROM followups"
    params: list = []
    if status:
        sql += " WHERE status=?"; params.append(status)
    sql += " ORDER BY remind_at ASC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def update_followup_status(followup_id: str, status: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE followups SET status=? WHERE id=?", (status, followup_id)
        )
    return cur.rowcount > 0


# ── Intent detection ──────────────────────────────────────────────────────────

_ESIM_KEYWORDS = (
    "esim", "e-sim", "sim nhật", "sim nhat", "sim japan", "sim hàn", "sim han",
    "data nhật", "data nhat", "data japan",
    "softbank", "docomo", "kddi", "au ", "rakuten",
    "nhận sms", "nhan sms", "sms otp", "sms code",
    "hotspot", "phát wifi", "phat wifi", "tethering", "chia sẻ mạng",
    "gia hạn", "gia han", "renewable", "renew",
    "gói", "goi ", "gb ", "30gb", "50gb", "20gb", "10gb", "100gb",
    "giá esim", "gia esim", "giá sim", "gia sim",
)


def detect_esim_intent(text: str) -> bool:
    """Return True if the message looks like an eSIM/product question."""
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in _ESIM_KEYWORDS)


# ── Consult engine (DB-grounded; never invents prices) ────────────────────────

def _extract_query_filters(query: str) -> dict:
    """Extract structured filters from a freeform query."""
    q = query.lower()
    f: dict = {}
    # Country
    if any(k in q for k in ("nhật", "nhat", "japan", "softbank", "docomo")):
        f["country"] = "JP"
    elif any(k in q for k in ("hàn", "han", "korea", "kr ", "kt ")):
        f["country"] = "KR"
    # Features
    if any(k in q for k in ("sms", "otp", "nhận tin", "nhan tin")):
        f["wants_sms"] = True
    if any(k in q for k in ("hotspot", "phát wifi", "phat wifi", "tethering",
                            "chia sẻ mạng", "chia se mang", "phát mạng")):
        f["wants_hotspot"] = True
    if any(k in q for k in ("gia hạn", "gia han", "renewable", "renew")):
        f["wants_renewable"] = True
    return f


def consult_lookup(query: str) -> tuple[list[dict], list[dict]]:
    """
    Return (active_matches, needs_update_matches).
    Combines:
      - keyword LIKE search on text columns
      - feature-based filter (country / supports_sms / hotspot / renewable)
    """
    filters = _extract_query_filters(query)

    # Pull all non-disabled products, then filter in-memory (small table)
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM products WHERE status != 'disabled'"
        ).fetchall()]

    # Keyword fallback hits
    q_low = query.lower()
    kw_hits = [
        p for p in rows
        if any(part and part in q_low for part in (
            p.get("network", "").lower(),
            p.get("country", "").lower(),
            p.get("name", "").lower(),
        ))
    ]

    # Feature-filtered hits
    def matches(p: dict) -> bool:
        if filters.get("country") and p.get("country") != filters["country"]:
            return False
        if filters.get("wants_sms") and not p.get("supports_sms"):
            return False
        if filters.get("wants_hotspot") and not p.get("supports_hotspot"):
            return False
        if filters.get("wants_renewable") and not p.get("renewable"):
            return False
        return True

    if filters:
        feature_hits = [p for p in rows if matches(p)]
    else:
        feature_hits = []

    # Merge (dedup by id)
    seen: set[str] = set()
    combined: list[dict] = []
    for p in kw_hits + feature_hits:
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        combined.append(p)

    active  = [p for p in combined if p["status"] == "active"]
    pending = [p for p in combined if p["status"] != "active"]
    return active, pending


def format_product_short(p: dict) -> str:
    bits = [f"<b>{p['name']}</b>"]
    if p.get("network"):
        bits.append(p["network"])
    if p.get("data_amount"):
        bits.append(p["data_amount"])
    if p.get("duration_days"):
        bits.append(f"{p['duration_days']}d")
    feats = []
    if p.get("supports_sms"):     feats.append("SMS")
    if p.get("supports_hotspot"): feats.append("Hotspot")
    if p.get("renewable"):        feats.append("Renew")
    if feats:
        bits.append("/".join(feats))
    if p["status"] == "active":
        if p.get("price_vnd"):
            bits.append(f"{int(p['price_vnd']):,}đ")
        elif p.get("price_jpy"):
            bits.append(f"¥{int(p['price_jpy']):,}")
    else:
        bits.append("⚠️ chưa verify")
    return " · ".join(bits)


def _format_product_for_customer(p: dict) -> str:
    """Customer-facing one-line product summary (no admin terms, no warnings)."""
    bits = [p["name"]]
    if p.get("data_amount"):
        bits.append(p["data_amount"])
    if p.get("duration_days"):
        bits.append(f"{p['duration_days']} ngày")
    feats: list[str] = []
    if p.get("supports_sms"):     feats.append("nhận SMS")
    if p.get("supports_hotspot"): feats.append("phát WiFi")
    if p.get("renewable"):        feats.append("gia hạn được")
    if feats:
        bits.append(", ".join(feats))
    if p.get("price_vnd"):
        bits.append(f"{int(p['price_vnd']):,}đ")
    elif p.get("price_jpy"):
        bits.append(f"¥{int(p['price_jpy']):,}")
    return " · ".join(bits)


def compute_lead_score(query: str) -> int:
    """Naive lead-scoring based on keyword signals in the query."""
    q = query.lower()
    score = 0
    if any(k in q for k in ("giá", "gia ", "bao nhiêu", "price", "cost")):
        score += 30
    if any(k in q for k in ("sms", "otp", "nhận tin")):
        score += 30
    if any(k in q for k in ("hotspot", "phát wifi", "phat wifi", "tethering")):
        score += 20
    if any(k in q for k in ("gia hạn", "gia han", "renew")):
        score += 20
    if any(k in q for k in ("ngày", "ngay", "duration", "gb", "data")):
        score += 10
    return min(100, score)


def build_consult_reply(query: str, *, audience: str = "customer"
                        ) -> tuple[str, list[str], float]:
    """
    Build a grounded consult reply.

    audience:
      "customer" — TikTok customer-facing tone (polite, no mày/tao,
                   never quotes needs_update as confirmed).
      "admin"    — Telegram-admin tone (direct, shows warnings + ids).

    Returns (reply_text, product_ids_used, confidence).
    NEVER invents prices. If no active product matches, says so clearly.
    """
    active, pending = consult_lookup(query)
    q = query.lower()

    if audience == "admin":
        # Admin tone: short, technical, with warnings.
        if active:
            lines = ["Active matches:"]
            for p in active[:4]:
                lines.append("• " + format_product_short(p).replace("<b>", "").replace("</b>", ""))
            if pending:
                lines.append(f"\n⚠ {len(pending)} needs_update candidates ignored: "
                             + ", ".join(p["name"] for p in pending[:3]))
            reply = "\n".join(lines)
            return reply, [p["id"] for p in active[:4]], 0.85

        if pending:
            names = ", ".join(p["name"] for p in pending[:3])
            reply = (
                f"⚠ Chỉ có needs_update candidates: {names}\n"
                "Verify giá/feature trước khi quote khách. "
                "Dùng /product_verify <id> sau khi xác nhận."
            )
            return reply, [p["id"] for p in pending[:3]], 0.25

        reply = ("Chưa có gói đã verify trong database khớp. "
                 "Dùng /product_add hoặc /product_verify để cập nhật catalog.")
        return reply, [], 0.0

    # ── Customer-facing (TikTok DM, polite, no mày/tao, never quotes
    #    needs_update as confirmed) ─────────────────────────────────────────
    if active:
        lines = ["Bên mình có mấy gói phù hợp nè:"]
        for p in active[:3]:
            lines.append("• " + _format_product_for_customer(p))
        sms_hits     = [p for p in active if p.get("supports_sms")]
        hotspot_hits = [p for p in active if p.get("supports_hotspot")]
        renew_hits   = [p for p in active if p.get("renewable")]
        if any(k in q for k in ("sms", "otp", "nhận tin")):
            if sms_hits:
                lines.append(f"Nhận SMS được nha: {sms_hits[0]['name']}")
            else:
                lines.append("Mấy gói trên chưa hỗ trợ SMS — bạn cần SMS thì để mình check kỹ rồi rep lại.")
        if any(k in q for k in ("hotspot", "phát wifi", "phat wifi", "tethering")):
            if hotspot_hits:
                lines.append("Phát WiFi được hết bạn nhé.")
        if any(k in q for k in ("gia hạn", "gia han", "renew")) and renew_hits:
            lines.append(f"Gia hạn được: {renew_hits[0]['name']}")
        return "\n".join(lines), [p["id"] for p in active[:3]], 0.85

    # Customer side: have unverified candidates but DO NOT quote them.
    if pending:
        reply = (
            "Bên mình có gói tương tự đang được cập nhật lại thông số/giá. "
            "Bạn để mình kiểm tra với admin rồi báo lại nha — "
            "mình không muốn báo sai số liệu."
        )
        return reply, [p["id"] for p in pending[:3]], 0.2

    reply = (
        "Hiện gói khớp với yêu cầu của bạn chưa có trong danh mục đã xác nhận. "
        "Mình note lại để admin bổ sung và sẽ rep bạn ngay khi có info chính xác nha."
    )
    return reply, [], 0.0


# ── Formatting helpers (Telegram) ─────────────────────────────────────────────

def format_products_list(status: str | None = None) -> str:
    items = list_products(status=status, limit=30)
    if not items:
        if status == "active":
            return "Chưa có gói đã verify trong database."
        if status == "needs_update":
            return "Không có sản phẩm nào ở trạng thái needs_update."
        if status == "disabled":
            return "Không có sản phẩm nào bị disable."
        return "Chưa có sản phẩm nào trong database."
    lines = ["<b>Products</b>" + (f" — status={status}" if status else "")]
    icons = {"active": "✅", "needs_update": "⚠️", "disabled": "🚫"}
    for p in items:
        icon = icons.get(p["status"], "•")
        lines.append(f"{icon} <code>{p['id']}</code> — {format_product_short(p)}")
    active_count = sum(1 for p in items if p["status"] == "active")
    needs_update = sum(1 for p in items if p["status"] == "needs_update")
    disabled     = sum(1 for p in items if p["status"] == "disabled")
    lines.append(
        f"\n<i>active={active_count} · needs_update={needs_update} · disabled={disabled}</i>"
    )
    return "\n".join(lines)


def format_product_detail(product_id: str) -> str:
    p = get_product(product_id)
    if not p:
        return f"Product <code>{product_id}</code> không tồn tại."
    icons = {"active": "✅", "needs_update": "⚠️", "disabled": "🚫"}
    flags: list[str] = []
    if p.get("supports_sms"):     flags.append("SMS")
    if p.get("supports_hotspot"): flags.append("Hotspot")
    if p.get("renewable"):        flags.append("Renewable")
    lines = [
        f"<b>{p['name']}</b> {icons.get(p['status'], '')}",
        f"id: <code>{p['id']}</code>",
        f"status: <b>{p['status']}</b>",
        f"network: {p.get('network', '?')} | country: {p.get('country', '?')}",
        f"duration: {p.get('duration_days', 0)}d | data: {p.get('data_amount', '?')}",
        f"price: ¥{int(p.get('price_jpy', 0)):,} / {int(p.get('price_vnd', 0)):,}đ",
        f"features: {', '.join(flags) or '(none)'}",
        f"notes: {p.get('notes', '') or '(none)'}",
        f"updated: {p.get('updated_at', '')[:16]}",
    ]
    return "\n".join(lines)


def format_leads_list() -> str:
    items = list_leads(limit=20)
    if not items:
        return "No leads yet."
    lines = ["<b>Leads</b>"]
    for l in items:
        lines.append(
            f"• <code>{l['id']}</code> [{l['platform']}] "
            f"<b>{(l.get('display_name') or l.get('username') or l.get('sender_key'))[:30]}</b> "
            f"score={l.get('lead_score', 0)} status={l.get('status', '?')}"
        )
    return "\n".join(lines)


def format_lead_detail(lead_id: str) -> str:
    l = get_lead(lead_id)
    if not l:
        return f"Lead <code>{lead_id}</code> not found."
    convs = list_conversations(lead_id=l["id"], limit=5)
    lines = [
        f"<b>Lead {l['id']}</b>",
        f"Platform: {l['platform']} | sender_key: <code>{l['sender_key']}</code>",
        f"Name: {l.get('display_name') or l.get('username') or '(unknown)'}",
        f"Score: {l.get('lead_score', 0)} | Status: {l.get('status', '?')}",
        f"Need: {l.get('need_summary', '') or '(none)'}",
        f"Created: {l['created_at'][:16]}",
    ]
    if convs:
        lines.append("\n<b>Recent messages:</b>")
        for c in convs:
            arrow = "→" if c["direction"] == "in" else "←"
            lines.append(f"  {arrow} {c['message'][:80]}")
    return "\n".join(lines)


def format_followups_list() -> str:
    items = list_followups()
    if not items:
        return "No pending followups."
    lines = ["<b>Followups</b>"]
    for f in items:
        lines.append(
            f"• <code>{f['id']}</code> lead={f['lead_id'][:14]} "
            f"@ {f['remind_at']} — {f['note'][:60]}"
        )
    return "\n".join(lines)


# ── Seed ──────────────────────────────────────────────────────────────────────

def seed_products_if_empty() -> int:
    """Seed sample products if empty. ALL marked needs_update — do not quote."""
    init_business_db()
    with _conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count > 0:
        return 0

    seeds = [
        dict(id="prod_jp_softbank_30d_50gb",
             name="Japan Softbank 30d 50GB",
             network="Softbank", country="JP",
             duration_days=30, data_amount="50GB",
             supports_sms=False, supports_hotspot=True, renewable=True,
             notes="Sample seed — needs price/spec verification before customer use",
             status="needs_update"),
        dict(id="prod_jp_softbank_15d_30gb",
             name="Japan Softbank 15d 30GB",
             network="Softbank", country="JP",
             duration_days=15, data_amount="30GB",
             supports_sms=False, supports_hotspot=True, renewable=False,
             notes="Sample seed — verify before quoting",
             status="needs_update"),
        dict(id="prod_jp_docomo_30d_unlim",
             name="Japan Docomo 30d Unlimited",
             network="Docomo", country="JP",
             duration_days=30, data_amount="Unlimited (FUP)",
             supports_sms=False, supports_hotspot=True, renewable=True,
             notes="Sample seed — FUP details unverified",
             status="needs_update"),
        dict(id="prod_jp_docomo_sms_30d_20gb",
             name="Japan Docomo + SMS 30d 20GB",
             network="Docomo", country="JP",
             duration_days=30, data_amount="20GB",
             supports_sms=True, supports_hotspot=True, renewable=False,
             notes="Sample seed — SMS support claimed but UNVERIFIED",
             status="needs_update"),
        dict(id="prod_kr_kt_15d_unlim",
             name="Korea KT 15d Unlimited",
             network="KT", country="KR",
             duration_days=15, data_amount="Unlimited",
             supports_sms=False, supports_hotspot=True, renewable=False,
             notes="Sample seed — Korea product, verify price/spec",
             status="needs_update"),
    ]
    for s in seeds:
        add_product(**s)
    return len(seeds)
