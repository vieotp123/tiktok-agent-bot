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
            "Telegram handler error" in src_loop,
            "outer try/except surfaces error to admin")
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
        ("làm đến khi hết quota",                 "brain_evolve_start"),
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


# ── Driver ────────────────────────────────────────────────────────────────────

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
    if category in (None, "ocr"):
        eval_ocr(rep)

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
