"""
Lightweight eval suite — locks the agent platform's invariants.

Categories:
  1. router      — model policy (cx/gpt-5.5 for chat; Claude for coding)
  2. risk        — risk classifier rule set
  3. planner     — structured plan output
  4. lifecycle   — task state machine transitions
  5. menu        — Telegram menu builders
  6. memory      — prompt-context limits, no payload_json leak
  7. files       — send_file blocks .env / storage_state
  8. tasks       — code_task lifecycle on the live DB
  9. prompt      — prompt_builder produces a usable Claude prompt

Runs in <30s. Safe to run on prod (read-only or transactional).

CLI:
    python -m bot.agent.evals
    python -m bot.agent.evals --category risk

Telegram:
    /agent_evals
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field

import httpx

BACKEND = "http://localhost:8000"


@dataclass
class EvalResult:
    name: str
    category: str
    ok: bool
    detail: str = ""


@dataclass
class EvalReport:
    started_at: float
    finished_at: float = 0.0
    results: list[EvalResult] = field(default_factory=list)

    def add(self, name: str, category: str, ok: bool, detail: str = "") -> None:
        self.results.append(EvalResult(name, category, ok, detail))

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def total(self) -> int:
        return len(self.results)


# ── Individual eval suites ────────────────────────────────────────────────────

async def eval_router(rep: EvalReport) -> None:
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{BACKEND}/router_status")
        d = r.json() if r.status_code == 200 else {}
    except Exception as e:
        rep.add("router_reachable", "router", False, f"backend unreachable: {e}")
        return
    rep.add("router_reachable", "router",
            bool(d.get("reachable")), f"models={d.get('model_count','?')}")
    rms = d.get("role_models", {})
    chat = rms.get("chat", "?")
    rep.add("chat_uses_cx_gpt55", "router",
            chat == "cx/gpt-5.5", f"chat={chat}")
    tg = rms.get("telegram_chat", "?")
    rep.add("telegram_chat_uses_cx_gpt55", "router",
            tg == "cx/gpt-5.5", f"telegram_chat={tg}")
    coding = rms.get("coding", "?")
    rep.add("coding_uses_claude", "router",
            coding.startswith("cc/claude"), f"coding={coding}")
    rep.add("coding_not_chat_model", "router",
            not coding.startswith("cx/gpt-5"), f"coding={coding}")


def eval_risk(rep: EvalReport) -> None:
    from bot.agent.risk import classify_risk
    cases = [
        ("đăng bài lên TikTok",                    "high"),
        ("gửi DM cho khách",                       "high"),
        ("restart tiktok-bot",                     "high"),
        ("git push to main branch",                "high"),
        ("edit .env file",                         "high"),
        ("sửa storage_state",                      "high"),
        ("tìm thông tin mới nhất về eSIM Nhật",    "low"),
        ("hello bro",                              "low"),
        ("giá btc hôm nay",                        "low"),
        ("có gói eSIM nhận SMS không",             "low"),
        ("update giá softbank",                    "medium"),
        ("thêm sản phẩm mới",                      "medium"),
        ("verify product 123",                     "medium"),
    ]
    for goal, want in cases:
        got = classify_risk(goal)
        rep.add(f"risk[{goal[:30]!r}]", "risk",
                got == want, f"got={got} want={want}")


def eval_planner(rep: EvalReport) -> None:
    from bot.agent.planner import plan_goal
    cases = [
        ("hello",                                  "low"),
        ("giá btc",                                "low"),
        ("tìm trend eSIM 2026",                    "low"),
        ("đăng bài lên TikTok",                    "high"),
        ("có eSIM Nhật nhận SMS không?",           "low"),
        ("refactor menu_main",                     "medium"),
    ]
    for goal, want_risk in cases:
        p = plan_goal(goal)
        ok = (p.get("risk_level") == want_risk
              and p.get("plan_id", "").startswith("plan_")
              and len(p.get("steps", [])) > 0
              and all("id" in s and s["id"].startswith("step_")
                      for s in p["steps"]))
        rep.add(f"plan[{goal[:30]!r}]", "planner", ok,
                f"risk={p.get('risk_level')} steps={len(p.get('steps',[]))}")


def eval_lifecycle(rep: EvalReport) -> None:
    from bot.agent.task_lifecycle import (validate_transition,
                                            STATES, valid_next_states)
    # Sanity: STATES contains the canonical names
    expected = {"queued", "planning", "running", "waiting_confirm",
                "testing", "deploying", "done", "failed", "cancelled",
                "paused"}
    rep.add("lifecycle_state_set", "lifecycle",
            expected.issubset(STATES), f"have={set(STATES)}")
    # Valid transitions
    for old, new in [
        ("queued", "planning"),
        ("planning", "running"),
        ("running", "testing"),
        ("testing", "deploying"),
        ("deploying", "done"),
        ("running", "waiting_confirm"),
        ("waiting_confirm", "running"),
        ("failed", "queued"),
    ]:
        ok, why = validate_transition(old, new)
        rep.add(f"transit[{old}->{new}]", "lifecycle", ok, why)
    # Invalid transitions
    for old, new in [
        ("done", "running"),
        ("cancelled", "running"),
        ("queued", "deploying"),
        ("queued", "done"),
        ("done", "queued"),
    ]:
        ok, why = validate_transition(old, new)
        rep.add(f"reject[{old}->{new}]", "lifecycle", not ok,
                f"correctly rejected: {why}")
    # Self-loop is no-op
    ok, _ = validate_transition("running", "running")
    rep.add("transit[running->running]", "lifecycle", ok, "no-op accepted")


def eval_menu(rep: EvalReport) -> None:
    from bot.telegram_bot import (
        menu_main, menu_status, menu_router, menu_tasks, menu_search,
        menu_files, menu_skills, menu_memory, menu_sales, menu_admin,
        menu_code, menu_agent,
    )
    menus = {
        "main": menu_main, "status": menu_status, "router": menu_router,
        "tasks": menu_tasks, "search": menu_search, "files": menu_files,
        "skills": menu_skills, "memory": menu_memory, "sales": menu_sales,
        "admin": menu_admin, "code": menu_code, "agent": menu_agent,
    }
    for name, fn in menus.items():
        try:
            text, kb = fn()
            ok = (bool(text) and isinstance(kb, dict)
                  and "inline_keyboard" in kb
                  and len(kb["inline_keyboard"]) >= 1)
            rep.add(f"menu_{name}", "menu", ok,
                    f"rows={len(kb.get('inline_keyboard',[]))}")
        except Exception as e:
            rep.add(f"menu_{name}", "menu", False, f"error: {e}")


def eval_memory(rep: EvalReport) -> None:
    from bot.memory_store import build_memory_context
    # Synthesize a query that should hit the seeded "Japan eSIM" memory
    ctx = build_memory_context("japan", namespace="global")
    rep.add("memory_context_nonempty_for_japan", "memory",
            bool(ctx), f"len={len(ctx)}")
    rep.add("memory_context_under_6000_chars", "memory",
            len(ctx) <= 6000, f"len={len(ctx)}")
    rep.add("memory_context_no_payload_json", "memory",
            "payload_json" not in ctx, "")
    # Empty namespace returns empty or small context
    ctx2 = build_memory_context("xyzzy_no_match_xyz",
                                 namespace="eval_namespace_does_not_exist")
    rep.add("memory_context_safe_unknown", "memory",
            len(ctx2) < 4000, f"len={len(ctx2)}")


def eval_memory_v2(rep: EvalReport) -> None:
    """Memory v2 — content-hash dedup, importance auto-decay, lesson retry recall."""
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from bot import memory_store as _ms

    ns = "eval_memory_v2"

    # Clean slate so the eval is repeatable on the live DB.
    with _ms._conn() as _c:
        _c.execute("DELETE FROM memories WHERE namespace=?", (ns,))
        _c.execute("DELETE FROM lessons  WHERE namespace=? AND skill='eval_skill_v2'", (ns,))

    # 1. Dedup: identical content → single row, importance bumped on re-add.
    id1 = _ms.add_memory("Title A", "Body A", namespace=ns, importance=4)
    id2 = _ms.add_memory("Title A", "Body A", namespace=ns, importance=4)
    rep.add("memory_v2_dedup_same_id", "memory_v2",
            id1 == id2, f"id1={id1} id2={id2}")
    with _ms._conn() as _c:
        n = _c.execute("SELECT COUNT(*) FROM memories WHERE namespace=?", (ns,)).fetchone()[0]
        imp = _c.execute("SELECT importance FROM memories WHERE id=?", (id1,)).fetchone()[0]
    rep.add("memory_v2_dedup_one_row", "memory_v2",
            n == 1, f"rows={n}")
    rep.add("memory_v2_dedup_bumps_importance", "memory_v2",
            imp == 5, f"importance={imp} (4 → 5 expected)")

    # Whitespace + case differ but normalize to same hash → still dedup.
    id3 = _ms.add_memory("  TITLE A  ", "  body a  ", namespace=ns)
    rep.add("memory_v2_dedup_normalized", "memory_v2",
            id3 == id1, f"id3={id3}")

    # Distinct content → distinct row.
    id4 = _ms.add_memory("Title B", "Body B", namespace=ns, importance=2)
    rep.add("memory_v2_distinct_inserts", "memory_v2",
            id4 != id1, f"id4={id4}")

    # 2. Decay: simulate a memory created 95 days ago, no last_used.
    old_ts = (_dt.now(_tz.utc) - _td(days=95)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _ms._conn() as _c:
        _c.execute(
            "UPDATE memories SET created_at=?, last_used_at='', importance=8 WHERE id=?",
            (old_ts, id4),
        )
    n_changed = _ms.decay_unused_memories(namespace=ns, half_life_days=30, floor=1)
    with _ms._conn() as _c:
        new_imp = _c.execute("SELECT importance FROM memories WHERE id=?", (id4,)).fetchone()[0]
    rep.add("memory_v2_decay_runs", "memory_v2",
            n_changed >= 1, f"changed={n_changed}")
    rep.add("memory_v2_decay_drops_importance", "memory_v2",
            new_imp == 5, f"importance={new_imp} (8 - floor(95/30)=3 → 5)")

    # Floor respected: re-running decay on a maxed-out floor row is a no-op.
    with _ms._conn() as _c:
        _c.execute("UPDATE memories SET importance=1 WHERE id=?", (id4,))
    n2 = _ms.decay_unused_memories(namespace=ns, half_life_days=30, floor=1)
    with _ms._conn() as _c:
        floor_imp = _c.execute("SELECT importance FROM memories WHERE id=?", (id4,)).fetchone()[0]
    rep.add("memory_v2_decay_respects_floor", "memory_v2",
            floor_imp == 1 and n2 == 0,
            f"floor_imp={floor_imp} changed={n2}")

    # 3. Lesson retry recall: only failures/partials surface, in importance order.
    _ms.add_lesson(skill="eval_skill_v2", outcome="success",
                   lesson_text="all green",   namespace=ns, importance=5)
    _ms.add_lesson(skill="eval_skill_v2", outcome="failure",
                   lesson_text="db locked",   namespace=ns, importance=7)
    _ms.add_lesson(skill="eval_skill_v2", outcome="partial",
                   lesson_text="timeout",     namespace=ns, importance=4)
    retry = _ms.lessons_for_retry("eval_skill_v2", namespace=ns, limit=5)
    outcomes = [l["outcome"] for l in retry]
    rep.add("memory_v2_retry_excludes_success", "memory_v2",
            "success" not in outcomes, f"outcomes={outcomes}")
    rep.add("memory_v2_retry_orders_by_importance", "memory_v2",
            len(retry) == 2 and retry[0]["lesson"] == "db locked",
            f"first={retry[0]['lesson'] if retry else None}")
    rep.add("memory_v2_retry_empty_skill_safe", "memory_v2",
            _ms.lessons_for_retry("", namespace=ns) == [], "")

    # 4. build_memory_context honours skill_hint + is_retry (cap raises to 5).
    ctx = _ms.build_memory_context(
        "anything", namespace=ns,
        skill_hint="eval_skill_v2", is_retry=True,
    )
    rep.add("memory_v2_context_uses_retry_lessons", "memory_v2",
            "db locked" in ctx and "timeout" in ctx,
            f"ctx_len={len(ctx)}")
    rep.add("memory_v2_context_excludes_success_on_retry", "memory_v2",
            "all green" not in ctx, "")

    # Cleanup
    with _ms._conn() as _c:
        _c.execute("DELETE FROM memories WHERE namespace=?", (ns,))
        _c.execute("DELETE FROM lessons  WHERE namespace=? AND skill='eval_skill_v2'", (ns,))


async def eval_bridge(rep: EvalReport) -> None:
    """Coding-worker bridge dirty-tree gate.

    Uses an isolated tmp git repo to verify:
      1. snapshot returns dirty=False on a clean tree.
      2. snapshot returns dirty=True after a tracked-file edit and lists
         that file.
      3. _files_changed_since() reports only paths newly dirty after
         the snapshot — pre-existing dirty files are excluded.
      4. run_once() refuses with status=dirty_tree when the tree is
         dirty — exercises the full gate, not just the snapshot helper.
      5. The refused task is NOT mutated (no start/finish/fail call).
    """
    import asyncio
    import os
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    from bot import coding_worker_bridge as _cwb
    from bot import code_tasks as _ct

    with tempfile.TemporaryDirectory() as td:
        tdp = _Path(td)
        env = {**os.environ, "GIT_AUTHOR_NAME": "eval",
               "GIT_AUTHOR_EMAIL": "eval@local",
               "GIT_COMMITTER_NAME": "eval",
               "GIT_COMMITTER_EMAIL": "eval@local"}
        run = lambda *a: subprocess.run(a, cwd=tdp, env=env,
                                         capture_output=True, text=True,
                                         timeout=10)
        run("git", "init", "-q", "-b", "main")
        (tdp / "a.txt").write_text("one\n")
        run("git", "add", "a.txt")
        run("git", "commit", "-q", "-m", "init")

        # Patch REPO so the bridge helpers query our tmp repo
        original_repo = _cwb.REPO
        _cwb.REPO = tdp
        try:
            snap_clean = _cwb.dirty_tree_snapshot()
            rep.add("bridge_dirty_clean_tree", "bridge",
                    snap_clean["dirty"] is False and snap_clean["files"] == [],
                    f"dirty={snap_clean['dirty']} files={snap_clean['files']}")

            # Introduce one pre-existing dirty file (simulates an
            # unrelated in-flight edit before the worker starts).
            (tdp / "a.txt").write_text("two\n")
            snap_pre = _cwb.dirty_tree_snapshot()
            rep.add("bridge_dirty_detects_modified", "bridge",
                    snap_pre["dirty"] is True and "a.txt" in snap_pre["files"],
                    f"dirty={snap_pre['dirty']} files={snap_pre['files']}")

            # Now simulate the worker ALSO touching a different file.
            (tdp / "b.txt").write_text("worker output\n")
            delta = _cwb._files_changed_since(snap_pre)
            rep.add("bridge_delta_excludes_pre_dirty", "bridge",
                    delta == ["b.txt"],
                    f"delta={delta} (must not include a.txt)")

            # 4–5. End-to-end gate: run_once must refuse on the dirty
            # tree and leave the queued task untouched. Mock the queue
            # and tool detection so the gate is the only thing under
            # test; no real CLI is invoked, no real task mutated.
            fake_task = {
                "id":          "ctk_eval_gate",
                "title":       "eval gate task",
                "description": "",
                "risk_level":  "low",
                "status":      "queued",
                "branch":      "dev-agent",
                "priority":    1,
            }
            fake_tool = _cwb.ToolInfo(
                name="claude", binary="/bin/false", version="fake",
                noninteractive_ok=True, notes="eval-only fake tool",
            )
            mutations: list[tuple] = []
            saved = {
                "tool_fn":        _cwb.get_preferred_coding_tool,
                "is_paused":      _cwb.is_paused,
                "next_queued":    _ct.next_queued_task,
                "update_task":    _ct.update_task,
                "fail_task":      _ct.fail_task,
                "finish_task":    _ct.finish_task,
                "code_is_paused": _ct.is_paused,
            }
            _cwb.get_preferred_coding_tool = lambda: fake_tool
            _cwb.is_paused                 = lambda: False
            _ct.next_queued_task           = lambda: dict(fake_task)
            _ct.is_paused                  = lambda: False
            _ct.update_task = lambda *a, **k: (
                mutations.append(("update", a, k)) or True)
            _ct.fail_task   = lambda *a, **k: (
                mutations.append(("fail",   a, k)) or True)
            _ct.finish_task = lambda *a, **k: (
                mutations.append(("finish", a, k)) or True)
            try:
                result = await _cwb.run_once(user="eval", allow_dirty=False)
            finally:
                _cwb.get_preferred_coding_tool = saved["tool_fn"]
                _cwb.is_paused                 = saved["is_paused"]
                _ct.next_queued_task           = saved["next_queued"]
                _ct.update_task                = saved["update_task"]
                _ct.fail_task                  = saved["fail_task"]
                _ct.finish_task                = saved["finish_task"]
                _ct.is_paused                  = saved["code_is_paused"]

            rep.add("bridge_run_once_refuses_dirty", "bridge",
                    result.get("status") == "dirty_tree",
                    f"status={result.get('status')!r} "
                    f"summary={(result.get('summary') or '')[:80]!r}")
            rep.add("bridge_run_once_dirty_files_reported", "bridge",
                    "a.txt" in (result.get("dirty_files_before") or []),
                    f"dirty_files_before={result.get('dirty_files_before')}")
            rep.add("bridge_run_once_no_state_change_on_refuse", "bridge",
                    not mutations,
                    f"mutations={[m[0] for m in mutations]}")
        finally:
            _cwb.REPO = original_repo


def eval_files_safety(rep: EvalReport) -> None:
    """is_safe_send_path must reject .env / storage_state."""
    from bot.telegram_files import is_safe_send_path
    cases_block = [
        "/opt/tiktok-bot/.env",
        "/opt/tiktok-bot/.env.example",
        "/opt/tiktok-bot/tiktok_storage_state.json",
        "/opt/tiktok-bot/storage_state.json",
        "../../etc/passwd",
        "/etc/passwd",
    ]
    for p in cases_block:
        try:
            ok, _ = is_safe_send_path(p)
        except Exception:
            ok = True  # if it raises, treat as unsafe
        rep.add(f"send_blocks[{p[:30]!r}]", "files",
                not ok, "blocked" if not ok else "ALLOWED!")


def eval_code_tasks(rep: EvalReport) -> None:
    from bot.code_tasks import (init_db, add_task, start_task,
                                 finish_task, cancel_task, get_task)
    init_db()
    tid = add_task("eval test task", description="from /agent_evals",
                   risk_level="low", priority=1, created_by="eval")
    t = get_task(tid)
    rep.add("code_task_added_queued", "tasks",
            bool(t) and t.get("status") == "queued", str(t and t.get("status")))

    rep.add("code_task_start_ok", "tasks",
            start_task(tid) and get_task(tid)["status"] == "running",
            get_task(tid)["status"])

    rep.add("code_task_finish_ok", "tasks",
            finish_task(tid, commit_hash="eval_xxx",
                        test_summary="eval-only") and
            get_task(tid)["status"] == "done",
            get_task(tid)["status"])

    # Lifecycle integration: done -> running should be rejected by the
    # state machine if a caller tried it (we don't actually mutate, just
    # validate via task_lifecycle).
    from bot.agent.task_lifecycle import validate_transition
    ok, _ = validate_transition("done", "running")
    rep.add("code_task_done_terminal", "tasks", not ok, "rejected as expected")

    # Cleanup: cancel a fresh task, not the done one (done is terminal)
    tid2 = add_task("eval cleanup", risk_level="low", priority=1,
                    created_by="eval")
    cancel_task(tid2)
    rep.add("code_task_cancel_ok", "tasks",
            get_task(tid2)["status"] == "cancelled",
            get_task(tid2)["status"])


def eval_telegram_routing(rep: EvalReport) -> None:
    """Verify the v2 reply-policy separation is intact:
       - send_chat_reply -> sendMessage path, never edits menu
       - edit_menu_panel -> editMessageText path, only for menu UI
       - rebuild_menu_at_bottom -> deletes old menu, sends fresh
    """
    import inspect as _inspect
    from bot import telegram_bot as tb

    src_send = _inspect.getsource(tb.send_chat_reply)
    rep.add("send_chat_reply_uses_sendMessage", "telegram_routing",
            "await send(" in src_send and "edit_msg" not in src_send,
            "calls send() and not edit_msg")

    src_edit = _inspect.getsource(tb.edit_menu_panel)
    rep.add("edit_menu_panel_uses_editMessageText", "telegram_routing",
            "edit_msg" in src_edit and "tg_call(\"editMessageText" not in src_edit,
            "calls edit_msg()")

    src_rebuild = _inspect.getsource(tb.rebuild_menu_at_bottom)
    rep.add("rebuild_menu_deletes_old", "telegram_routing",
            "delete_message" in src_rebuild and "save_menu_state" in src_rebuild,
            "deletes old + saves new")
    rep.add("rebuild_menu_sends_new", "telegram_routing",
            "await send(" in src_rebuild,
            "uses send() to land at bottom")

    # Plain-text path: bot_loop must use send_chat_reply, NOT
    # show_action_result, for non-callback replies.
    src_loop = _inspect.getsource(tb.bot_loop)
    rep.add("bot_loop_plain_text_uses_send_chat_reply", "telegram_routing",
            "await send_chat_reply(chat_id, reply" in src_loop,
            "plain-text branch routes through send_chat_reply")
    rep.add("bot_loop_outer_error_uses_send_chat_reply", "telegram_routing",
            "Agent lỗi khi xử lý lệnh" in src_loop,
            "outer try/except surfaces error to admin in Vietnamese")
    rep.add("bot_loop_clears_pending_before_handle", "telegram_routing",
            ("_session_clear()" in src_loop and
             "consumed action=" in src_loop),
            "session cleared before handler runs")

    # Callback do:* path SHOULD still use show_action_result (legitimate
    # menu edit) — confirm we didn't accidentally remove it.
    src_cb = _inspect.getsource(tb.dispatch_callback)
    rep.add("callback_do_uses_show_action_result", "telegram_routing",
            "show_action_result" in src_cb,
            "callback do: still edits panel")

    # Auth must compare as strings (avoid int/str mismatch).
    src_auth = _inspect.getsource(tb.is_admin_chat)
    rep.add("is_admin_chat_compares_strings", "telegram_routing",
            "str(chat_id) == str(TG_ADMIN)" in src_auth,
            "string comparison only")


def eval_sessions(rep: EvalReport) -> None:
    """Permission-session scopes + ALWAYS_CONFIRM_HINTS overrides."""
    from bot.agent.sessions import (
        VALID_SCOPES, can_auto_approve, grant_session, revoke_session,
        current_session, current_scope, ALWAYS_CONFIRM_HINTS,
    )

    rep.add("sessions_default_low_only", "sessions",
            current_scope() == "low_only",
            f"current_scope={current_scope()}")

    # No active session → low only auto-approves
    revoke_session(user="eval")  # clear any leftover
    ok, _ = can_auto_approve("low",    goal="hello")
    rep.add("sessions_default_low_ok", "sessions", ok, "")
    ok, _ = can_auto_approve("medium", goal="update product")
    rep.add("sessions_default_medium_blocked", "sessions",
            not ok, "medium blocked at default scope")
    ok, _ = can_auto_approve("high",   goal="restart bot")
    rep.add("sessions_default_high_blocked", "sessions",
            not ok, "high always blocked at default")

    # Grant low_medium for 5 min → medium auto-approves, high still blocked.
    grant_session("low_medium", 5, user="eval")
    ok, _ = can_auto_approve("medium", goal="update product")
    rep.add("sessions_low_medium_allows_medium", "sessions", ok,
            "low_medium → medium auto-approve")
    ok, _ = can_auto_approve("high",   goal="post to TikTok")
    rep.add("sessions_low_medium_blocks_high", "sessions",
            not ok, "high still blocked")

    # ALWAYS_CONFIRM_HINTS override every scope.
    for hint_word, sample_goal in [
        (".env",          "edit .env file"),
        ("storage_state", "rotate storage_state"),
        ("restart",       "restart tiktok-bot"),
        ("deploy",        "deploy prod"),
        ("rollback",      "rollback prod"),
    ]:
        ok, why = can_auto_approve("low", goal=sample_goal)
        rep.add(f"always_confirm[{hint_word}]", "sessions",
                not ok, f"blocked: {why[:60]}")

    # Cleanup
    revoke_session(user="eval")
    rep.add("sessions_revoke_works", "sessions",
            current_session() is None, "session cleared")

    # Validate scope set is sealed
    expected_scopes = {"low_only", "low_medium",
                       "code_low_medium", "admin_readonly"}
    rep.add("sessions_valid_scope_set", "sessions",
            set(VALID_SCOPES) == expected_scopes,
            f"VALID_SCOPES={set(VALID_SCOPES)}")


def eval_brain_evolve(rep: EvalReport) -> None:
    """Lock the brain-evolve loop semantics."""
    from bot.agent import brain_evolve as be

    # Reset baseline
    be.stop(reason="eval_reset")
    rep.add("be_disabled_after_stop", "brain_evolve",
            not be.is_enabled(), "")

    s = be.start(2, user="eval")
    rep.add("be_start_enabled", "brain_evolve",
            s["enabled"] and s["max_tasks_per_run"] == 2,
            f"max={s['max_tasks_per_run']}")

    # done → loop continues
    cont = be.on_task_done({"task_id": "ctk_eval_done",
                             "status":  "done", "summary": "ok"},
                            user="eval")
    rep.add("be_done_continues", "brain_evolve",
            cont and be.is_enabled(),
            "loop continues after done")

    # noop → stop (queue empty)
    cont = be.on_task_done({"task_id": "", "status": "noop",
                             "summary": "no queue"}, user="eval")
    rep.add("be_noop_stops", "brain_evolve",
            (not cont) and (not be.is_enabled()),
            "")

    # quota_limited → pause (enabled stays True)
    be.start(1, user="eval")
    cont = be.on_task_done({"task_id": "ctk_q",
                             "status": "quota_limited",
                             "summary": "paused"}, user="eval")
    rep.add("be_quota_pause", "brain_evolve",
            (not cont) and be.is_enabled(),
            "pause but stay enabled")

    # pending_action → stop
    be.start(1, user="eval")
    cont = be.on_task_done({"task_id": "ctk_p",
                             "status": "pending_action",
                             "summary": "high"}, user="eval")
    rep.add("be_pending_stops", "brain_evolve",
            (not cont) and (not be.is_enabled()),
            "")

    # Two consecutive failures → stop
    be.start(1, user="eval")
    be.on_task_done({"task_id": "ctk_a", "status": "worker_failed",
                      "summary": "fail1"}, user="eval")
    cont = be.on_task_done({"task_id": "ctk_b",
                             "status": "smoke_failed",
                             "summary": "fail2"}, user="eval")
    rep.add("be_two_failures_stop", "brain_evolve",
            (not cont) and (not be.is_enabled()),
            "")

    # Status panel non-empty
    panel = be.status_panel_vi()
    rep.add("be_status_panel", "brain_evolve",
            ("Brain Evolution" in panel
             and ("đang chạy" in panel or "đã dừng" in panel)),
            f"len={len(panel)}")

    # Cleanup
    be.stop(reason="eval_cleanup")


def eval_nl_router_v2(rep: EvalReport) -> None:
    """Vietnamese NL classifier coverage for the brain-evolve era."""
    from bot.agent.nl_router import classify
    cases = [
        ("tự cải thiện brain đi",                "brain_evolve_start"),
        # "làm đến khi hết quota" was originally brain_evolve_start (cap=3
        # loop); migrated to agent_autorun_start in self-improve mode
        # because cap-3 stops well before quota actually hits, while the
        # autorun self-improve mode genuinely runs until quota.
        ("làm đến khi hết quota",                 "agent_autorun_start"),
        ("dừng tự cải thiện",                     "brain_evolve_stop"),
        ("xem brain evolve",                      "brain_evolve_status"),
        ("nhớ là coding dùng opus 4.7",           "memory_add"),
        ("tìm trong memory opus",                 "memory_search"),
        ("quên cái 99",                           "memory_forget"),
        ("hết quota thì hẹn chạy tiếp",           "quota_schedule"),
        ("khi có quota thì tự làm tiếp",          "quota_schedule"),
        ("kiểm tra quota claude",                 "quota_status"),
        ("probe claude",                          "quota_status"),
        ("hello",                                 "chat"),
    ]
    for text, want in cases:
        got = classify(text)
        rep.add(f"nl[{text[:30]!r}]", "nl_router_v2",
                got.name == want,
                f"got={got.name} want={want}")


def eval_owner_command_agent(rep: EvalReport) -> None:
    """Owner Command Agent v2 — Vietnamese NL coverage for new intents.

    Covers: agent_progress, agent_autorun_*, esim_*, web_search_nl,
    browser_task, system_*, plus risk/confirm flags.
    """
    from bot.agent.nl_router import classify

    # (text, want_intent, want_risk_or_None, want_confirm_or_None)
    cases: list[tuple[str, str, str | None, bool | None]] = [
        # Agent progress
        ("đang làm tới đâu rồi",        "agent_progress",       "low",    False),
        ("sao im vậy",                  "agent_progress",       "low",    False),
        ("xem tiến độ",                 "agent_progress",       "low",    False),
        ("kẹt ở đâu",                   "agent_progress",       "low",    False),
        ("agent đang lỗi gì",           "agent_progress",       "low",    False),
        ("còn task gì chưa làm",        "agent_progress",       "low",    False),
        ("xem log gần nhất",            "agent_progress",       "low",    False),

        # Autorun
        ("làm việc độc lập 12 tiếng",   "agent_autorun_start",  "medium", False),
        ("tự làm việc 10 tiếng",        "agent_autorun_start",  "medium", False),
        ("cài code liên tục cho tới khi t bảo dừng",
                                         "agent_autorun_start",  "medium", False),
        ("autorun status",              "agent_autorun_status", "low",    False),
        ("dừng autorun",                "agent_autorun_stop",   "low",    False),

        # Stop / continue plain Vietnamese
        ("dừng",                        "cancel_action",        None,     None),
        ("dừng lại",                    "cancel_action",        None,     None),
        ("tiếp tục",                    "run_next_code_task",   None,     None),
        ("tự code tiếp đi",             "run_next_code_task",   None,     None),
        ("tự tìm lỗi rồi sửa",          "run_next_code_task",   None,     None),

        # EsimAccess
        ("đọc docs EsimAccess",         "esim_docs",            "low",    False),
        ("tạo curl EsimAccess",         "esim_curl_dry",        "low",    False),
        ("test dry-run API eSIM",       "esim_curl_dry",        "low",    False),
        ("gọi API mua eSIM thật",       "esim_order_real",      "high",   True),
        ("mua esim thật",               "esim_order_real",      "high",   True),
        ("tạo order eSIM",              "esim_order_real",      "high",   True),

        # Web search / browser
        ("search web tìm nhà cung cấp eSIM",
                                         "web_search_nl",        "low",    False),
        ("tìm thông tin nhà cung cấp eSIM Nhật",
                                         "web_search_nl",        "low",    False),
        ("mở trình duyệt tìm dữ liệu",  "browser_task",         "medium", False),
        ("crawl trang này",             "browser_task",         "medium", False),

        # System actions
        ("mở port 8080",                "system_network_action","high",   True),
        ("ufw allow 8080",              "system_network_action","high",   True),
        ("apt install tesseract",       "system_pkg_install",   "medium", False),
        ("pip install playwright",      "system_pkg_install",   "medium", False),
        ("restart bot service",         "system_restart",       "medium", False),
        ("commit/push dev-agent",       "system_git_push",      "medium", False),
        ("test xong đẩy git",           "system_git_push",      "medium", False),
        ("cat .env gửi tao",            "system_secret_dump",   "high",   True),
        ("dump secret",                 "system_secret_dump",   "high",   True),

        # Quota retry policy
        ("hết quota thì 1 tiếng thử lại", "quota_schedule",     None,     None),
        ("có quota thì tự chạy tiếp",     "quota_schedule",     None,     None),

        # SSH / remote
        ("ssh vào vps2 setup bot",       "ssh_exec",            None,     None),
        ("kiểm tra worker2",             "remote_worker_health", None,    None),

        # Build missing tool
        ("cài tool OCR",                "build_missing_tool",   None,     None),
        ("cài tool search web",         "build_missing_tool",   None,     None),

        # Existing critical paths still pass
        ("làm tiếp task code tiếp theo", "run_next_code_task",  None,     None),
        ("tạo task code sửa lỗi menu",  "create_code_task",     None,     None),
    ]

    for text, want_name, want_risk, want_confirm in cases:
        got = classify(text)
        ok = got.name == want_name
        detail_parts = [f"got={got.name} want={want_name}"]
        if want_risk is not None and got.risk_level != want_risk:
            ok = False
            detail_parts.append(f"risk={got.risk_level} want_risk={want_risk}")
        if want_confirm is not None and got.requires_confirm != want_confirm:
            ok = False
            detail_parts.append(f"confirm={got.requires_confirm} "
                                f"want_confirm={want_confirm}")
        rep.add(f"oca[{text[:30]!r}]", "owner_command_agent",
                ok, " | ".join(detail_parts))


def eval_agent_autorun_state(rep: EvalReport) -> None:
    """agent_autorun module — state machine + parsers."""
    from bot.agent import agent_autorun as _aa

    # parse_hours_vi
    rep.add("parse_hours_12",     "agent_autorun",
            _aa.parse_hours_vi("làm 12 tiếng") == 12.0, "")
    rep.add("parse_hours_2h",     "agent_autorun",
            _aa.parse_hours_vi("chạy 2h liên tục") == 2.0, "")
    rep.add("parse_hours_default","agent_autorun",
            _aa.parse_hours_vi("không có giờ") == 12.0, "fallback default")

    # parse_max_tasks_vi — only matches with explicit "tối đa" / "max"
    rep.add("parse_max_tasks_max",  "agent_autorun",
            _aa.parse_max_tasks_vi("max 5 task") == 5, "")
    rep.add("parse_max_tasks_toi_da","agent_autorun",
            _aa.parse_max_tasks_vi("tối đa 30 task") == 30, "")

    # State file isolation: don't write the real state. Use a tmp file by
    # snapshotting + restoring.
    import json as _json, tempfile as _tmp, os as _os
    real_path = _aa.STATE_FILE
    backup    = real_path.read_text(encoding="utf-8") if real_path.exists() else None
    try:
        # Start a fresh autorun
        d = _aa.start(hours=2.0, max_tasks=5,
                      objective="eval test", user="evals")
        rep.add("autorun_started", "agent_autorun",
                d.get("enabled") is True and d.get("hours") == 2.0,
                f"got hours={d.get('hours')}")

        # is_due_to_stop should be False just after start
        should, reason = _aa.is_due_to_stop()
        rep.add("autorun_not_due_after_start", "agent_autorun",
                not should, f"reason={reason}")

        # record_outcome with done → completed_tasks++ ; consec_failures=0
        _aa.record_outcome({"status": "done", "task_id": "t1"})
        s = _aa.state()
        rep.add("autorun_done_counts", "agent_autorun",
                s.get("completed_tasks") == 1
                and s.get("consecutive_failures") == 0,
                f"completed={s.get('completed_tasks')}")

        # record_outcome with quota_limited → paused
        _aa.record_outcome({"status": "quota_limited", "task_id": "t2"})
        s = _aa.state()
        rep.add("autorun_pause_on_quota", "agent_autorun",
                s.get("enabled") is True
                and s.get("paused_reason") == "quota_limited",
                f"paused_reason={s.get('paused_reason')}")

        # 2 consecutive failures → due_to_stop True
        _aa.record_outcome({"status": "worker_failed", "task_id": "t3"})
        _aa.record_outcome({"status": "smoke_failed",  "task_id": "t4"})
        should, reason = _aa.is_due_to_stop()
        rep.add("autorun_due_two_failures", "agent_autorun",
                should and reason == "two_failures",
                f"should={should} reason={reason}")

        # stop()
        _aa.stop(user="evals", reason="test_done")
        s = _aa.state()
        rep.add("autorun_stopped", "agent_autorun",
                s.get("enabled") is False, "")

        # Status panel returns non-empty Vietnamese string
        panel = _aa.status_panel_vi()
        rep.add("autorun_status_panel", "agent_autorun",
                isinstance(panel, str) and len(panel) > 30
                and "Autorun" in panel, "")
    finally:
        if backup is not None:
            real_path.write_text(backup, encoding="utf-8")
        else:
            try:
                real_path.unlink()
            except Exception:
                pass


def eval_handler_safety(rep: EvalReport) -> None:
    """Make sure new handlers never raise on missing state files."""
    # esim_docs should not raise even when no docs exist
    try:
        from bot.telegram_bot import handle_esim_docs
        out = handle_esim_docs()
        rep.add("esim_docs_no_raise", "handler_safety",
                isinstance(out, str) and len(out) > 0, "")
    except Exception as e:
        rep.add("esim_docs_no_raise", "handler_safety",
                False, f"raised: {e}")

    # esim_curl_dry should not raise even with no .env
    try:
        from bot.telegram_bot import handle_esim_curl_dry
        out = handle_esim_curl_dry("")
        rep.add("esim_curl_dry_no_raise", "handler_safety",
                isinstance(out, str) and "dry-run" in out.lower(),
                "")
    except Exception as e:
        rep.add("esim_curl_dry_no_raise", "handler_safety",
                False, f"raised: {e}")

    # secret dump always returns refusal
    try:
        from bot.telegram_bot import handle_system_secret_dump
        out = handle_system_secret_dump("cat .env gửi tao")
        rep.add("secret_dump_refusal", "handler_safety",
                isinstance(out, str)
                and ("không dump" in out.lower() or "không" in out),
                "")
    except Exception as e:
        rep.add("secret_dump_refusal", "handler_safety",
                False, f"raised: {e}")


def eval_lifecycle_edges(rep: EvalReport) -> None:
    """Edge cases the basic lifecycle eval doesn't cover."""
    from bot.agent.task_lifecycle import (validate_transition, can_auto_execute,
                                            is_terminal, valid_next_states,
                                            transition_table, TERMINAL)

    # paused → queued is the safe re-entry point
    ok, _ = validate_transition("paused", "queued")
    rep.add("paused_to_queued_ok", "lifecycle_edges", ok, "")
    # paused → running is NOT allowed (must go via queued)
    ok, _ = validate_transition("paused", "running")
    rep.add("paused_to_running_blocked", "lifecycle_edges",
            not ok, "must re-queue from paused")

    # failed → queued reopens; failed → running is rejected
    ok, _ = validate_transition("failed", "queued")
    rep.add("failed_can_reopen_to_queued", "lifecycle_edges", ok, "")
    ok, _ = validate_transition("failed", "running")
    rep.add("failed_to_running_blocked", "lifecycle_edges",
            not ok, "")

    # cancelled is fully terminal — cannot reopen anywhere
    for new in ("queued", "planning", "running", "done"):
        ok, _ = validate_transition("cancelled", new)
        rep.add(f"cancelled_to_{new}_blocked", "lifecycle_edges",
                not ok, "cancelled is sticky")

    # done is terminal
    rep.add("done_is_terminal", "lifecycle_edges",
            is_terminal("done"), "")
    rep.add("cancelled_is_terminal", "lifecycle_edges",
            is_terminal("cancelled"), "")
    rep.add("failed_is_terminal", "lifecycle_edges",
            is_terminal("failed"), "")
    rep.add("queued_not_terminal", "lifecycle_edges",
            not is_terminal("queued"), "")

    # can_auto_execute: never auto-runs high-risk
    rep.add("auto_exec_blocks_high_risk", "lifecycle_edges",
            not can_auto_execute({"status": "queued",
                                   "risk_level": "high"})[0],
            "high never auto-runs")
    # paused tasks never auto-run
    rep.add("auto_exec_blocks_paused", "lifecycle_edges",
            not can_auto_execute({"status": "paused",
                                   "risk_level": "low"})[0],
            "paused blocked")
    # waiting_confirm never auto-runs without confirm
    rep.add("auto_exec_blocks_waiting_confirm", "lifecycle_edges",
            not can_auto_execute({"status": "waiting_confirm",
                                   "risk_level": "low"})[0],
            "waiting_confirm blocked")
    # low + queued → can auto-run
    rep.add("auto_exec_low_queued_ok", "lifecycle_edges",
            can_auto_execute({"status": "queued",
                                "risk_level": "low"})[0],
            "")

    # transition_table is well-formed (every key in STATES, every value
    # only contains valid states)
    tt = transition_table()
    from bot.agent.task_lifecycle import STATES
    rep.add("transition_table_keys_valid", "lifecycle_edges",
            all(k in STATES for k in tt.keys()), "")
    rep.add("transition_table_values_valid", "lifecycle_edges",
            all(v in STATES for vs in tt.values() for v in vs), "")

    # No state can transition to itself except via the no-op pathway
    # (we accept self-loops in validate_transition explicitly).
    for s in STATES:
        ok, why = validate_transition(s, s)
        rep.add(f"selfloop[{s}]", "lifecycle_edges", ok,
                "self-loop accepted as no-op")


def eval_prompt_builder(rep: EvalReport) -> None:
    from bot.agent.prompt_builder import (build_coding_prompt,
                                           classify_coding_task,
                                           estimate_code_task_risk,
                                           select_files_to_inspect,
                                           build_test_plan)
    task = {
        "id":          "ctk_eval0001",
        "title":       "add OCR worker skeleton",
        "description": "create bot/agent/ocr_worker.py skeleton with "
                       "Playwright integration",
        "risk_level":  "medium",
        "branch":      "dev-agent",
    }
    cls   = classify_coding_task(task["description"])
    risk  = estimate_code_task_risk(task["description"])
    files = select_files_to_inspect(task["description"])
    rep.add("prompt_classify", "prompt", cls in ("feature", "infra"),
            f"cls={cls}")
    rep.add("prompt_risk_estimate", "prompt",
            risk in ("medium", "high"), f"risk={risk}")
    rep.add("prompt_file_hints_nonempty", "prompt",
            len(files) >= 1, f"hints={len(files)}")

    prompt = build_coding_prompt(task)
    must = ["Code Task — add OCR worker skeleton",
            "/opt/tiktok-bot",
            "dev-agent",
            "GITHUB_TOKEN",
            "git remote set-url origin",
            "9. Git instructions",
            "10. Final report format",
            "Stop conditions",
            "smoke_test.sh"]
    missing = [m for m in must if m not in prompt]
    rep.add("prompt_has_required_sections", "prompt",
            not missing, f"missing={missing}")
    rep.add("prompt_length_reasonable", "prompt",
            1500 <= len(prompt) <= 12000, f"len={len(prompt)}")

    plan = build_test_plan(task)
    rep.add("test_plan_includes_smoke", "prompt",
            "smoke_test.sh" in plan, "")
    rep.add("test_plan_includes_evals", "prompt",
            "bot.agent.evals" in plan, "")
    rep.add("test_plan_includes_dup_check", "prompt",
            "uniq -d" in plan, "")

    # ── New file-hint coverage ────────────────────────────────────────────
    hint_cases = [
        ("fix telegram menu rendering",      "bot/telegram_bot.py"),
        ("update prompt_builder hints",      "bot/agent/prompt_builder.py"),
        ("expand evals coverage",            "bot/agent/evals.py"),
        ("session grant scope tweak",        "bot/agent/sessions.py"),
        ("tweak self_check observability",   "bot/agent/self_check.py"),
        ("new code_tasks status field",      "bot/code_tasks.py"),
        ("worker_roles dashboard order",     "bot/agent/worker_roles.py"),
        ("audit log JSON schema",            "bot/agent/audit_log.py"),
        ("self_improve loop",                "bot/agent/self_improve.py"),
        ("BTC fallback",                     "bot/tools.py"),
        ("rebrand telegram_report",          "bot/telegram_report.py"),
    ]
    for desc, expect in hint_cases:
        files = select_files_to_inspect(desc)
        ok = expect in files
        rep.add(f"hint[{desc[:28]!r}]", "prompt", ok,
                f"got={files[:3]} need={expect}")

    # ── New high-risk classification cases ────────────────────────────────
    risk_cases_high = [
        "edit deploy_prod.sh",
        "modify rollback_prod.sh",
        "rotate GITHUB_TOKEN",
        "drop table products",
        "alter table memories",
        "merge main",
    ]
    for desc in risk_cases_high:
        got = estimate_code_task_risk(desc)
        rep.add(f"risk_high[{desc[:28]!r}]", "prompt",
                got == "high", f"got={got}")


def eval_ocr(rep: EvalReport) -> None:
    """OCR tool — module hygiene, redaction, intent routing, skill enabled."""
    from bot.ocr import (
        is_ocr_safe_path, redact_secrets, is_ocr_intent,
        OCR_ALLOWED_EXTS, OCR_MAX_BYTES,
    )
    from bot.agent.nl_router import classify
    from bot.agent.skill_registry import get_skill

    # Redaction MUST scrub plausible secrets before they hit audit/reply.
    samples = [
        ("token: ghp_abcdef0123456789xyz live",     "ghp_abcdef0123456789xyz"),
        ("Authorization: Bearer abcdefghij1234567890", "abcdefghij1234567890"),
        ("api_key=sk-test1234567890ABCDEFGH ok",     "sk-test1234567890ABCDEFGH"),
        ("password = SuperSecret!23 done",           "SuperSecret!23"),
    ]
    for raw, leaked in samples:
        redacted, n = redact_secrets(raw)
        rep.add(f"ocr_redact[{leaked[:18]!r}]", "ocr",
                n >= 1 and leaked not in redacted,
                f"n={n} redacted={redacted[:60]!r}")

    # Empty / falsy input is a no-op (no crash, no false positive count).
    redacted, n = redact_secrets("")
    rep.add("ocr_redact_empty_safe", "ocr",
            redacted == "" and n == 0, f"n={n}")

    # Path safety mirrors is_safe_send_path + extension allow-list + size cap.
    block_cases = [
        "/opt/tiktok-bot/.env",
        "/opt/tiktok-bot/tiktok_storage_state.json",
        "/etc/passwd",
        "/opt/tiktok-bot/docs/CURRENT_STATUS.md",  # wrong extension
        "/opt/tiktok-bot/data/code_prompts/foo.txt",
    ]
    for path in block_cases:
        ok, _ = is_ocr_safe_path(path)
        rep.add(f"ocr_blocks[{path[-30:]!r}]", "ocr",
                not ok, "blocked" if not ok else "ALLOWED!")

    # Hard caps are sane (no accidental loosening).
    rep.add("ocr_extension_allowlist", "ocr",
            OCR_ALLOWED_EXTS == frozenset({".png", ".jpg", ".jpeg",
                                            ".webp", ".gif"}),
            f"allow={sorted(OCR_ALLOWED_EXTS)}")
    rep.add("ocr_size_cap_8mb", "ocr",
            OCR_MAX_BYTES == 8 * 1024 * 1024,
            f"cap={OCR_MAX_BYTES}")

    # NL intent: explicit OCR phrasings classify to ocr_image, not chat.
    nl_cases = [
        ("/ocr abcdef12",                           "ocr_image"),
        ("ocr abcdef12",                            "ocr_image"),
        ("đọc text trong ảnh abcdef12",             "ocr_image"),
        ("phân tích ảnh abcdef12",                  "ocr_image"),
        ("extract text from image foo.png",         "ocr_image"),
        # build-tool path must still beat ocr_image when phrased as
        # "thêm tool ocr ..." — protects the owner-tooling doctrine.
        ("thêm tool ocr vào agent",                 "build_missing_tool"),
        ("hello bro",                               "chat"),
    ]
    for text, want in nl_cases:
        got = classify(text)
        rep.add(f"ocr_nl[{text[:28]!r}]", "ocr",
                got.name == want, f"got={got.name} want={want}")

    # is_ocr_intent (caption-fallback helper) is consistent with the
    # NL classifier on the "yes" cases.
    rep.add("ocr_intent_helper_yes", "ocr",
            is_ocr_intent("đọc text trong ảnh") and is_ocr_intent("ocr now"),
            "")
    rep.add("ocr_intent_helper_no", "ocr",
            not is_ocr_intent("hello bro"), "")

    # Skill is registered, enabled, and risk=low.
    skill = get_skill("ocr_image")
    rep.add("ocr_skill_registered", "ocr",
            skill is not None and skill.enabled
            and skill.risk_level == "low"
            and skill.handler == "handle_ocr",
            f"skill={skill}")


async def eval_seo(rep: EvalReport) -> None:
    """SEO research worker v0 — module hygiene + skill + worker role."""
    from bot.seo_research import (
        _slug, fetch_google_suggestions, research_keyword,
        format_research_vi,
    )
    from bot.agent.skill_registry import get_skill
    from bot.agent.worker_roles import list_worker_roles

    # _slug: stable, capped, never empty.
    rep.add("seo_slug_basic", "seo",
            _slug("eSIM Nhật") == "esim-nhật", f"got={_slug('eSIM Nhật')!r}")
    rep.add("seo_slug_empty", "seo",
            _slug("") == "seed", f"got={_slug('')!r}")
    rep.add("seo_slug_cap", "seo",
            len(_slug("a" * 200)) <= 40, f"len={len(_slug('a' * 200))}")
    rep.add("seo_slug_strips_punct", "seo",
            _slug("Hello, World!") == "hello-world",
            f"got={_slug('Hello, World!')!r}")

    # research_keyword on empty seed must be a clean error envelope, not a raise.
    out = await research_keyword("", lang="vi")
    rep.add("seo_empty_seed_envelope", "seo",
            out.get("error") == "empty_seed"
            and out.get("suggestions") == []
            and out.get("results") == [],
            f"out keys={sorted(out.keys())}")

    # format_research_vi handles empty + populated cases without raising.
    rep.add("seo_format_empty_safe", "seo",
            "thiếu seed" in format_research_vi({"seed": ""}),
            "")
    sample = {
        "seed": "test",
        "suggestions": ["a", "b"],
        "results": [{"title": "T", "url": "u", "snippet": "s"}],
    }
    s_out = format_research_vi(sample)
    rep.add("seo_format_populated", "seo",
            "test" in s_out and "DDG" in s_out and "Google" in s_out,
            f"len={len(s_out)}")

    # Skill registered, enabled, low-risk, correct handler.
    skill = get_skill("seo_research")
    rep.add("seo_skill_registered", "seo",
            skill is not None and skill.enabled
            and skill.risk_level == "low"
            and skill.handler == "research_keyword",
            f"skill={skill}")

    # Worker role flipped from placeholder to live.
    seo_role = next((w for w in list_worker_roles()
                      if w.name == "seo_marketing"), None)
    rep.add("seo_worker_role_live", "seo",
            seo_role is not None and seo_role.status == "live"
            and seo_role.risk_level == "low",
            f"role={seo_role}")


async def eval_content_factory(rep: EvalReport) -> None:
    """Content factory v0 — module hygiene + skill + worker role."""
    from bot.content_factory import (
        CAPTION_MAX_CHARS, _build_caption_prompt, _extract_hashtags,
        _slug, _strip_trailing_hashtags, format_post_vi,
        generate_image_brief, write_caption,
    )
    from bot.agent.skill_registry import get_skill
    from bot.agent.worker_roles import list_worker_roles

    # _slug stable + capped + non-empty fallback.
    rep.add("cf_slug_basic", "content_factory",
            _slug("eSIM Nhật") == "esim-nhật",
            f"got={_slug('eSIM Nhật')!r}")
    rep.add("cf_slug_empty", "content_factory",
            _slug("") == "post", f"got={_slug('')!r}")
    rep.add("cf_slug_cap", "content_factory",
            len(_slug("a" * 200)) <= 40, f"len={len(_slug('a' * 200))}")

    # Caption prompt mentions brief, language, no-invent rule.
    prompt = _build_caption_prompt(
        "eSIM Nhật 7 ngày 5GB", lang="vi", audience="customer",
    )
    rep.add("cf_prompt_brief_in", "content_factory",
            "eSIM Nhật 7 ngày 5GB" in prompt, "")
    rep.add("cf_prompt_no_price_invent", "content_factory",
            "KHÔNG bịa giá" in prompt, "")
    rep.add("cf_prompt_caption_cap", "content_factory",
            str(CAPTION_MAX_CHARS) in prompt,
            f"cap={CAPTION_MAX_CHARS}")

    # Hashtag extractor: order-preserving, case-insensitive dedup.
    tags = _extract_hashtags(
        "Hello #esim #Nhật #eSIM #5g 🚀 #esim",
    )
    rep.add("cf_hashtags_extracted", "content_factory",
            tags == ["#esim", "#Nhật", "#5g"],
            f"got={tags}")
    rep.add("cf_hashtags_empty_safe", "content_factory",
            _extract_hashtags("") == [], "")

    # Trailing-hashtag stripper preserves caption body.
    body = _strip_trailing_hashtags(
        "Caption thân thiện. CTA gọn.\n#esim #nhat",
    )
    rep.add("cf_strip_trailing_tags", "content_factory",
            body == "Caption thân thiện. CTA gọn.",
            f"got={body!r}")

    # Empty brief returns a clean error envelope (no LLM call, no raise).
    out = await write_caption("", lang="vi")
    rep.add("cf_empty_brief_envelope", "content_factory",
            out.get("error") is True
            and out.get("error_detail") == "empty_brief"
            and out.get("caption") == ""
            and out.get("hashtags") == [],
            f"out keys={sorted(out.keys())}")

    # Image brief is deterministic, vertical, no-text overlay, marked ungenerated.
    img = generate_image_brief("eSIM Nhật", lang="vi")
    rep.add("cf_image_brief_shape", "content_factory",
            img.get("generated") is False
            and img.get("backend") == "placeholder"
            and "9:16" in img.get("composition", "")
            and isinstance(img.get("palette"), list),
            f"img={sorted(img.keys())}")

    # format_post_vi handles empty + populated cases without raising.
    rep.add("cf_format_empty_safe", "content_factory",
            "thiếu brief" in format_post_vi({}),
            "")
    sample = {
        "brief":      "eSIM Nhật 7 ngày",
        "caption":    "Đi Nhật đừng quên eSIM nhé! #esim #nhat",
        "hashtags":   ["#esim", "#nhat"],
        "image_brief": img,
        "model":      "cx/gpt-5.5",
    }
    s_out = format_post_vi(sample)
    rep.add("cf_format_populated", "content_factory",
            "Caption draft" in s_out and "#esim" in s_out
            and "Image brief" in s_out,
            f"len={len(s_out)}")

    # Skill registered, enabled, medium-risk, correct handler.
    skill = get_skill("content_factory")
    rep.add("cf_skill_registered", "content_factory",
            skill is not None and skill.enabled
            and skill.risk_level == "medium"
            and skill.handler == "draft_post",
            f"skill={skill}")

    # Worker role flipped from placeholder → live, on chat model role.
    cf_role = next((w for w in list_worker_roles()
                    if w.name == "content_factory"), None)
    rep.add("cf_worker_role_live", "content_factory",
            cf_role is not None and cf_role.status == "live"
            and cf_role.model_role == "chat"
            and cf_role.risk_level == "medium",
            f"role={cf_role}")


# ── Driver ────────────────────────────────────────────────────────────────────

def eval_skill_registry_v2(rep: EvalReport) -> None:
    """Tool Registry v2 — auto-discovery, stats, NL toggle, persistence."""
    import json as _json
    import tempfile as _tmp
    from pathlib import Path as _Path
    from bot.agent import skill_registry as _sr
    from bot.agent.nl_router import classify as _classify

    # Discovery: returns the live registry, never empty on a healthy boot.
    rows = _sr.discover_skills()
    rep.add("skill_v2_discover_nonempty", "skill_registry_v2",
            len(rows) >= 15, f"discovered={len(rows)}")
    rep.add("skill_v2_discover_shape", "skill_registry_v2",
            all("skill" in r and "handler_found" in r for r in rows),
            f"keys={sorted(rows[0].keys()) if rows else []}")
    # `chat` is the canonical built-in skill — every healthy boot has it
    # AND it MUST resolve to a real callable.
    chat_row = next((r for r in rows if r["skill"].name == "chat"), None)
    rep.add("skill_v2_chat_handler_found", "skill_registry_v2",
            chat_row is not None and chat_row["handler_found"] is True,
            "")

    # Stats: known shape, no NaN/None where we expect numbers.
    stats = _sr.compute_skill_stats()
    chat_stats = stats.get("chat", {})
    expected_keys = {"runs", "successes", "success_rate",
                     "last_used", "stale"}
    rep.add("skill_v2_stats_shape", "skill_registry_v2",
            expected_keys.issubset(set(chat_stats.keys())),
            f"have={sorted(chat_stats.keys())}")
    rep.add("skill_v2_stats_success_rate_bounded", "skill_registry_v2",
            all(0.0 <= s["success_rate"] <= 1.0 for s in stats.values()),
            "all rates within [0,1]")
    rep.add("skill_v2_stats_no_unknown_skills", "skill_registry_v2",
            set(stats.keys()) == {r["skill"].name for r in rows},
            "stats covers exactly the registry")

    # Stale flag: a known never-run skill (`deploy` is FUTURE/disabled
    # and has no audit entries) must be flagged stale.
    deploy_stats = stats.get("deploy", {})
    rep.add("skill_v2_stats_stale_for_unused", "skill_registry_v2",
            deploy_stats.get("stale") is True
            and deploy_stats.get("runs") == 0,
            f"deploy={deploy_stats}")

    # NL toggle classification — both directions and "xem skills".
    nl_cases = [
        ("tắt skill ocr_image",  "skill_toggle", False, "ocr_image"),
        ("bật skill chat",       "skill_toggle", True,  "chat"),
        ("disable skill foo",    "skill_toggle", False, "foo"),
        ("enable skill bar",     "skill_toggle", True,  "bar"),
        ("turn off skill baz",   "skill_toggle", False, "baz"),
    ]
    for text, want_name, want_enabled, want_target in nl_cases:
        got = _classify(text)
        ok = (got.name == want_name
              and bool(got.args.get("enabled")) == want_enabled
              and got.args.get("name") == want_target)
        rep.add(f"skill_v2_nl[{text[:28]!r}]", "skill_registry_v2", ok,
                f"got={got.name} args={got.args}")
    list_got = _classify("xem skills")
    rep.add("skill_v2_nl_list", "skill_registry_v2",
            list_got.name == "skill_list", f"got={list_got.name}")

    # Persistence: set + revert via a tmp override file so we don't
    # leave a real disable on prod.
    real_path     = _sr.OVERRIDES_FILE
    backup_text   = real_path.read_text(encoding="utf-8") if real_path.exists() else None
    target_skill  = "btc_price"
    original      = _sr.get_skill(target_skill)
    original_flag = bool(original.enabled) if original else None
    try:
        rep.add("skill_v2_set_unknown_returns_false", "skill_registry_v2",
                _sr.set_skill_enabled("__nope_xyz__", True) is False,
                "unknown skill rejected")
        rep.add("skill_v2_set_known_returns_true", "skill_registry_v2",
                _sr.set_skill_enabled(target_skill, False) is True,
                f"toggled {target_skill}")
        rep.add("skill_v2_set_persists_on_disk", "skill_registry_v2",
                real_path.exists()
                and _json.loads(real_path.read_text(encoding="utf-8"))
                       .get(target_skill) is False,
                f"overrides_file={real_path}")
        rep.add("skill_v2_set_applies_to_registry", "skill_registry_v2",
                _sr.get_skill(target_skill).enabled is False,
                "live skill flipped")
        # apply_overrides() re-applies after a reset
        _sr.get_skill(target_skill).enabled = True   # simulate fresh import
        n = _sr.apply_overrides()
        rep.add("skill_v2_apply_reapplies", "skill_registry_v2",
                n >= 1 and _sr.get_skill(target_skill).enabled is False,
                f"changed={n}")
    finally:
        # Restore prior state — registry flag and override file.
        if original is not None and original_flag is not None:
            original.enabled = original_flag
        if backup_text is not None:
            real_path.write_text(backup_text, encoding="utf-8")
        else:
            try:
                real_path.unlink()
            except Exception:
                pass


def eval_nl_router_v3(rep: EvalReport) -> None:
    """NL router v3 — confidence calibration, build-question routing,
    explain_intent API, and VN↔EN translation table.

    Locks invariants the v3 spec promised:
      - short stub directives ("claude", "task") return `ambiguous`,
        not `chat`, so the backend can't hallucinate from stale state.
      - "có tool X không / làm sao để X" routes to `build_missing_tool`.
      - explain_intent surfaces a Vietnamese decision trail.
      - translate_command normalises pure-English imperatives to VN
        before classify() (no-op when text already has diacritics).
      - existing pass-throughs (`hello` → chat, `ok` → confirm_action)
        still hold after calibration.
    """
    from bot.agent.nl_router import (
        VN_EN_TABLE, classify, explain_intent, translate_command,
    )

    # ── A. Ambiguous stubs — historically chat, now must clarify ──────────
    stub_cases = [
        ("claude",       "ambiguous"),
        ("claude?",      "ambiguous"),
        ("task",         "ambiguous"),
        ("code",         "ambiguous"),
        ("tự",           "ambiguous"),
        ("làm",          "ambiguous"),
        ("làm gì",       "ambiguous"),
        ("chạy",         "ambiguous"),
    ]
    for text, want in stub_cases:
        got = classify(text)
        rep.add(f"v3_stub[{text!r}]", "nl_router_v3",
                got.name == want and got.risk_level == "low",
                f"got={got.name} risk={got.risk_level}")

    # ── B. Build-question — capability-question must offer to build, not
    #    fall to chat where the LLM might invent a fake "yes I can" reply.
    build_cases = [
        "có tool web_scraper không",
        "có tool slack_notify không",
        "có cách nào để gửi email",
        "có api nào cho gửi sms",
        "bot làm được i18n không",
        "bot có deploy được không",
        "agent chạy được headless không",
        "how to export csv",
        "how do i deploy a remote vps",
        "có thể parse pdf được không",
    ]
    for text in build_cases:
        got = classify(text)
        rep.add(f"v3_build[{text[:30]!r}]", "nl_router_v3",
                got.name == "build_missing_tool"
                and got.risk_level == "medium",
                f"got={got.name} risk={got.risk_level}")

    # ── C. Translate command — EN tokens → VN equivalents ────────────────
    translate_cases = [
        ("stop",          "dừng"),
        ("cancel",        "hủy"),
        ("next",          "tiếp tục"),
        ("continue",      "tiếp tục"),
        ("list tasks",    "liệt kê task"),
        ("list skills",   "xem skills"),
        ("show files",    "xem file"),
        ("search",        "tìm"),
        ("remember",      "nhớ"),
        ("forget",        "quên"),
    ]
    for text, want_substr in translate_cases:
        norm = translate_command(text)
        rep.add(f"v3_tr[{text!r}]", "nl_router_v3",
                want_substr in norm,
                f"got={norm!r}")

    # Diacritic-bearing input must NOT be re-translated (no-op).
    for text in ("dừng", "tìm thông tin", "nhớ là claude xài opus"):
        norm = translate_command(text)
        rep.add(f"v3_tr_noop[{text[:24]!r}]", "nl_router_v3",
                norm == text, f"got={norm!r}")

    # ── D. Cross-lingual end-to-end: translate → classify ────────────────
    cross_cases = [
        ("stop",        "cancel_action"),
        ("next",        "run_next_code_task"),
        ("continue",    "run_next_code_task"),
        ("list tasks",  "list_tasks"),
        ("list skills", "skill_list"),
    ]
    for text, want in cross_cases:
        got = classify(translate_command(text))
        rep.add(f"v3_cross[{text!r}]", "nl_router_v3",
                got.name == want, f"got={got.name} want={want}")

    # ── E. explain_intent shape — keys + key facts ───────────────────────
    e1 = explain_intent("hello")
    rep.add("v3_explain_chat_keys", "nl_router_v3",
            {"intent", "confidence", "risk_level", "reason_vi"}
                .issubset(e1.keys())
            and e1["intent"] == "chat",
            f"got_keys={sorted(e1.keys())} intent={e1['intent']}")
    e2 = explain_intent("claude")
    rep.add("v3_explain_ambiguous", "nl_router_v3",
            e2["intent"] == "ambiguous"
            and "stub" in e2["reason_vi"].lower(),
            f"reason={e2['reason_vi'][:80]}")
    e3 = explain_intent("có tool web_scraper không")
    rep.add("v3_explain_build", "nl_router_v3",
            e3["intent"] == "build_missing_tool"
            and "build" in e3["reason_vi"].lower(),
            f"reason={e3['reason_vi'][:80]}")

    # ── F. Regression — existing classifications still hold ──────────────
    regress = [
        ("hello",                 "chat"),
        ("đồng ý",                "confirm_action"),
        ("dừng",                  "cancel_action"),
        ("tự cải thiện brain đi", "brain_evolve_start"),
        ("xem skills",            "skill_list"),
        ("kiểm tra quota claude", "quota_status"),
    ]
    for text, want in regress:
        got = classify(text)
        rep.add(f"v3_regress[{text[:24]!r}]", "nl_router_v3",
                got.name == want, f"got={got.name} want={want}")

    # ── G. VN_EN_TABLE shape ──────────────────────────────────────────────
    rep.add("v3_table_size", "nl_router_v3",
            len(VN_EN_TABLE) >= 15,
            f"len={len(VN_EN_TABLE)}")
    rep.add("v3_table_lowercase_keys", "nl_router_v3",
            all(k == k.lower() for k in VN_EN_TABLE),
            "all keys lowercase")


async def run_all_evals(category: str | None = None) -> EvalReport:
    rep = EvalReport(started_at=time.time())

    if category in (None, "router"):
        await eval_router(rep)
    if category in (None, "risk"):
        eval_risk(rep)
    if category in (None, "planner"):
        eval_planner(rep)
    if category in (None, "lifecycle"):
        eval_lifecycle(rep)
    if category in (None, "menu"):
        eval_menu(rep)
    if category in (None, "memory"):
        eval_memory(rep)
    if category in (None, "memory_v2"):
        eval_memory_v2(rep)
    if category in (None, "files"):
        eval_files_safety(rep)
    if category in (None, "bridge"):
        await eval_bridge(rep)
    if category in (None, "tasks"):
        eval_code_tasks(rep)
    if category in (None, "prompt"):
        eval_prompt_builder(rep)
    if category in (None, "telegram_routing"):
        eval_telegram_routing(rep)
    if category in (None, "sessions"):
        eval_sessions(rep)
    if category in (None, "lifecycle_edges"):
        eval_lifecycle_edges(rep)
    if category in (None, "brain_evolve"):
        eval_brain_evolve(rep)
    if category in (None, "nl_router_v2"):
        eval_nl_router_v2(rep)
    if category in (None, "owner_command_agent"):
        eval_owner_command_agent(rep)
    if category in (None, "agent_autorun"):
        eval_agent_autorun_state(rep)
    if category in (None, "handler_safety"):
        eval_handler_safety(rep)
    if category in (None, "ocr"):
        eval_ocr(rep)
    if category in (None, "seo"):
        await eval_seo(rep)
    if category in (None, "content_factory"):
        await eval_content_factory(rep)
    if category in (None, "skill_registry_v2"):
        eval_skill_registry_v2(rep)
    if category in (None, "nl_router_v3"):
        eval_nl_router_v3(rep)

    rep.finished_at = time.time()
    return rep


def format_report(rep: EvalReport, html: bool = True) -> str:
    by_cat: dict[str, list[EvalResult]] = {}
    for r in rep.results:
        by_cat.setdefault(r.category, []).append(r)

    if html:
        out = [f"<b>🧪 Agent Evals</b> "
               f"<i>{rep.passed}/{rep.total} passed in "
               f"{rep.finished_at - rep.started_at:.1f}s</i>"]
        for cat in sorted(by_cat):
            cat_pass = sum(1 for r in by_cat[cat] if r.ok)
            cat_tot  = len(by_cat[cat])
            icon = "✅" if cat_pass == cat_tot else "⚠"
            out.append(f"\n<b>{icon} {cat}</b> ({cat_pass}/{cat_tot})")
            for r in by_cat[cat]:
                if not r.ok:
                    out.append(f"  ❌ {r.name} — {r.detail[:80]}")
        if rep.failed == 0:
            out.append("\n🎉 <b>All evals passed.</b>")
        else:
            out.append(f"\n💥 <b>{rep.failed} eval(s) failed.</b>")
        return "\n".join(out)

    # Plain text (CLI)
    out = [f"Agent Evals — {rep.passed}/{rep.total} passed in "
           f"{rep.finished_at - rep.started_at:.1f}s"]
    for cat in sorted(by_cat):
        cat_pass = sum(1 for r in by_cat[cat] if r.ok)
        cat_tot  = len(by_cat[cat])
        out.append(f"  [{cat:<10}] {cat_pass}/{cat_tot}")
        for r in by_cat[cat]:
            mark = "✓" if r.ok else "✗"
            out.append(f"    {mark} {r.name}: {r.detail[:80]}")
    return "\n".join(out)


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def _cli() -> int:
    p = argparse.ArgumentParser(prog="bot.agent.evals")
    p.add_argument("--category", default=None)
    p.add_argument("--html", action="store_true")
    args = p.parse_args()
    rep = asyncio.run(run_all_evals(args.category))
    print(format_report(rep, html=args.html))
    return 0 if rep.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli())
