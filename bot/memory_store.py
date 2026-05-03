"""
Memory Store — OpenClaw-style multi-tier persistent memory for the agent platform.

Database:  data/agent_memory.db   (SQLite)

Tables:
  raw_events    — append-only event log (never injected into prompts directly)
  memories      — semantic/episodic/procedural facts, namespace-scoped
  lessons       — episodic outcomes per skill (what worked / what failed)
  task_state    — durable per-task scratch-pad / state machine
  memory_usage  — audit trail of context injections

Design rules:
  - Never dump raw_events into LLM prompts.
  - build_memory_context() retrieves top-K relevant memories (keyword search).
  - Lessons injected as few-shot examples only when the same skill re-runs.
  - Hard limits: max 8 items, max 6000 chars per context injection.
  - payload_json from raw_events is NEVER included in prompt context.
  - Namespaces isolate memory across users / contexts.

v2 additions:
  - memories.content_hash    — dedup key (sha256 of normalized title+content).
  - decay_unused_memories()  — importance auto-decay for stale entries.
  - lessons_for_retry()      — per-skill failure recall for retry injection.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DB_PATH  = Path("/opt/tiktok-bot/data/agent_memory.db")
RAW_FILE = Path("/opt/tiktok-bot/data/audit/raw_events.jsonl")   # legacy JSONL (kept for compat)

# ── Schema ─────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    source       TEXT    NOT NULL DEFAULT '',
    actor        TEXT    NOT NULL DEFAULT '',
    event_type   TEXT    NOT NULL DEFAULT '',
    summary      TEXT    NOT NULL DEFAULT '',
    payload_json TEXT    NOT NULL DEFAULT '{}',
    tags         TEXT    NOT NULL DEFAULT '[]',
    task_id      TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace    TEXT    NOT NULL DEFAULT 'global',
    memory_type  TEXT    NOT NULL DEFAULT 'semantic',
    title        TEXT    NOT NULL DEFAULT '',
    summary      TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL DEFAULT '',
    tags         TEXT    NOT NULL DEFAULT '[]',
    importance   INTEGER NOT NULL DEFAULT 5,
    confidence   REAL    NOT NULL DEFAULT 1.0,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    last_used_at TEXT    NOT NULL DEFAULT '',
    content_hash TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT    NOT NULL DEFAULT 'global',
    skill       TEXT    NOT NULL DEFAULT '',
    title       TEXT    NOT NULL DEFAULT '',
    root_cause  TEXT    NOT NULL DEFAULT '',
    fix         TEXT    NOT NULL DEFAULT '',
    tests       TEXT    NOT NULL DEFAULT '',
    lesson      TEXT    NOT NULL DEFAULT '',
    outcome     TEXT    NOT NULL DEFAULT 'success',
    tags        TEXT    NOT NULL DEFAULT '[]',
    importance  INTEGER NOT NULL DEFAULT 5,
    task_id     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS task_state (
    task_id      TEXT    PRIMARY KEY,
    status       TEXT    NOT NULL DEFAULT 'running',
    goal         TEXT    NOT NULL DEFAULT '',
    current_step TEXT    NOT NULL DEFAULT '',
    state_json   TEXT    NOT NULL DEFAULT '{}',
    updated_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT    NOT NULL,
    task_id       TEXT    NOT NULL DEFAULT '',
    query         TEXT    NOT NULL DEFAULT '',
    memory_ids_json TEXT  NOT NULL DEFAULT '[]',
    token_estimate INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace);
CREATE INDEX IF NOT EXISTS idx_memories_type      ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_lessons_skill      ON lessons(skill);
CREATE INDEX IF NOT EXISTS idx_raw_events_type    ON raw_events(event_type);
CREATE INDEX IF NOT EXISTS idx_raw_events_ts      ON raw_events(timestamp);
"""


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_v2(conn)
    conn.commit()
    return conn


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Idempotent: add content_hash column + index to pre-v2 memories tables."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(memories)")}
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
        # Backfill hashes for existing rows so dedup works retroactively.
        rows = conn.execute("SELECT id, title, content FROM memories").fetchall()
        for r in rows:
            h = _content_hash(r["title"] or "", r["content"] or "")
            conn.execute("UPDATE memories SET content_hash=? WHERE id=?", (h, r["id"]))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_memories_hash "
        "ON memories(namespace, content_hash)"
    )


def _content_hash(title: str, content: str) -> str:
    """Stable hash for dedup. Normalises whitespace + case."""
    norm = (title.strip().lower() + "\x1f" + content.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── Raw events (SQLite) ────────────────────────────────────────────────────────

def add_raw_event(
    source: str,
    action: str,
    summary: str,
    metadata: dict | None = None,
    *,
    actor: str = "",
    event_type: str = "",
    tags: list[str] | None = None,
    task_id: str = "",
) -> int:
    """
    Append one raw event to the SQLite raw_events table.
    Also writes to legacy JSONL for backward compat.
    NEVER include secrets in metadata/payload_json.
    """
    ts      = _now()
    etype   = event_type or action
    payload = json.dumps(metadata or {}, ensure_ascii=False)

    # Write to SQLite
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO raw_events "
            "(timestamp, source, actor, event_type, summary, payload_json, tags, task_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, source, actor, etype, summary[:300], payload, json.dumps(tags or []), task_id),
        )
        row_id = cur.lastrowid

    # Also write to legacy JSONL
    try:
        RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": ts, "source": source, "action": action,
                 "summary": summary[:300], "metadata": metadata or {}}
        with open(RAW_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return row_id


def tail_raw_events(n: int = 20) -> list[dict]:
    """Return last N raw events from SQLite (newest last)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM raw_events ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return list(reversed([dict(r) for r in rows]))


# ── Semantic / episodic / procedural memories ──────────────────────────────────

def add_memory(
    title: str,
    content: str,
    *,
    namespace: str = "global",
    memory_type: str = "semantic",
    summary: str = "",
    tags: list[str] | None = None,
    importance: int = 5,
    confidence: float = 1.0,
    dedup: bool = True,
    # legacy compat: accept username as alias for namespace
    username: str = "",
) -> int:
    """
    Add a memory entry.  Returns row id.

    memory_type: "semantic" | "episodic" | "procedural"
    importance:  1 (trivial) – 10 (critical)
    confidence:  0.0 – 1.0
    dedup:       if True (default), an existing row in the same namespace
                 with the same (title+content) hash is reused — its
                 importance is bumped (+1, capped at 10) and updated_at
                 refreshed instead of inserting a new row.
    """
    if username and namespace == "global":
        namespace = username          # legacy caller compatibility
    now  = _now()
    imp  = max(1, min(10, importance))
    conf = max(0.0, min(1.0, confidence))
    chash = _content_hash(title, content)
    with _conn() as conn:
        if dedup:
            existing = conn.execute(
                "SELECT id, importance FROM memories "
                "WHERE namespace=? AND content_hash=? LIMIT 1",
                (namespace, chash),
            ).fetchone()
            if existing:
                new_imp = min(10, max(existing["importance"], imp) + 1)
                conn.execute(
                    "UPDATE memories SET importance=?, updated_at=? WHERE id=?",
                    (new_imp, now, existing["id"]),
                )
                return existing["id"]
        cur = conn.execute(
            "INSERT INTO memories "
            "(namespace, memory_type, title, summary, content, tags, importance, confidence, "
            " created_at, updated_at, last_used_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (namespace, memory_type, title[:200], summary[:400], content[:2000],
             json.dumps(tags or []), imp, conf, now, now, "", chash),
        )
        return cur.lastrowid


def search_memory(
    query: str,
    *,
    namespace: str = "global",
    memory_type: str | None = None,
    limit: int = 8,
    min_importance: int = 1,
    # legacy compat
    username: str = "",
) -> list[dict]:
    """
    Keyword search over memories for a namespace.
    Searches title, summary, content, tags (LIKE, case-insensitive).
    Returns rows sorted by importance DESC, created_at DESC.
    """
    if username and namespace == "global":
        namespace = username
    q = f"%{query.lower()}%"
    sql = (
        "SELECT * FROM memories "
        "WHERE namespace=? AND importance>=? "
        "AND (LOWER(title) LIKE ? OR LOWER(summary) LIKE ? "
        "     OR LOWER(content) LIKE ? OR LOWER(tags) LIKE ?)"
    )
    params: list = [namespace, min_importance, q, q, q, q]
    if memory_type:
        sql += " AND memory_type=?"
        params.append(memory_type)
    sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# legacy alias
def search_memory_simple(username: str, query: str, limit: int = 5,
                          memory_type: str | None = None) -> list[dict]:
    return search_memory(query, namespace=username, memory_type=memory_type, limit=limit)


def list_memories(
    namespace: str = "global",
    *,
    memory_type: str | None = None,
    limit: int = 20,
    min_importance: int = 1,
) -> list[dict]:
    """Return recent memories for a namespace, optionally filtered by type."""
    sql = "SELECT * FROM memories WHERE namespace=? AND importance>=?"
    params: list = [namespace, min_importance]
    if memory_type:
        sql += " AND memory_type=?"
        params.append(memory_type)
    sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete_memory(memory_id: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return True


def update_memory_tags(
    memory_id: int,
    tags: list[str],
    *,
    mode: str = "add",
) -> dict | None:
    """Update tags for a memory. mode = add | set | remove.

    Returns {"id", "tags"} on success, None if the memory doesn't exist.
    Tags are normalised: stripped, lowercased, deduped, empties dropped.
    """
    norm = []
    seen: set[str] = set()
    for t in tags or []:
        t = (t or "").strip().lower()
        if not t or t in seen:
            continue
        seen.add(t)
        norm.append(t)

    with _conn() as conn:
        row = conn.execute(
            "SELECT id, tags FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        if not row:
            return None
        try:
            current = json.loads(row["tags"] or "[]")
            if not isinstance(current, list):
                current = []
        except Exception:
            current = []

        if mode == "set":
            merged = norm
        elif mode == "remove":
            drop = set(norm)
            merged = [t for t in current if t not in drop]
        else:  # add
            merged = list(current)
            existing = set(current)
            for t in norm:
                if t not in existing:
                    merged.append(t)
                    existing.add(t)

        conn.execute(
            "UPDATE memories SET tags=?, updated_at=? WHERE id=?",
            (json.dumps(merged), _now(), memory_id),
        )
    return {"id": memory_id, "tags": merged}


def compact_memories(namespace: str = "global", keep_top: int = 50) -> int:
    """
    Delete low-importance old memories beyond keep_top.
    Returns number of rows deleted.
    """
    with _conn() as conn:
        # Get ids to keep
        rows = conn.execute(
            "SELECT id FROM memories WHERE namespace=? "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            (namespace, keep_top),
        ).fetchall()
        keep_ids = [r["id"] for r in rows]
        if not keep_ids:
            return 0
        placeholders = ",".join("?" * len(keep_ids))
        cur = conn.execute(
            f"DELETE FROM memories WHERE namespace=? AND id NOT IN ({placeholders})",
            [namespace] + keep_ids,
        )
        return cur.rowcount


def decay_unused_memories(
    namespace: str = "global",
    *,
    half_life_days: int = 30,
    floor: int = 1,
    now: Optional[datetime] = None,
) -> int:
    """
    Decay importance of memories not recently used.

    Drops importance by 1 per `half_life_days` elapsed since last_used_at
    (or created_at if never used). Importance never drops below `floor`.
    Returns the number of rows whose importance changed.
    """
    if half_life_days < 1:
        return 0
    cutoff_now = now or datetime.now(timezone.utc)
    changed = 0
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, importance, last_used_at, created_at "
            "FROM memories WHERE namespace=? AND importance>?",
            (namespace, floor),
        ).fetchall()
        for r in rows:
            ref = _parse_ts(r["last_used_at"]) or _parse_ts(r["created_at"])
            if ref is None:
                continue
            elapsed = (cutoff_now - ref).total_seconds() / 86400.0
            steps = int(elapsed // half_life_days)
            if steps <= 0:
                continue
            new_imp = max(floor, r["importance"] - steps)
            if new_imp != r["importance"]:
                conn.execute(
                    "UPDATE memories SET importance=? WHERE id=?",
                    (new_imp, r["id"]),
                )
                changed += 1
    return changed


# ── Episodic lessons ───────────────────────────────────────────────────────────

def add_lesson(
    skill: str,
    outcome: str,
    lesson_text: str,
    task_id: str = "",
    *,
    namespace: str = "global",
    title: str = "",
    root_cause: str = "",
    fix: str = "",
    tests: str = "",
    tags: list[str] | None = None,
    importance: int = 5,
) -> int:
    """
    Record what happened when a skill ran.
    outcome: "success" | "failure" | "partial"
    """
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO lessons "
            "(namespace, skill, title, root_cause, fix, tests, lesson, outcome, "
            " tags, importance, task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (namespace, skill, title[:200], root_cause[:500], fix[:500], tests[:300],
             lesson_text[:500], outcome,
             json.dumps(tags or []), max(1, min(10, importance)), task_id, now),
        )
        return cur.lastrowid


def lessons_for_retry(
    skill: str,
    *,
    namespace: str = "global",
    limit: int = 3,
) -> list[dict]:
    """
    Recent failure/partial lessons for a skill — for retry-time injection.

    Successful lessons are excluded: when re-running the same skill the
    agent only needs to be reminded of what previously went wrong.
    """
    if not skill:
        return []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lessons "
            "WHERE skill=? AND namespace=? AND outcome IN ('failure','partial') "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            (skill, namespace, max(1, limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def list_lessons(
    skill: str | None = None,
    *,
    namespace: str | None = None,
    outcome: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return recent lessons, optionally filtered by skill, namespace, or outcome."""
    sql = "SELECT * FROM lessons WHERE 1=1"
    params: list = []
    if namespace:
        sql += " AND namespace=?"
        params.append(namespace)
    if skill:
        sql += " AND skill=?"
        params.append(skill)
    if outcome:
        sql += " AND outcome=?"
        params.append(outcome)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Task state ─────────────────────────────────────────────────────────────────

def update_task_state(
    task_id: str,
    *,
    status: str = "running",
    goal: str = "",
    current_step: str = "",
    state: dict | None = None,
) -> None:
    """Upsert task state record."""
    now = _now()
    state_json = json.dumps(state or {}, ensure_ascii=False)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO task_state (task_id, status, goal, current_step, state_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "  status=excluded.status, "
            "  goal=CASE WHEN excluded.goal!='' THEN excluded.goal ELSE goal END, "
            "  current_step=excluded.current_step, "
            "  state_json=excluded.state_json, "
            "  updated_at=excluded.updated_at",
            (task_id, status, goal[:500], current_step[:200], state_json, now),
        )


def get_task_state(task_id: str) -> dict | None:
    """Return task state dict or None if not found."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM task_state WHERE task_id=?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["state"] = json.loads(d.get("state_json", "{}"))
    except Exception:
        d["state"] = {}
    return d


# ── Context builder (prompt-safe) ──────────────────────────────────────────────

_CONTEXT_MAX_ITEMS = 8
_CONTEXT_MAX_CHARS = 6000


def build_memory_context(
    query: str,
    namespace: str = "global",
    *,
    max_items: int = _CONTEXT_MAX_ITEMS,
    max_chars: int = _CONTEXT_MAX_CHARS,
    include_lessons: bool = True,
    task_id: str = "",
    skill_hint: str | None = None,
    is_retry: bool = False,
) -> str:
    """
    Build a compact memory context string for LLM injection.

    Hard limits:
      - max_items: cap at 8 memories + 3 lessons (5 on retry)
      - max_chars: cap at 6000 total characters
      - NEVER include payload_json from raw_events
      - NEVER include full content if summary is available

    skill_hint / is_retry:
      - If `skill_hint` is given, lesson injection uses that skill directly
        (callers like the runner know the exact skill).
      - If `is_retry=True`, prior failure/partial lessons for the skill are
        prioritised over generic recent lessons, and the cap rises to 5.

    Returns formatted string (empty string if nothing relevant found).
    """
    mem_limit    = min(max_items, _CONTEXT_MAX_ITEMS)
    lesson_limit = min(5 if is_retry else 3, max_items)

    # 1. Search memories
    memories = search_memory(query, namespace=namespace, limit=mem_limit)

    # 2. Search global memories (project-level)
    if namespace != "global":
        global_mems = search_memory(query, namespace="global", limit=3)
        # Merge, dedup by id
        seen = {m["id"] for m in memories}
        for m in global_mems:
            if m["id"] not in seen:
                memories.append(m)
                seen.add(m["id"])
    memories = memories[:mem_limit]

    # 3. Get relevant lessons
    lessons = []
    if include_lessons:
        # Prefer caller-supplied skill; otherwise infer from query keywords.
        skill = skill_hint
        if not skill:
            for keyword, skill_name in [
                ("btc", "btc_price"), ("bitcoin", "btc_price"),
                ("search", "search_web"), ("tìm", "search_web"),
                ("chat", "chat"), ("tin nhắn", "send_tiktok_dm"),
            ]:
                if keyword in query.lower():
                    skill = skill_name
                    break
        if is_retry and skill:
            # On retry: failures + partials only — successes don't help.
            lessons = lessons_for_retry(skill, namespace=namespace, limit=lesson_limit)
        else:
            lessons = list_lessons(skill=skill, limit=lesson_limit)

    if not memories and not lessons:
        return ""

    parts: list[str] = ["### Memory Context"]
    char_count = len(parts[0])

    # Add memories
    if memories:
        parts.append("\n**Relevant facts:**")
        char_count += 20
        memory_ids: list[int] = []
        for m in memories:
            text = m.get("summary") or m.get("content", "")[:300]
            title = m.get("title", "")
            tag_list = json.loads(m.get("tags", "[]"))
            tag_str = " ".join(f"#{t}" for t in tag_list[:3]) if tag_list else ""
            line = f"• [{m['memory_type']}] "
            if title:
                line += f"**{title}**: "
            line += text[:200]
            if tag_str:
                line += f"  {tag_str}"

            if char_count + len(line) > max_chars:
                break
            parts.append(line)
            char_count += len(line)
            memory_ids.append(m["id"])

        # Log memory usage
        if task_id:
            _log_memory_usage(task_id, query, memory_ids, char_count)

        # Update last_used_at
        if memory_ids:
            _touch_memories(memory_ids)

    # Add lessons
    if lessons and char_count < max_chars - 200:
        parts.append("\n**Past lessons:**")
        char_count += 18
        for lsn in lessons:
            icon = {"success": "✅", "failure": "❌", "partial": "⚠️"}.get(lsn["outcome"], "•")
            text = lsn.get("lesson", "")[:150]
            line = f"{icon} [{lsn['skill']}] {text}"
            if char_count + len(line) > max_chars:
                break
            parts.append(line)
            char_count += len(line)

    return "\n".join(parts)


def get_context_for_task(
    goal: str,
    source: str = "internal",
    sender_key: str = "global",
) -> str:
    """
    Convenience wrapper: build memory context for a running task.
    Infers namespace from source + sender_key.
    """
    namespace = sender_key if sender_key else "global"
    if source in ("telegram", "tg_admin"):
        namespace = "tg_admin"
    elif source == "tiktok":
        namespace = f"tiktok:{sender_key}" if sender_key else "tiktok"
    return build_memory_context(goal, namespace=namespace)


def _log_memory_usage(task_id: str, query: str, memory_ids: list[int], token_est: int) -> None:
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO memory_usage (timestamp, task_id, query, memory_ids_json, token_estimate) "
                "VALUES (?, ?, ?, ?, ?)",
                (_now(), task_id, query[:200], json.dumps(memory_ids), token_est // 4),
            )
    except Exception:
        pass


def _touch_memories(memory_ids: list[int]) -> None:
    try:
        now = _now()
        with _conn() as conn:
            placeholders = ",".join("?" * len(memory_ids))
            conn.execute(
                f"UPDATE memories SET last_used_at=? WHERE id IN ({placeholders})",
                [now] + memory_ids,
            )
    except Exception:
        pass


# ── Formatting helpers ─────────────────────────────────────────────────────────

def format_search_results(results: list[dict], query: str) -> str:
    """Human-readable memory search results for Telegram /memory_search."""
    if not results:
        return f"No memories found for <b>{query}</b>."
    lines = [f"<b>Memory search:</b> {query} ({len(results)} result(s))"]
    for r in results:
        tags = json.loads(r.get("tags", "[]"))
        tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
        title = r.get("title", "")
        text  = r.get("summary") or r.get("content", r.get("text", ""))
        imp   = r.get("importance", 5)
        imp_bar = "⭐" * min(imp, 3)
        lines.append(
            f"• [{r.get('memory_type', r.get('memory_type', 'semantic'))}] {imp_bar}"
            + (f" <b>{title}</b>" if title else "")
            + f"\n  {str(text)[:150]}"
            + (f"\n  {tag_str}" if tag_str else "")
            + f"\n  <i>{r.get('created_at', r.get('created_at', ''))[:16]}</i>"
        )
    return "\n\n".join(lines)


def format_lessons_list(skill: str | None = None, limit: int = 10) -> str:
    """Human-readable lessons for Telegram /lessons command."""
    lessons = list_lessons(skill=skill, limit=limit)
    if not lessons:
        target = f" for skill '{skill}'" if skill else ""
        return f"No lessons recorded{target} yet."
    icon_map = {"success": "✅", "failure": "❌", "partial": "⚠️"}
    lines = [f"<b>Lessons</b>{' — ' + skill if skill else ''} (last {len(lessons)})"]
    for lsn in lessons:
        icon = icon_map.get(lsn.get("outcome", ""), "•")
        title = lsn.get("title", "")
        text  = lsn.get("lesson", lsn.get("lesson_text", ""))[:120]
        lines.append(
            f"{icon} <b>{lsn['skill']}</b>"
            + (f" — {title}" if title else "")
            + f" ({lsn.get('outcome', '?')})\n"
            f"   {text}\n"
            f"   <i>{lsn.get('created_at', '')[:16]}</i>"
        )
    return "\n\n".join(lines)


# ── Seed helper ────────────────────────────────────────────────────────────────

def seed_if_empty(namespace: str = "global") -> int:
    """
    Seed 5 project memories if the namespace is empty.
    Returns number of memories inserted (0 if already seeded).
    """
    with _conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE namespace=?", (namespace,)
        ).fetchone()[0]
    if count > 0:
        return 0

    seeds = [
        dict(
            title="Business Context — Japan eSIM",
            content=(
                "Owner runs muaesim.vn — Vietnam-based store selling Japan/Asia eSIM data cards. "
                "Primary sales channel: TikTok Live + DMs. Target customers: Vietnamese travelers to Japan. "
                "Key products: data-only eSIMs for Japan, South Korea, Thailand. "
                "Brand: Chatgibiti (TikTok account). Support language: Vietnamese."
            ),
            summary="Cửa hàng muaesim.vn bán eSIM Nhật Bản/châu Á qua TikTok (Chatgibiti).",
            tags=["business", "esim", "japan", "tiktok", "muaesim"],
            importance=9,
            memory_type="procedural",
        ),
        dict(
            title="TikTok Worker Fragility",
            content=(
                "TikTok DOM is highly dynamic — CSS class names change with app version. "
                "Playwright selectors break silently: JS extractor may return [] without errors. "
                "Always verify [read] and [send] log lines appear within 10s of polling start. "
                "If baseline never sets, bot processes nothing. Scroll-to-bottom before first poll. "
                "Never trust silence — add js_items=0 log when extractor returns empty."
            ),
            summary="TikTok DOM bất ổn — selector break silent, cần verify log [read] sau start.",
            tags=["tiktok", "playwright", "fragility", "dom", "debug"],
            importance=8,
            memory_type="procedural",
        ),
        dict(
            title="Duplicate Reply Risk — Old AWS Bot",
            content=(
                "Previous deployment on AWS had a separate bot process that may still be sending replies. "
                "Symptom: TikTok user receives two replies — one from current bot, one from old bot. "
                "Fix: ensure only one bot process is active. Check for stale AWS sessions. "
                "Old fallback phrases: 'đợi tí m', 'não t đang lag', 'gửi lại phát nữa t xử'. "
                "These must NEVER appear in replies from the current system."
            ),
            summary="Nguy cơ bot AWS cũ vẫn gửi reply song song — kiểm tra process trùng lặp.",
            tags=["aws", "duplicate", "risk", "fallback-phrases", "deploy"],
            importance=8,
            memory_type="episodic",
        ),
        dict(
            title="9Router Model Policy",
            content=(
                "Chat/TikTok/Telegram/search use cx/gpt-5.5 (fast, cheap). "
                "Coding/reasoning use cc/claude-sonnet-4-6 (best available on 9Router). "
                "Critic uses cx/gpt-5.3-codex. Fallback: openai/gpt-4o-mini. "
                "Model list cached 5 minutes. ENV vars can override but must be validated against /models. "
                "Do NOT hardcode model names — always use select_model(role)."
            ),
            summary="9Router policy: chat→cx/gpt-5.5, coding→cc/claude-sonnet-4-6, fallback→gpt-4o-mini.",
            tags=["9router", "models", "policy", "llm"],
            importance=7,
            memory_type="procedural",
        ),
        dict(
            title="Platform Architecture — Channel Separation",
            content=(
                "Telegram bot = admin command center (owner only, never public). "
                "TikTok bot = Chatgibiti social worker (responds to customer DMs). "
                "Backend FastAPI at port 8000 = LLM gateway for both channels. "
                "Task Queue in SQLite. Skill Registry with risk levels (low/medium/high). "
                "High-risk actions require /confirm_action. Audit log at data/audit/actions.jsonl."
            ),
            summary="Telegram=admin, TikTok=chatgibiti, backend=LLM gateway. High-risk cần confirm.",
            tags=["architecture", "telegram", "tiktok", "backend", "platform"],
            importance=7,
            memory_type="procedural",
        ),
    ]

    inserted = 0
    for s in seeds:
        add_memory(
            title=s["title"],
            content=s["content"],
            summary=s.get("summary", ""),
            namespace=namespace,
            memory_type=s.get("memory_type", "semantic"),
            tags=s.get("tags", []),
            importance=s.get("importance", 5),
        )
        inserted += 1
    return inserted
