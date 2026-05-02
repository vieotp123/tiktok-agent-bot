"""
Memory Store — multi-tier persistent memory for the agent platform.

Tiers (from AGENT_BLUEPRINT.md):
  raw_events    — append-only chronological action log (JSONL, 7-day TTL)
  memories      — semantic facts about users/topics (SQLite)
  lessons       — episodic outcomes: what worked / what failed per skill
  skill_notes   — procedural tuning notes per skill (human-reviewed)

Design rules (from LangGraph + CrewAI patterns):
  - Never dump raw_events into LLM prompts.
  - Retrieve only top-K relevant memories (simple keyword match for now).
  - Lessons are injected as few-shot examples only when the same skill reruns.
  - skill_notes are human-reviewed before activation.
  - No vector DB yet — plain keyword search is sufficient for MVP.

Future:
  - Add embedding-based similarity search (sentence-transformers or API).
  - Add TTL pruning for raw_events (delete rows > 7 days old).
  - Add memory summarization: compact old entries into a summary row.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH  = Path("/opt/tiktok-bot/data/memory_store.db")
RAW_FILE = Path("/opt/tiktok-bot/data/audit/raw_events.jsonl")  # separate from action audit


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    memory_type TEXT    NOT NULL DEFAULT 'semantic',  -- semantic | episodic | procedural
    tags        TEXT    NOT NULL DEFAULT '[]',         -- JSON list of tag strings
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill       TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,   -- "success" | "failure" | "partial"
    lesson_text TEXT    NOT NULL,
    task_id     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    skill       TEXT    NOT NULL,
    note        TEXT    NOT NULL,
    author      TEXT    NOT NULL DEFAULT 'human',  -- "human" | "agent" (human-reviewed)
    active      INTEGER NOT NULL DEFAULT 1,        -- 0 = disabled
    created_at  TEXT    NOT NULL
);
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


# ── Raw events (JSONL) ────────────────────────────────────────────────────────

def add_raw_event(
    source: str,
    action: str,
    summary: str,
    metadata: dict | None = None,
) -> None:
    """Append one raw event to the JSONL log. Never include secrets in metadata."""
    RAW_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":       _now(),
        "source":   source,
        "action":   action,
        "summary":  summary[:300],
        "metadata": metadata or {},
    }
    with open(RAW_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def tail_raw_events(n: int = 20) -> list[dict]:
    """Return last N raw events (newest last)."""
    if not RAW_FILE.exists():
        return []
    lines = RAW_FILE.read_text(encoding="utf-8").strip().splitlines()
    events = []
    for line in lines[-n:]:
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    return events


# ── Semantic memories ─────────────────────────────────────────────────────────

def add_memory(
    username: str,
    text: str,
    memory_type: str = "semantic",
    tags: list[str] | None = None,
) -> int:
    """
    Add a semantic memory entry for a user.
    Returns the new row id.
    """
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO memories (username, text, memory_type, tags, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (username, text[:1000], memory_type, json.dumps(tags or []), now, now),
        )
        return cur.lastrowid


def search_memory_simple(
    username: str,
    query: str,
    limit: int = 5,
    memory_type: str | None = None,
) -> list[dict]:
    """
    Simple keyword search over memories for a user.
    Searches `text` and `tags` columns (LIKE match, case-insensitive).
    No vector search — straightforward LIKE query.
    Returns list of dicts sorted by created_at DESC.
    """
    q = f"%{query.lower()}%"
    sql = (
        "SELECT * FROM memories WHERE username=? AND (LOWER(text) LIKE ? OR LOWER(tags) LIKE ?)"
    )
    params: list = [username, q, q]
    if memory_type:
        sql += " AND memory_type=?"
        params.append(memory_type)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def list_memories(
    username: str,
    memory_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return recent memories for a user, optionally filtered by type."""
    sql = "SELECT * FROM memories WHERE username=?"
    params: list = [username]
    if memory_type:
        sql += " AND memory_type=?"
        params.append(memory_type)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete_memory(memory_id: int) -> bool:
    with _conn() as conn:
        conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    return True


# ── Episodic lessons ──────────────────────────────────────────────────────────

def add_lesson(
    skill: str,
    outcome: str,
    lesson_text: str,
    task_id: str = "",
) -> int:
    """
    Record what happened when a skill ran.
    outcome: "success" | "failure" | "partial"
    """
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO lessons (skill, outcome, lesson_text, task_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (skill, outcome, lesson_text[:500], task_id, now),
        )
        return cur.lastrowid


def list_lessons(
    skill: str | None = None,
    outcome: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return recent lessons, optionally filtered by skill or outcome."""
    sql = "SELECT * FROM lessons WHERE 1=1"
    params: list = []
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


def format_lessons_list(skill: str | None = None, limit: int = 10) -> str:
    """Human-readable lessons for Telegram /lessons command."""
    lessons = list_lessons(skill=skill, limit=limit)
    if not lessons:
        target = f" for skill '{skill}'" if skill else ""
        return f"No lessons recorded{target} yet."
    icon_map = {"success": "✅", "failure": "❌", "partial": "⚠️"}
    lines = [f"<b>Lessons</b>{' — ' + skill if skill else ''} (last {len(lessons)})"]
    for l in lessons:
        icon = icon_map.get(l["outcome"], "•")
        lines.append(
            f"{icon} <b>{l['skill']}</b> ({l['outcome']})\n"
            f"   {l['lesson_text'][:120]}\n"
            f"   <i>{l['created_at'][:16]}</i>"
        )
    return "\n\n".join(lines)


# ── Procedural skill notes ────────────────────────────────────────────────────

def add_skill_note(
    skill: str,
    note: str,
    author: str = "human",
    active: bool = True,
) -> int:
    """
    Add a procedural note for a skill (prompt tuning, quirks, etc.).
    Only human-authored notes are trusted. Agent-authored notes need review.
    """
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO skill_notes (skill, note, author, active, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (skill, note[:500], author, int(active), now),
        )
        return cur.lastrowid


def list_skill_notes(
    skill: str | None = None,
    active_only: bool = True,
) -> list[dict]:
    sql = "SELECT * FROM skill_notes WHERE 1=1"
    params: list = []
    if skill:
        sql += " AND skill=?"
        params.append(skill)
    if active_only:
        sql += " AND active=1"
    sql += " ORDER BY created_at DESC"
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Memory search result formatter ───────────────────────────────────────────

def format_search_results(results: list[dict], query: str) -> str:
    """Human-readable search results for Telegram /memory_search command."""
    if not results:
        return f"No memories found for <b>{query}</b>."
    lines = [f"<b>Memory search:</b> {query} ({len(results)} result(s))"]
    for r in results:
        tags = json.loads(r.get("tags", "[]"))
        tag_str = " ".join(f"#{t}" for t in tags) if tags else ""
        lines.append(
            f"• [{r['memory_type']}] {r['text'][:150]}"
            + (f"\n  {tag_str}" if tag_str else "")
            + f"\n  <i>{r['created_at'][:16]}</i>"
        )
    return "\n\n".join(lines)
