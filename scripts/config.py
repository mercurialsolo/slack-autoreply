"""Paths and settings shared by every script.

State lives outside the skill dir so a skill update never touches your data:
  ~/.slack-autoreply/            (override with AUTOREPLY_HOME)
    .env                         tokens + settings (see .env.example)
    VOICE.md                     your hand-written style notes — the skill maintains this
    voice_samples.json           your real messages, pulled and kept fresh
    inbox/                       one JSON per inbound message, with triage, decision, outcome
    outcomes.jsonl               learning signals (approved / corrected / dropped / missed …)
    corrections.jsonl            what the bot wrote vs. what you actually sent
    followups.json               "back to you by X" promises
    state.json                   daemon bookkeeping (pending drafts, sent messages)
    replies.jsonl                audit trail
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.getenv("AUTOREPLY_HOME", "~/.slack-autoreply")).expanduser().resolve()

load_dotenv(STATE_DIR / ".env")
load_dotenv()  # a .env in the cwd fills anything still missing

INBOX_DIR = STATE_DIR / "inbox"


def p(name: str) -> Path:
    return STATE_DIR / name


def ensure_dirs() -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)


def env_list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(key, default).split(",") if x.strip()]


def env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


CFG = dict(
    # behaviour
    mode=os.getenv("MODE", "learn").strip().lower(),             # learn | draft | auto
    delay_min=env_float("REPLY_DELAY_MIN", 20),
    delay_max=env_float("REPLY_DELAY_MAX", 90),
    only_when_away=env_bool("ONLY_WHEN_AWAY", False),
    channels=os.getenv("REPLY_IN_CHANNELS", "mentions").strip().lower(),
    group_dms=os.getenv("REPLY_IN_GROUP_DMS", "mentions").strip().lower(),
    allow_users=env_list("ALLOW_USERS"),
    block_users=env_list("BLOCK_USERS"),
    allow_channels=env_list("ALLOW_CHANNELS"),
    block_channels=env_list("BLOCK_CHANNELS"),
    quiet_hours=os.getenv("QUIET_HOURS", "").strip(),
    max_per_hour=env_int("MAX_REPLIES_PER_HOUR_PER_CONVERSATION", 6),
    disclose=env_bool("DISCLOSE_IF_ASKED", True),
    context_messages=env_int("CONTEXT_MESSAGES", 25),
    # triage
    triage_fallback_model=os.getenv("TRIAGE_FALLBACK_MODEL", "claude-haiku-5-5"),
    triage_min_confidence=env_float("TRIAGE_MIN_CONFIDENCE", 0.6),
    sensitivity_escalate=env_float("SENSITIVITY_ESCALATE", 4),
    ack_style=os.getenv("ACK_STYLE", "reaction").strip().lower(),
    ack_emoji=os.getenv("ACK_EMOJI", "eyes").strip().strip(":"),
    # follow-ups
    followup_lead=env_int("FOLLOWUP_REMINDER_MINUTES", 15),
    # sharing
    share_trusted=env_list("SHARE_TRUSTED_USERS"),
    # learning
    promote_n=env_int("TRUST_PROMOTE_N", 5),
    promote_score=env_float("TRUST_PROMOTE_SCORE", 0.85),
    promote_n_global=env_int("TRUST_PROMOTE_N_GLOBAL", 20),
    promote_score_global=env_float("TRUST_PROMOTE_SCORE_GLOBAL", 0.9),
    demote_bad_in_last5=env_int("TRUST_DEMOTE_BAD_IN_LAST5", 2),
    observe_hours=env_float("OBSERVE_HOURS", 4),       # auto-sent + no correction for this long = good
    draft_expire_hours=env_float("DRAFT_EXPIRE_HOURS", 24),
    voice_cap=env_int("VOICE_SAMPLES_CAP", 600),
    # dispatch (daemon → headless Claude Code)
    dispatch=os.getenv("DISPATCH", "claude").strip().lower(),   # claude | none
    claude_bin=os.getenv("CLAUDE_BIN", "claude"),
    claude_cwd=os.path.expanduser(os.getenv("CLAUDE_CWD", "").strip()) or None,
    claude_model=os.getenv("CLAUDE_MODEL", "").strip() or None,
    claude_max_turns=env_int("CLAUDE_MAX_TURNS", 25),
    claude_timeout=env_int("CLAUDE_TIMEOUT_SECONDS", 600),
    dispatch_parallel=env_int("DISPATCH_PARALLEL", 2),
    dispatch_use_api_key=env_bool("DISPATCH_USE_API_KEY", False),
    session_continuity=env_bool("SESSION_CONTINUITY", True),
    session_max_age_hours=env_float("SESSION_MAX_AGE_HOURS", 72),
    session_max_runs=env_int("SESSION_MAX_RUNS", 20),
    context_dirs=env_list("CONTEXT_DIRS"),
    mcp_allow=env_list("CONTEXT_MCP_ALLOW", "*"),
)

SEND_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "+1", "thumbsup"}
DROP_REACTIONS = {"x", "negative_squared_cross_mark", "no_entry_sign", "-1", "thumbsdown", "wastebasket"}
OK_SUBTYPES = {None, "file_share"}
