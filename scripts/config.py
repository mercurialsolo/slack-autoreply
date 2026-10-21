"""Paths and settings shared by every script.

State lives outside the skill dir so a skill update never touches your data:
  ~/.slack-autoreply/            (override with AUTOREPLY_HOME)
    .env                         tokens + settings (see .env.example)
    VOICE.md                     your hand-written style notes — the skill maintains this
    voice_samples.json           your real messages, pulled and kept fresh
    inbox/                       one JSON per inbound message, with triage, decision, outcome
    outcomes.jsonl               learning signals (approved / corrected / dropped / missed …)
    corrections.jsonl            what the bot wrote vs. what you actually sent
    self_edits.jsonl             messages you typed, then edited: before → after
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


def env(key: str, default: str = "") -> str:
    """os.getenv, minus dotenv's empty-value-with-inline-comment trap.

    `.env.example` ships keys as `ALLOW_USERS=    # IDs or @handles`. python-dotenv strips an
    inline comment only when something precedes it, so an *empty* value yields the comment text
    itself. Unnoticed that is severe: ALLOW_USERS becomes a one-entry allow-list that matches
    nobody (every message silently skipped), ANTHROPIC_API_KEY becomes a 401, and CLAUDE_MODEL is
    passed to `claude -p` as a bogus model. A value that begins with '#' is always a mis-parse.
    """
    v = os.getenv(key)
    if v is None:
        return default
    v = v.strip()
    return "" if v.startswith("#") else v


def env_list(key: str, default: str = "") -> list[str]:
    return [x.strip() for x in env(key, default).split(",") if x.strip()]


def env_bool(key: str, default: bool) -> bool:
    return (env(key) or str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key) or str(default))
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key) or str(default))
    except ValueError:
        return default


CFG = dict(
    # behaviour
    mode=env("MODE", "learn").strip().lower(),             # learn | draft | auto
    delay_min=env_float("REPLY_DELAY_MIN", 20),
    delay_max=env_float("REPLY_DELAY_MAX", 90),
    only_when_away=env_bool("ONLY_WHEN_AWAY", False),
    channels=env("REPLY_IN_CHANNELS", "mentions").strip().lower(),
    group_dms=env("REPLY_IN_GROUP_DMS", "mentions").strip().lower(),
    allow_users=env_list("ALLOW_USERS"),
    block_users=env_list("BLOCK_USERS"),
    allow_channels=env_list("ALLOW_CHANNELS"),
    block_channels=env_list("BLOCK_CHANNELS"),
    quiet_hours=env("QUIET_HOURS", "").strip(),
    max_per_hour=env_int("MAX_REPLIES_PER_HOUR_PER_CONVERSATION", 6),
    disclose=env_bool("DISCLOSE_IF_ASKED", True),
    context_messages=env_int("CONTEXT_MESSAGES", 25),
    # triage
    triage_fallback_model=env("TRIAGE_FALLBACK_MODEL", "claude-haiku-5-5"),
    triage_min_confidence=env_float("TRIAGE_MIN_CONFIDENCE", 0.6),
    sensitivity_escalate=env_float("SENSITIVITY_ESCALATE", 4),
    # Slack never notifies you about your own messages, and Solo posts as you — so a draft
    # in the self-DM cannot badge. This is how you get told: any command that reaches you.
    notify_cmd=env("NOTIFY_CMD", ""),
    ack_style=env("ACK_STYLE", "reaction").strip().lower(),
    ack_emoji=env("ACK_EMOJI", "eyes").strip().strip(":"),
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
    dispatch=env("DISPATCH", "claude").strip().lower(),   # claude | none
    claude_bin=env("CLAUDE_BIN", "claude"),
    claude_cwd=os.path.expanduser(env("CLAUDE_CWD", "").strip()) or None,
    claude_model=env("CLAUDE_MODEL", "").strip() or None,
    claude_max_turns=env_int("CLAUDE_MAX_TURNS", 25),
    claude_timeout=env_int("CLAUDE_TIMEOUT_SECONDS", 600),
    # A logged-out or rate-limited node is a *standing* failure: nothing alerts until a real
    # message has already failed on it. Being logged out is caught for free by `claude auth
    # status`, which runs every tick. This is the paid half — a real one-turn draft, which is the
    # only thing that also sees a rate limit — and it is off by default because one costs ~$0.21
    # (cache creation on a 1M-context model), so watching an idle night would cost more than the
    # night's work. Set it to e.g. 360 if you want it.
    health_probe_minutes=env_int("HEALTH_PROBE_MINUTES", 0),
    dispatch_parallel=env_int("DISPATCH_PARALLEL", 2),
    dispatch_use_api_key=env_bool("DISPATCH_USE_API_KEY", False),
    session_continuity=env_bool("SESSION_CONTINUITY", True),
    # Why 1 and not 72: a resumed session replays its whole transcript, and the prompt cache that
    # makes that cheap has a 1h TTL. Resumed inside the hour a run costs ~$0.25; resumed cold it
    # pays full cache creation on everything prior and climbs with depth — measured $0.39 at the
    # start of one session and $2.76 by its twelfth run, for the same three turns. Past an hour a
    # resume buys a bigger bill and nothing else, so start fresh and let `show` rebuild the
    # thread from Slack.
    review_nudge_after=env_int("REVIEW_NUDGE_AFTER", 10),   # unread corrections before Solo asks you to run `review`
    session_max_age_hours=env_float("SESSION_MAX_AGE_HOURS", 1),
    session_max_runs=env_int("SESSION_MAX_RUNS", 20),
    context_dirs=env_list("CONTEXT_DIRS"),
    mcp_allow=env_list("CONTEXT_MCP_ALLOW", "*"),
    # Handing work to an agent starts in shadow: Solo proposes, the owner dispatches. "auto" is
    # earned later, from the record in delegations.jsonl, not switched on by hope.
    # The routing gates. 0.5 and 0.6 are the values they shipped with, not values chosen against
    # this traffic — `python3 tools/threshold_sweep.py` replays what each one catches and releases
    # on messages already scored, with no model calls.
    triage_max_execute=env_float("TRIAGE_MAX_EXECUTE", 0.5),
    triage_min_answerable=env_float("TRIAGE_MIN_ANSWERABLE", 0.5),
    triage_min_in_scope=env_float("TRIAGE_MIN_IN_SCOPE", 0.5),
    triage_short_message=env_int("TRIAGE_SHORT_MESSAGE", 30),
    delegate_mode=env("DELEGATE_MODE", "shadow"),
    delegate_reach=env("DELEGATE_REACH", ""),
    delegate_role=env("DELEGATE_ROLE", ""),
    # Solo runs on the node; the owner's agents run on their laptop. Mailboxes are per-machine —
    # a `bus send` here queues here and is never seen there — so crossing machines is relay's job.
    delegate_peer=env("DELEGATE_PEER", ""),
    delegate_repeat_hours=env_float("DELEGATE_REPEAT_HOURS", 6),   # don't re-ask the same conversation inside this
    delegate_cwd=env("DELEGATE_CWD", ""),
    claudectl_bin=env("CLAUDECTL_BIN", "claudectl"),
)

SEND_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "+1", "thumbsup"}
DROP_REACTIONS = {"x", "negative_squared_cross_mark", "no_entry_sign", "-1", "thumbsdown", "wastebasket"}
DELEGATE_REACTIONS = {"robot_face", "robot"}   # 🤖 on a proposal hands it to an agent
OK_SUBTYPES = {None, "file_share"}
