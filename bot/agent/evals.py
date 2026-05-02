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
