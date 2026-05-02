"""
Channel capability constants — lightweight abstraction layer.

This module documents the intended role separation between channels.
No routing logic lives here; handlers enforce the separation themselves.

Architecture:
  Telegram  = COMMAND_CENTER  (admin control, file in/out, task management,
                                risk confirmation, reports)
  TikTok    = CHAT_WORKER     (user chat in TARGET_CHAT_NAME only, no high-risk
                                actions, no file control, no admin commands)

Future channels (Discord, LINE, etc.) should declare their capabilities here
before wiring them into the dispatcher.
"""

# Telegram channel — full control surface
TELEGRAM_CAPABILITIES = frozenset({
    "command_center",    # receives and routes admin commands
    "file_in",           # admin can upload files for processing
    "file_out",          # bot can send reports / generated files back
    "confirm_action",    # human-in-the-loop for high-risk actions
    "reports",           # structured output (JSON, markdown, screenshots)
    "task_control",      # /run_task, /cancel_task, /tasks, /pending_actions
    "llm_chat",          # plain-text → 9Router backend
})

# TikTok channel — social chat worker, restricted scope
TIKTOK_CAPABILITIES = frozenset({
    "chat_worker",       # respond to chat messages in TARGET_CHAT_NAME
    "social_context",    # understands group chat dynamics
    "btc_price",         # can fetch BTC price inline
    "web_search",        # can do basic web search inline
    # EXCLUDED:
    # "file_control"     — no uploading / downloading files from TikTok
    # "high_risk_actions"— no restart, no DM blast, no git ops
    # "command_center"   — TikTok is NOT the admin control surface
})

# Actions that always require Telegram /confirm_action before execution
HIGH_RISK_ACTIONS = frozenset({
    "restart_service",
    "send_tiktok_dm",
    "git_operation",
    "edit_config",
    "publish_post",
    "delete_data",
})


def channel_can(channel: str, capability: str) -> bool:
    """Check whether a named channel has a given capability."""
    caps = {
        "telegram": TELEGRAM_CAPABILITIES,
        "tiktok":   TIKTOK_CAPABILITIES,
    }
    return capability in caps.get(channel, frozenset())
