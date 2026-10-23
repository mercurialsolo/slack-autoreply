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

import json
import os
import subprocess
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

SKILL_DIR = Path(__file__).resolve().parent.parent   # the plugin root: scripts/ sits directly under it
# How the daemon invokes the skill in `claude -p`. Plugin skills are namespaced by the plugin's
# manifest name, so this is `/solo:slack-autoreply`. A machine still running the skill as a plain
# copy under ~/.claude/skills/slack-autoreply sets SKILL_COMMAND=/slack-autoreply.
SKILL_COMMAND = os.getenv("SKILL_COMMAND", "/solo:slack-autoreply").strip() or "/solo:slack-autoreply"
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
    # Engineering requests: one channel whose messages become GitHub issues for a coding agent.
    eng_channel=env("ENG_CHANNEL", ""),
    eng_repo=env("ENG_REPO", ""),
    eng_label=env("ENG_LABEL", "agent"),
    eng_match=env("ENG_MATCH", ""),          # regex a top-level message must start with, e.g. ^\*?SR-\d+
    eng_test_match=env("ENG_TEST_MATCH", r"(?i)\[test\]"),   # a tag anywhere in the SR marks it a test
    eng_test_label=env("ENG_TEST_LABEL", "sr:test"),        # filterable; closes itself after the agent reports
    eng_settle_minutes=env_int("ENG_SETTLE_MINUTES", 15),   # time for the thread to fill in before an agent starts
    # assignee=label pairs: the first @mention in a request is who it is for, and the label is the
    # queue their cluster's eng_pickup.py takes from. Empty = every request gets ENG_LABEL.
    eng_routes=dict(e.split("=", 1) for e in env_list("ENG_ROUTES") if "=" in e),
    gh_bin=env("GH_BIN", "gh"),
)

SEND_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "+1", "thumbsup"}
DROP_REACTIONS = {"x", "negative_squared_cross_mark", "no_entry_sign", "-1", "thumbsdown", "wastebasket"}
DELEGATE_REACTIONS = {"robot_face", "robot"}   # 🤖 on a proposal hands it to an agent
OK_SUBTYPES = {None, "file_share"}


# ------------------------------------------------------------- shared secrets (Infisical)
# Optional team secret store. Opt-in per machine: a machine with an Infisical identity file
# (~/.config/infisical-staffai/node.env) reads Solo's keys from the team's Infisical, and a key
# changed there reaches Solo on its next restart. Without the identity, or when Infisical can't be
# reached, the keys come from ~/.slack-autoreply/.env exactly as before.
#
# Only the keys named here are loaded, never a whole folder: /shared also holds an
# ANTHROPIC_API_KEY, and Solo bills the Claude Code login, not the API key; the rest (database
# passwords, proxies) would only leak into every `claude -p` Solo starts.
SECRET_KEYS = ("SLACK_APP_TOKEN", "SLACK_USER_TOKEN", "SLACK_BOT_TOKEN", "TYPESAFE_API_KEY", "LINEAR_API_KEY")
SECRET_SOURCES: dict[str, str] = {}   # key -> "infisical dev:/agents" | ".env"; names only, never values
LOADED_MARK = "AUTOREPLY_SECRETS"     # set once loaded, so the commands a dispatched agent runs inherit them


def _infisical_secrets(node_env: Path, mint: str, cli: str, envname: str, paths: list[str]) -> dict[str, tuple[str, str]]:
    """{key: (value, "dev:/path")} for SECRET_KEYS, the first folder that has a key winning.

    A path is `/folder` in the identity's own project, or `<project id>:/folder` in another one —
    Solo's Slack user token belongs in a project only Solo's machine can read, not in the one
    every node and container reads.

    The token lives only in this process's memory and the CLI's environment — never in
    os.environ, where every child process would inherit it, and never in argv, where `ps` shows it."""
    ident = dotenv_values(node_env)
    domain = (ident.get("INFISICAL_DOMAIN") or "").rstrip("/")
    project = ident.get("INFISICAL_PROJECT_ID") or ""
    if not domain or not project:
        raise RuntimeError(f"{node_env} has no INFISICAL_DOMAIN / INFISICAL_PROJECT_ID")
    if not domain.endswith("/api"):
        domain += "/api"
    out = subprocess.run([mint], capture_output=True, text=True, timeout=20)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(out.stderr.strip()[:200] or "mint-token gave no token")
    cli_env = {**os.environ, "INFISICAL_TOKEN": out.stdout.strip(), "INFISICAL_DOMAIN": domain}
    found: dict[str, tuple[str, str]] = {}
    for entry in paths:
        proj, _, path = entry.rpartition(":")
        res = subprocess.run([cli, "export", "--projectId", proj or project, "--env", envname, "--path", path,
                              "--format", "json", "--silent"], capture_output=True, text=True, timeout=20, env=cli_env)
        if res.returncode != 0:
            raise RuntimeError(f"export {envname}:{entry} failed: {res.stderr.strip()[:200]}")
        for row in json.loads(res.stdout or "[]"):
            if row.get("key") in SECRET_KEYS and row.get("value") and row["key"] not in found:
                found[row["key"]] = (row["value"], f"{envname}:{entry}")
    return found


def load_secrets() -> str:
    """Overlay Solo's keys from Infisical onto os.environ. Returns one line for the log, names only.

    Infisical wins over .env: it is where a key gets rotated, so a stale .env copy must not shadow it."""
    if os.getenv(LOADED_MARK):
        return f"secrets: {os.environ[LOADED_MARK]} (inherited)"
    for k in SECRET_KEYS:
        if env(k) and not env(k).endswith("..."):   # `xapp-...` is .env.example's placeholder, not a key
            SECRET_SOURCES[k] = ".env"
    node_env = Path(os.getenv("INFISICAL_NODE_ENV", "~/.config/infisical-staffai/node.env")).expanduser()
    if env("SOLO_INFISICAL", "on").lower() in ("0", "off", "false", "no"):
        line = "secrets: .env only (SOLO_INFISICAL=off)"
    elif not node_env.exists():
        line = "secrets: .env only (no Infisical identity on this machine)"
    else:
        envname = env("INFISICAL_ENV", "dev")
        try:
            found = _infisical_secrets(node_env,
                                       os.path.expanduser(env("INFISICAL_MINT_TOKEN", "~/.claude/skills/infisical/scripts/mint-token")),
                                       env("INFISICAL_BIN", "infisical"), envname,
                                       env_list("INFISICAL_PATHS", "/agents,/shared"))
        except (OSError, subprocess.SubprocessError, RuntimeError, json.JSONDecodeError) as e:
            line = f"secrets: .env only — Infisical failed: {e}"
        else:
            for k, (value, where) in found.items():
                os.environ[k] = value
                SECRET_SOURCES[k] = f"infisical {where}"
            gave = ", ".join(sorted(found)) or "none of Solo's keys"
            line = f"secrets: Infisical {envname} gave {gave}"
    missing = [k for k in SECRET_KEYS if k not in SECRET_SOURCES]
    if missing:
        line += f"; not set anywhere: {', '.join(missing)}"
    os.environ[LOADED_MARK] = line.removeprefix("secrets: ")
    return line
