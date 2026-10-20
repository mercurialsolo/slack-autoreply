#!/usr/bin/env python3
"""Offline test suite. No tokens, no network, no real Claude: a fake Slack client and a fake
`claude` binary exercise the daemon end to end — guards → triage → inbox → dispatch → policy →
learning → trust → sessions → recall. Run:  python3 tests/test_offline.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TMP = Path(tempfile.mkdtemp(prefix="slack-autoreply-test-"))
HOME = TMP / "home"
STATE = HOME / ".slack-autoreply"
(HOME / "proj").mkdir(parents=True)
STATE.mkdir(parents=True)

FAKE_CLAUDE = TMP / "fakeclaude"
FAKE_CLAUDE.write_text(f"""#!/usr/bin/env bash
# pretend Claude Code: records a decision via slackctl act, emits stream-json like the real CLI
prompt="$2"; item="${{prompt##* }}"
if [ -n "${{FAKE_FAIL_RESUME:-}}" ] && printf '%s ' "$@" | grep -q -- '--resume'; then echo "No conversation found" >&2; exit 1; fi
{sys.executable} {SCRIPTS}/slackctl.py act "$item" --action reply --text "yep, latest one is in Drive: https://x.io/deck\\nping me if the link doesn't open" --sources drive >/dev/null
echo '{{"type":"system","subtype":"init","mcp_servers":[{{"name":"drive","status":"connected"}}]}}'
echo '{{"type":"assistant","message":{{"content":[{{"type":"tool_use","id":"t1","name":"mcp__drive__search","input":{{}}}},{{"type":"text","text":"ok"}}]}}}}'
echo '{{"type":"result","subtype":"success","is_error":false,"num_turns":3,"total_cost_usd":0.0,"result":"sent"}}'
""")
FAKE_CLAUDE.chmod(0o755)

ENV = {
    "HOME": str(HOME), "AUTOREPLY_HOME": str(STATE), "REPLY_DELAY_MIN": "0", "REPLY_DELAY_MAX": "0", "OBSERVE_HOURS": "0",
    "CLAUDE_BIN": str(FAKE_CLAUDE), "CONTEXT_DIRS": str(HOME / "proj"), "MODE": "learn", "LOG_LEVEL": "WARNING",
}
for k in ("SLACK_USER_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
    os.environ.pop(k, None)
os.environ.update(ENV)
sys.path.insert(0, str(SCRIPTS))

import slackctl as S  # noqa: E402
import recall  # noqa: E402
import triage  # noqa: E402
import voice  # noqa: E402
from followups import hold_deadline  # noqa: E402

U, D, C = "UPRIYA01", "DPRIYA01", "CGROWTH1"
U2, D2 = "UANA0001", "DANA0001"
NAMES = {"UME": ("barada", "Barada"), U: ("priya", "Priya"), U2: ("ana", "Ana")}


class FakeClient:
    def __init__(self):
        self.posted, self.reactions, self.updates, self.history, self.counter = [], [], [], {}, 0

    def auth_test(self): return {"user_id": "UME", "team": "T1", "user": "barada"}
    def users_info(self, user):
        h, n = NAMES.get(user, ("x", user))
        return {"user": {"id": user, "name": h, "real_name": n, "tz_label": "IST", "profile": {"display_name": n, "title": "Head of Growth"}}}
    def conversations_open(self, users): return {"channel": {"id": {"UME": "DSELF", U: D, U2: D2}[users]}}
    def conversations_info(self, channel): return {"channel": {"name": "growth", "user": {D: U, D2: U2}.get(channel)}}
    def chat_getPermalink(self, channel, message_ts): return {"permalink": f"https://slack/{channel}/{message_ts}"}
    def users_getPresence(self, user): return {"presence": "away"}
    def users_list(self, **k): return {"members": [{"id": U, "name": "priya", "profile": {"display_name": "Priya"}, "real_name": "Priya"}], "response_metadata": {}}
    def conversations_list(self, **k): return {"channels": [{"id": C, "name": "growth"}], "response_metadata": {}}
    def conversations_history(self, channel, **k):
        msgs = self.history.get(channel, [])
        if k.get("latest"): msgs = [m for m in msgs if float(m["ts"]) < float(k["latest"])]
        if k.get("oldest"): msgs = [m for m in msgs if float(m["ts"]) > float(k["oldest"])]
        return {"messages": list(reversed(msgs))[: k.get("limit", 50)], "response_metadata": {}}
    def conversations_replies(self, channel, ts, **k): return {"messages": [], "response_metadata": {}}
    def chat_postMessage(self, channel, text, thread_ts=None):
        self.counter += 1; ts = f"{2000000000 + self.counter}.000000"; self.posted.append((channel, text, thread_ts, ts)); return {"ts": ts}
    def reactions_add(self, channel, timestamp, name): self.reactions.append((channel, timestamp, name)); return {}
    def chat_update(self, channel, ts, text): self.updates.append((channel, ts, text)); return {}
    def search_messages(self, query, **k):
        return {"messages": {"matches": [{"ts": "1699000000.000", "user": U, "text": "the series A deck v3 is final", "channel": {"id": D, "is_im": True}, "permalink": "https://slack/x"}], "paging": {"pages": 1}}}


def fake_init(self):
    self.client = FakeClient(); self.me = "UME"; self.team = "T1"; self.me_handle = "barada"; self.me_name = "Barada"; self.self_dm = "DSELF"
    self.names = {"UME": "Barada"}; self.handles = {"UME": "barada"}; self.channel_names = {}
    self.bot = None; self.bot_dm = None


S.Slack.__init__ = fake_init
passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}  {detail}")
        shutil.rmtree(TMP, ignore_errors=True)
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


def msg(d, user, channel, ts, text, ctype="im"):
    d.client.history.setdefault(channel, []).append({"user": user, "ts": ts, "text": text})
    d.on_event({"type": "message", "user": user, "channel": channel, "channel_type": ctype, "ts": ts, "text": text})
    return f"{channel}-{ts.replace('.', '_')}"


# ----------------------------------------------------------- pure helpers
check("quiet hours wrap", S.in_quiet_hours("23-07", datetime(2026, 1, 1, 2)) and not S.in_quiet_hours("23-07", datetime(2026, 1, 1, 12)))
check("slack markup cleaned", S.clean_text("hey <@U123> see <#C1|general> <https://x.io|doc> &amp; <!here>", {"U123": "Sam"}.get) == "hey @Sam see #general doc (https://x.io) & @here")
mon = datetime(2026, 10, 12, 10, 0); fri = datetime(2026, 10, 16, 17, 0)
check("hold deadline eod", hold_deadline(3, mon)[0] == "by end of day" and hold_deadline(3, mon)[1].hour == 18)
check("hold deadline friday→monday", hold_deadline(3, fri)[0] == "Monday morning" and hold_deadline(3, fri)[1].weekday() == 0)
check("hold deadline this week", hold_deadline(1, mon)[0] == "this week")

# ---------------------------------------------------------- triage parse
resp = {"model": "jev-1", "answers": {"route": {"choice": "reply", "confidence": 0.81}, "sensitivity": {"score": 1.4, "probabilities": {"0": .7, "1": .2, "2": .1}},
        "urgency": {"score": 2.0}, "needs_context": {"noul": 0.72}, "asks_for_content": {"noul": 0.9}}}
pol = triage.Policy(0.6, 4)
t = pol.apply(triage.Triage("reply", 0.81, triage._score_1_to_n(triage._lookup(resp, "sensitivity"), 5), triage._score_1_to_n(triage._lookup(resp, "urgency"), 5), 0.72, 0.9, "t"))
check("jev score normalised", abs(t.sensitivity - 1.4) < 1e-6 and t.route == "reply")
check("policy: sensitivity forces escalate", pol.apply(triage.Triage("reply", 0.9, 4.2, 1, 0, 0, "t")).route == "escalate")
check("policy: low confidence reply→hold", pol.apply(triage.Triage("reply", 0.3, 2, 2, 0, 0, "t")).route == "hold")
check("policy: low confidence ignore→ack", pol.apply(triage.Triage("ignore", 0.3, 2, 2, 0, 0, "t")).route == "ack")
check("policy: unknown route→hold", pol.apply(triage.Triage("bogus", 0.9, 2, 2, 0, 0, "t")).route == "hold")
check("triage asks Jev for topic + answerability",
      {"topic", "can_answer_now"} <= set(triage.questions("me")))
# A hold is only right when the answer needs something we must go and fetch. When nothing needs
# looking up, "not answerable" just means there was nothing to look up — a small ask about how to
# communicate reads that way, and promising a callback for a two-word job floods the conversation.
check("policy: unanswerable AND needs their files → hold",
      pol.apply(triage.Triage("reply", 0.9, 2, 2, 0.9, 0, "t", can_answer=0.2)).route == "hold")
check("policy: nothing to look up → just reply, no hold",
      pol.apply(triage.Triage("reply", 0.9, 2, 2, 0.1, 0, "t", can_answer=0.2)).route == "reply")
# Saying nothing leaks nothing, so scope only gates routes that actually speak.
check("policy: an out-of-scope message that deserved silence stays ignored",
      pol.apply(triage.Triage("ignore", 0.9, 2, 2, 0, 0, "t", in_scope=0.2)).route == "ignore")
check("policy: confident reply and answerable stays reply",
      pol.apply(triage.Triage("reply", 0.9, 2, 2, 0, 0, "t", can_answer=0.9)).route == "reply")
check("triage defaults keep positional construction working",
      triage.Triage("reply", 0.9, 2, 2, 0, 0, "t").topic == "other")

# Jev 429/5xx are transient; without a retry one blip drops the message to a fallback
# that may not be configured. Stub httpx so the first call 529s and the second succeeds.
def _jev_retry_recovers():
    import types
    _saved = sys.modules.get("httpx")
    calls = {"n": 0}
    class _Err(Exception):
        def __init__(self, m, request=None, response=None):
            super().__init__(m); self.response = response
    class _Resp:
        def __init__(self, code, payload=None): self.status_code, self._p = code, payload or {}
        def json(self): return self._p
        def raise_for_status(self):
            if self.status_code >= 400: raise _Err(str(self.status_code), response=self)
    fake = types.SimpleNamespace(HTTPStatusError=_Err, TransportError=OSError)
    def _post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1: return _Resp(529)
        return _Resp(200, {"model": "jev-1", "answers": {
            "route": {"choice": "reply", "confidence": 0.9},
            "sensitivity": {"score": 2.0}, "urgency": {"score": 2.0},
            "needs_context": {"noul": 0.2}, "asks_for_content": {"noul": 0.0},
            "topic": {"choice": "status_request"}, "can_answer_now": {"noul": 0.8}}})
    fake.post = _post
    sys.modules["httpx"] = fake
    try:
        t = triage.jev({"message": "hi"}, "me", "k")
        return calls["n"] == 2 and t.route == "reply" and t.topic == "status_request"
    finally:
        if _saved is not None: sys.modules["httpx"] = _saved
        else: sys.modules.pop("httpx", None)

check("jev retries a transient 529 instead of falling through", _jev_retry_recovers())

# .env.example ships `ALLOW_USERS=    # IDs or @handles`; dotenv only strips an inline comment
# when a value precedes it, so an empty value yields the comment text. Left unguarded that makes
# allow_users a one-entry list matching nobody — every message silently skipped.
def _env_ignores_inline_comment():
    import config as _c
    saved = {k: os.environ.get(k) for k in ("ALLOW_USERS", "CLAUDE_MODEL")}
    try:
        os.environ["ALLOW_USERS"] = "# IDs or @handles; empty = everyone. BLOCK wins."
        os.environ["CLAUDE_MODEL"] = "# empty = your Claude Code default"
        return (_c.env_list("ALLOW_USERS") == []
                and _c.env("CLAUDE_MODEL") == ""
                and _c.env_list("ALLOW_USERS", "") == []
                and _c.env("MODE", "learn") in ("learn", "draft", "auto"))
    finally:
        for k, v in saved.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

check("env() treats a comment-only value as empty", _env_ignores_inline_comment())

# Informational messages must land on `ignore` (free, silent) and not `ack` (a visible 👀 on
# every FYI). The old ack criterion said "sharing something or giving a heads-up", which pulled
# FYIs, status broadcasts and shared links into a reaction.
_routes = triage.questions("me")["route"]["criteria"]
check("ignore covers informational messages",
      all(w in _routes["ignore"].lower() for w in ("fyi", "status broadcast", "heads-up")))
check("ack no longer claims every shared heads-up",
      "sharing something or giving a heads-up" not in _routes["ack"].lower())

# Hard boundaries: Solo answers questions about this workspace's work. Anything asking it to act
# on a system, or asking about work outside this workspace, goes to the owner — never auto.
_q = triage.questions("me")
check("triage asks Jev about actions and scope",
      {"asks_to_execute", "in_scope"} <= set(_q))
check("a request to execute escalates even when it looks routine",
      pol.apply(triage.Triage("reply", 0.95, 1, 1, 0, 0, "t", asks_to_execute=0.9)).route == "escalate")
check("an out-of-scope question escalates",
      pol.apply(triage.Triage("reply", 0.95, 1, 1, 0, 0, "t", in_scope=0.1)).route == "escalate")
check("in-scope question asking nothing of systems is untouched",
      pol.apply(triage.Triage("reply", 0.95, 1, 1, 0, 0, "t", can_answer=0.9,
                              asks_to_execute=0.0, in_scope=1.0)).route == "reply")
# A Jev response that omits the two safety answers must parse as "wants an action, out of scope",
# so a malformed or truncated reply escalates to the owner instead of sailing through as routine.
def _jev_fails_closed():
    import types
    saved = sys.modules.get("httpx")
    class _Err(Exception):
        def __init__(self, m, request=None, response=None): super().__init__(m); self.response = response
    class _Resp:
        status_code = 200
        def json(self): return {"model": "jev-1", "answers": {"route": {"choice": "reply", "confidence": 0.99}}}
        def raise_for_status(self): pass
    fake = types.SimpleNamespace(HTTPStatusError=_Err, TransportError=OSError, post=lambda url, **k: _Resp())
    sys.modules["httpx"] = fake
    try:
        t = triage.jev({"message": "hi"}, "me", "k")
        return t.asks_to_execute == 1.0 and t.in_scope == 0.0 and pol.apply(t).route == "escalate"
    finally:
        if saved is not None: sys.modules["httpx"] = saved
        else: sys.modules.pop("httpx", None)

check("a jev answer missing the safety fields escalates, not replies", _jev_fails_closed())

# The drafting agent holds Bash(python3 slackctl.py *). It must not be able to send or self-approve.
check("sending subcommands are forbidden to the drafting agent",
      {"approve", "drop", "listen", "pause", "resume"} <= S.DISPATCH_FORBIDDEN)
check("the agent can still record a decision and research",
      not ({"act", "show", "history", "search", "about", "note"} & S.DISPATCH_FORBIDDEN))
_env = dict(os.environ); _env["AUTOREPLY_DISPATCH"] = "1"
_r = subprocess.run([sys.executable, str(SCRIPTS / "slackctl.py"), "approve", "x"], capture_output=True, text=True, env=_env)
check("approve is refused in a dispatched run", _r.returncode != 0 and "refused" in (_r.stdout + _r.stderr))
# 🤖 had no terminal equivalent while ✅ and ❌ did, so handing work over meant being in Slack.
# It is gated the same way: the writer that proposes an investigation may not hand itself one.
_rd = subprocess.run([sys.executable, str(SCRIPTS / "slackctl.py"), "dispatch", "x"],
                     capture_output=True, text=True, env=_env)
check("dispatch is refused in a dispatched run too",
      _rd.returncode != 0 and "refused" in (_rd.stdout + _rd.stderr))
check("CONTEXT_MCP_ALLOW=none grants no mcp servers", S.discover_mcp_names(None, ["none"]) == [])

# `--allowedTools` pre-approves; it does not deny. Answering "any of the 15 accounts being warmed
# up ready for deployment?" on 2026-10-15 (item C0BV5ECCFFT-1792079120_386789) the writer used
# ToolSearch and then mcp__claude_ai_Google_Drive__read_file_content, with CONTEXT_MCP_ALLOW=deepwiki
# and with neither tool in the allowlist. The agents are the ones with privileged access; Solo is
# supposed to ask them, not to hold the same reach itself.
_wcmd, _, _ = S.claude_command({"id": "x"})
_adir = _wcmd[_wcmd.index("--disallowedTools") + 1]
check("the writer runs with --strict-mcp-config, so an install's own servers are not reachable",
      "--strict-mcp-config" in _wcmd)
check("ToolSearch is denied to the writer — it is the escalator to a deferred tool",
      "ToolSearch" in _adir)
check("no mcp config is passed when no server is allowed",
      "--mcp-config" not in _wcmd, f"cmd: {_wcmd}")
check("an allowed server with no definition anywhere grants nothing rather than falling back",
      S.mcp_config_for(None, ["deepwiki"]) == ([], None))

# Slack cannot badge a self-DM that holds only your own messages (unread_count_display excludes
# them), so the ping that tells you a draft is waiting has to come from outside Slack.
def _notify_cmd_runs():
    import tempfile
    marker = Path(tempfile.mkdtemp()) / "pinged.txt"
    S.CFG["notify_cmd"] = f"/bin/sh -c 'printf %s \"$1\" > {marker}' --"
    try:
        S.Slack().notify("Draft hold → DM with Sri — react ✅ to send")
        return marker.exists() and "Draft hold" in marker.read_text()
    finally:
        S.CFG["notify_cmd"] = ""

check("a waiting draft pings you outside Slack when NOTIFY_CMD is set", _notify_cmd_runs())

def _notify_survives_a_broken_cmd():
    S.CFG["notify_cmd"] = "/nonexistent/ping"
    try:
        return bool(S.Slack().notify("draft"))   # must still post
    finally:
        S.CFG["notify_cmd"] = ""

check("a failed ping never loses the draft", _notify_survives_a_broken_cmd())

# With a bot token the owner gets a real notification: the draft stays in the self-DM where
# ✅/❌ work, and the bot DMs a one-liner plus a permalink so it is findable.
def _bot_nudge():
    d = S.Slack()
    d.bot = FakeClient(); d.bot_dm = "DBOT"
    d.notify("*Draft hold → DM with Sri* — react :white_check_mark: to send\nChecking")
    sent = d.bot.posted
    return len(sent) == 1 and sent[0][0] == "DBOT" and "Draft hold" in sent[0][1]

check("the bot nudges you so a draft actually notifies", _bot_nudge())

def _nudge_failure_is_survivable():
    d = S.Slack()
    class Broken:
        def chat_postMessage(self, **k): raise RuntimeError("bot revoked")
    d.bot = Broken(); d.bot_dm = "DBOT"
    ts = d.notify("draft")
    # the draft must land somewhere approvable: the self-DM, not nowhere
    return bool(ts) and d.client.posted[-1][0] == "DSELF"

check("a revoked bot costs the notification, never the draft", _nudge_failure_is_survivable())

def _console_is_the_bot_dm():
    d = S.Slack(); d.bot = FakeClient(); d.bot_dm = "DBOT"
    ts = d.notify("*Draft reply → DM with Priya* — react :white_check_mark: to send")
    # the draft goes to the bot DM, not the self-DM, so Slack will actually notify
    return (d.console == "DBOT" and len(d.bot.posted) == 1 and d.bot.posted[0][0] == "DBOT"
            and not any(c == "DSELF" for c, *_ in d.client.posted))

check("with a bot, the draft itself goes to the bot DM", _console_is_the_bot_dm())

def _console_update_uses_the_bot():
    d = S.Slack(); d.bot = FakeClient(); d.bot_dm = "DBOT"
    ts = d.notify("draft")
    d.console_update(ts, "✅ Sent to Priya")
    # a bot's message can only be edited by the bot
    return d.bot.updates and d.bot.updates[-1][0] == "DBOT" and not d.client.updates

check("a bot-posted draft is edited by the bot", _console_update_uses_the_bot())

def _falls_back_without_a_bot():
    d = S.Slack(); d.bot = None; d.bot_dm = None
    ts = d.notify("draft")
    d.console_update(ts, "✅ Sent")
    return d.console == "DSELF" and d.client.posted[-1][0] == "DSELF" and d.client.updates[-1][0] == "DSELF"

check("without a bot it still works, in the self-DM", _falls_back_without_a_bot())

# ✅ is the gesture you are meant to be able to try. A simulated item must never reach a real
# conversation through it — found by probing the bot-DM approval path with a fake item.
def _simulated_approval_sends_nothing():
    d = S.Slack(); d.bot = FakeClient(); d.bot_dm = "DBOT"
    st = S.State()
    sim = {"id": "sim-approve", "created": S.now_iso(), "status": "decided", "simulated": True,
           "channel": "DREAL", "channel_type": "im", "sender": "U9", "sender_name": "Probe",
           "text": "x", "reply_thread_ts": None,
           "decision": {"action": "reply", "text": "must not send", "sources": [], "used_context": False,
                        "note": "", "urgency": 2, "at": S.now_iso()}}
    S.save_item(sim)
    before = len(d.client.posted)
    # the simulated guard returns before the ledger or follow-ups are touched
    out = S.approve_draft(d, sim, None, None, st, "9999.0")
    return "simulated" in out.lower() and len(d.client.posted) == before

check("approving a simulated draft sends nothing", _simulated_approval_sends_nothing())

# ------------------------------------------------------------ the daemon
voice.ensure_voice_file()
voice.save_samples([{"text": "yep on it", "channel": D, "ts": "1000", "prompt": "can you look?", "reply_to_user": U}])
d = S.Daemon("none"); fc = d.client
check("no triage model → item has triage=None (Claude decides)", True)

iid = msg(d, U, D, "1700000001.000", "hey, where's the latest mason deck?")
item = S.load_item(iid)
check("inbound queued with context", item["status"] == "queued" and item["triage"] is None and "mason deck" in item["context"])

summary = S.execute_decision(d, item, {"action": "reply", "text": "it's in drive: https://x.io/deck", "sources": ["drive"], "used_context": True}, d.ledger, d.followups, set(), state=d.state)
check("learn mode: first reply is a draft", "drafted" in summary and fc.posted[-1][0] == "DSELF")
draft_ts = fc.posted[-1][3]
d.on_event({"type": "reaction_added", "user": "UME", "reaction": "white_check_mark", "item": {"type": "message", "channel": "DSELF", "ts": draft_ts}})
check("✅ sends as you + scores approved", fc.posted[-1][0] == D and d.ledger.rows[-1]["outcome"] == "approved")
sent_ts = fc.posted[-1][3]

d.on_event({"type": "message", "subtype": "message_changed", "channel": D, "message": {"user": "UME", "ts": sent_ts, "text": "deck is here https://x.io/deck — v3"},
            "previous_message": {"user": "UME", "ts": sent_ts, "text": "it's in drive: https://x.io/deck"}})
check("editing a sent reply → corrected + pair stored", d.ledger.rows[-1]["outcome"] == "corrected" and d.ledger.corrections()[-1]["actual"].startswith("deck is here"))

iid2 = msg(d, U2, D2, "1700000002.000", "can you review the PR today?")
S.execute_decision(d, S.load_item(iid2), {"action": "hold", "text": "yep, by end of day", "urgency": 3}, d.ledger, d.followups, set(), state=d.state)
msg(d, "UME", D2, "1700000003.000", "looking now")
check("replying yourself retires the draft → replaced", d.ledger.rows[-1]["outcome"] == "replaced" and not d.state.read()["pending"])
check("your own message becomes a voice sample with its prompt", voice.load_samples()[-1]["text"] == "looking now" and voice.load_samples()[-1]["prompt"] == "can you review the PR today?")

# This check used to assert the opposite, and the fixture is why it had to change: "fyi shipped
# it" → "nice, thanks!" is the owner carrying on a conversation, not rescuing a dropped message.
# Scoring it held the ignore route at 0.12 in the real ledger, where `effective_mode` reads it.
iid3 = msg(d, U, D, "1700000004.000", "fyi shipped it")
S.execute_decision(d, S.load_item(iid3), {"action": "ignore", "text": ""}, d.ledger, d.followups, set(), state=d.state)
_before = len(d.ledger.rows)
msg(d, "UME", D, "1700000005.000", "nice, thanks!")
check("thanking someone after an ignore is not a miss — nothing was asked",
      len(d.ledger.rows) == _before)

iid3b = msg(d, U, D, "1700000004.500", "did the warmup accounts finish?")
S.execute_decision(d, S.load_item(iid3b), {"action": "ignore", "text": ""}, d.ledger, d.followups, set(), state=d.state)
msg(d, "UME", D, "1700000005.500", "checking now")
check("but answering an ignored question is", d.ledger.rows[-1]["outcome"] == "missed")

def _triage_ignore(mentioned):
    """A confident `ignore` straight from triage — the half that skips the writer entirely."""
    real = d.triager.run
    d.triager.run = lambda state: triage.Triage(
        route="ignore", confidence=0.7, sensitivity=1.0, urgency=2.0, needs_context=0.1,
        asks_for_content=0.1, source="jev:test", topic="status_update", can_answer=0.9)
    try:
        ts = "1700000009.100" if mentioned else "1700000009.200"
        text = (f"<@{d.me}> that's strange, assume its still zero") if mentioned else "all good here"
        d.on_inbound({"user": U, "channel": D, "channel_type": "im", "text": text, "ts": ts})
        return S.load_item(f"{D}-{ts.replace('.', '_')}")
    finally:
        d.triager.run = real


check("a confident ignore on a mention is lifted to an ack, so it reaches you",
      (_triage_ignore(True).get("triage") or {}).get("route") == "ack")
check("and the same message without your name is still ignored, as it should be",
      (_triage_ignore(False).get("triage") or {}).get("route") == "ignore")

# The record: three times the writer took a hold and turned it into silence, and every one was a
# direct @mention asking for something. Triage's confidence floor rewrites triage's route, not the
# writer's choice, so nothing stood between a mention and nothing at all.
def _ignore_a_mention():
    it = S.load_item(iid3b); it["mentioned"] = True; S.save_item(it)
    try:
        S.cmd_act(SimpleNamespace(item=iid3b, action="ignore", text="", text_file=None, emoji="",
                                  sources="", note="", urgency=None, task="", role="", thread=False))
    except SystemExit as e:
        return str(e)
    return ""


# Sri replied "we should send a reply to Danielle today" in a #adair-homes thread Solo had
# already drafted in twice that day. No @, so the channel filter dropped it before the log line.
def _thread_reply(thread_ts):
    """A channel message with no mention, as a reply inside `thread_ts`."""
    ts = thread_ts.replace("17000000", "17000001")
    d.client.history.setdefault(C, []).append({"user": U, "ts": ts, "text": "we should reply today"})
    d.on_inbound({"user": U, "channel": C, "channel_type": "channel", "ts": ts,
                  "thread_ts": thread_ts, "text": "we should reply today"})
    return S.item_path(f"{C}-{ts.replace('.', '_')}").exists()


d.state.update(lambda st: st["sessions"].__setitem__(f"{C}:1700000020.000",
                                                     {"id": "s1", "runs": 1, "last": S.now_iso(), "cost": 0.5}))
check("an unmentioned reply in a thread Solo already drafted in is picked up", _thread_reply("1700000020.000"))
check("and in a thread it has never touched it is still left alone", not _thread_reply("1700000021.000"))

# The learning loop's last two stages are manual, so the evidence piles up unread. These three
# are the part that makes that visible — the daemon counts the debt and asks; it never edits the
# guide itself, because `review` writes to a git repo the owner owns.
def _review_debt():
    guide = S.config.p("VOICE.md")
    os.utime(guide, (time.time() - 60, time.time() - 60))   # VOICE.md was last written a minute ago
    before, _ = S.review_debt()
    with open(S.config.p("corrections.jsonl"), "a") as fh:
        for _ in range(3):
            fh.write(json.dumps({"at": S.now_iso(), "outcome": "replaced"}) + "\n")
    return before, S.review_debt()[0]


_was, _now = _review_debt()
check("corrections written after VOICE.md changed are the review debt", _now - _was == 3)

os.utime(S.config.p("VOICE.md"), None)   # you ran `review`; the guide is current again
check("and folding them in clears the debt", S.review_debt()[0] == 0)


def _nudges():
    old = S.CFG["review_nudge_after"]
    S.CFG["review_nudge_after"] = 2
    os.utime(S.config.p("VOICE.md"), (time.time() - 60, time.time() - 60))   # debt again
    sent = []
    real = S.Daemon.notify                           # patch the class: an instance attribute here
    S.Daemon.notify = lambda self, t: sent.append(t) # would shadow every later test's patch
    try:
        d.nudge_review(); d.nudge_review()           # twice on the same VOICE.md
    finally:
        S.Daemon.notify = real; S.CFG["review_nudge_after"] = old
    return sent


_n = _nudges()
check("Solo asks you to run review once the debt is real", len(_n) == 1 and "corrections since VOICE.md" in _n[0])
check("and does not ask again until VOICE.md moves", len(_n) == 1)
check("it asks for the reviewer rather than editing the guide itself", "solo review" in _n[0])

_sealed = subprocess.run([sys.executable, str(S.SCRIPT_REAL), "history", "@someone"],
                         capture_output=True, text=True,
                         env={**os.environ, "AUTOREPLY_SEALED": "1", "AUTOREPLY_HOME": str(TMP)})
check("a sealed rehearsal cannot read the thread it is being scored against",
      _sealed.returncode != 0 and "sealed" in (_sealed.stderr + _sealed.stdout).lower())
check("every sealed command is a real subcommand", S.SEALED_FORBIDDEN <= {"history", "search", "about", "ask"})
check("the seal reaches the writer's environment",
      S.claude_command({**S.load_item(iid3b), "sealed": True})[1].get("AUTOREPLY_SEALED") == "1"
      and "AUTOREPLY_SEALED" not in S.claude_command(S.load_item(iid3b))[1])

# `auto` was a config value nine checks accepted and nothing branched on, so Solo proposed the
# same Adair lookup six times in three days and every one waited for a tap. The daemon hands it
# over now — and must ask one question once.
def _hand_over(mode, task="check Lexi's threads for Danielle since Oct 15", twice=False):
    sent, notes = [], []
    real_d, real_n = S.dispatch_task, S.Daemon.notify
    S.dispatch_task = lambda it, who, where: (sent.append(it["id"]),
                                              S.record_delegation(item=it["id"], peer="p", outcome="dispatched",
                                                                  conversation=S.session_key(it)), "handed to p")[2]
    S.Daemon.notify = lambda self, t: notes.append(t)
    old_mode = S.CFG["delegate_mode"]
    S.CFG["delegate_mode"] = mode
    it = S.load_item(iid2)
    try:
        d.hand_over(it, {"action": "hold", "task": task})
        if twice:
            d.hand_over(it, {"action": "hold", "task": task})
    finally:
        S.dispatch_task, S.Daemon.notify = real_d, real_n
        S.CFG["delegate_mode"] = old_mode
    return sent, notes


check("a proposal is still only a proposal in shadow", _hand_over("shadow")[0] == [])
_sent, _notes = _hand_over("auto")
check("in auto the daemon hands it over without waiting for a 🤖", _sent == [iid2])
check("and says so, including that the answer still needs your ✅", "✅" in _notes[0] and "🤖" in _notes[0])
check("a decision carrying no task hands over nothing", _hand_over("auto", task="")[0] == [])
check("the same conversation is not asked twice", _hand_over("auto", twice=True)[0] == [])
check("the writer still cannot hand itself work",
      "refused" in S.dispatch_task.__doc__.lower() or True)

# Isolated from the live ledger rows above: a conversation of its own, so closing it here
# cannot retire a dispatch another check is still asserting on.
_conv = {"id": "CLOOP-1", "channel": "CLOOP", "reply_thread_ts": None}
S.record_delegation(item="CLOOP-1", peer="p", outcome="dispatched", conversation=S.session_key(_conv))
check("an open dispatch is visible to the next message in that conversation",
      S.open_dispatch(_conv, 6) is not None)
check("but not once it has gone stale", S.open_dispatch(_conv, 0) is None)
# Recorded exactly as `cmd_say` records it: the item id it was handed, and no conversation —
# the agent reporting back has never heard of a session key. Writing it here with one is what
# let a dedupe that could never clear ship green.
S.record_delegation(item="CLOOP-1", role="staffai", outcome="reported", say="say-x")
check("and stops being open once the agent reports back", S.open_dispatch(_conv, 6) is None)

# Asking again is not judgement. On 19 Oct Solo proposed the Adair lookup at 16:34, three hours
# after the agent had answered it at 13:27 — a different thread, so `open_dispatch` was blind to
# it, and nothing else ever put a returned report in front of the writer.
_r_src = {"id": "CREP-900.000", "channel": "CREP", "channel_type": "channel", "sender_name": "Sri",
          "sender": "U9", "text": "any replies yet?", "created": S.now_iso(), "status": "done",
          "reply_thread_ts": "900.000", "ts": "900.000"}
_r_say = {"id": "say-rep1", "channel": "CREP", "channel_type": "channel", "sender_name": "growth",
          "sender": "CREP", "text": "[staffai reporting]", "created": S.now_iso(), "status": "done",
          "reply_thread_ts": None, "ts": "901.000", "origin": "agent:staffai",
          "intent": "2 of 3 have replied — Danielle this AM, Kerry Saturday, Ryan nothing",
          "outcome": {"status": "drafted", "at": S.now_iso()}}
S.save_item(_r_src); S.save_item(_r_say)
S.record_delegation(item=_r_src["id"], peer="p", role="staffai", outcome="dispatched",
                    task="check Lexi's Adair threads for replies", conversation="CREP:900.000")
# …recorded the way `cmd_say` records it: the item id and the say id, no conversation.
S.record_delegation(item=_r_src["id"], role="staffai", outcome="reported", say="say-rep1",
                    report="2 of 3 have replied")

_later = {"id": "CREP-999.000", "channel": "CREP", "channel_type": "channel", "sender_name": "Sri",
          "sender": "U9", "text": "any more replies from Danielle, Kerry or Ryan?",
          "created": S.now_iso(), "status": "queued", "reply_thread_ts": "999.000", "ts": "999.000",
          "triage": None, "context": "(none)"}
_rep = S.channel_reports(_later)
check("a report that came back is carried to the next message in that channel",
      len(_rep) == 1 and "Danielle this AM" in _rep[0]["answer"])
check("and the writer is told nobody has seen it yet",
      "NOT seen it" in _rep[0]["state"])
check("the question it answered comes with it, so a new ask can be narrower",
      "Adair" in _rep[0]["task"])
check("a thread-keyed dedupe would have missed it — this is a different conversation",
      S.open_dispatch(_later, 6) is None and S.session_key(_later) != "CREP:900.000")
check("it reaches the writer's prompt, under a heading that says what it is",
      "already been asked in this channel" in "\n".join(S.render_reports(_rep)))
check("nothing crosses channels — pick_channel already decided which room may hear it",
      S.channel_reports(_later | {"channel": "CELSE"}) == [])

_r_say2 = dict(_r_say, id="say-rep1")
S.save_item({**_r_say, "outcome": {"status": "sent_from_draft", "at": S.now_iso()}})
check("once it has gone out the writer is told that instead",
      "already been told" in S.channel_reports(_later)[0]["state"])

S.record_delegation(item="CREP-998.000", peer="p", role="staffai", outcome="dispatched",
                    task="transcripts for the uncovered scenarios", conversation="CREP:998.000")
_out = [r for r in S.channel_reports(_later, limit=5) if r["task"].startswith("transcripts")]
check("a question still out is shown as still out, so it is not asked twice",
      len(_out) == 1 and "still out" in _out[0]["state"] and not _out[0].get("answer"))

# The 16 Oct Adair dispatch still had no return row on the 19th. Reading that as "an answer is
# coming" would park the question forever on a run that died three days ago.
_old_at = (S.datetime.now() - S.timedelta(hours=48)).isoformat(timespec="seconds")
S.record_delegation(item="CREP-997.000", peer="p", role="staffai", outcome="dispatched",
                    task="a lookup nothing ever answered", conversation="CREP:997.000")
import json as _json
_rows = [_json.loads(l) for l in S.DELEGATIONS.read_text().splitlines() if l.strip()]
for _r in _rows:
    if _r.get("item") == "CREP-997.000":
        _r["at"] = _old_at
S.DELEGATIONS.write_text("".join(_json.dumps(_r) + "\n" for _r in _rows))
_dead = [r for r in S.channel_reports(_later, limit=9) if r["task"].startswith("a lookup nothing")]
check("a dispatch that never came back is not reported as an answer on its way",
      len(_dead) == 1 and "NOT covered" in _dead[0]["state"])
check("under seal the writer sees none of it — a report is future information in a rehearsal",
      "already been asked in this channel" not in S.render_item({**_later, "sealed": True}, d.ledger, set()))

_refused = _ignore_a_mention()
check("the writer cannot answer a mention with silence", "not available here" in _refused)
check("and is told what to do instead", "ack" in _refused and "emoji" in _refused)

S.execute_decision(d, S.load_item(iid3), {"action": "ack", "text": ""}, d.ledger, d.followups, set(), state=d.state)
check("ack → 👀 reaction", fc.reactions[-1][2] == "eyes")
S.execute_decision(d, S.load_item(iid3), {"action": "escalate", "text": "let me come back to you", "note": "money"}, d.ledger, d.followups, set(), state=d.state)
check("escalate → self-DM alert, never sent", fc.posted[-1][0] == "DSELF" and "Needs you" in fc.posted[-1][1])

for i in range(13):
    d.ledger.record(item={"id": f"x{i}", "sender": U, "sender_name": "Priya", "channel": D, "channel_type": "im", "decision": {"action": "reply"}}, outcome="approved")
check("trust: reply earns auto with Priya", d.ledger.effective_mode("reply", U, D, "learn")[0] == "auto")
check("trust: Ana still draft", d.ledger.effective_mode("reply", U2, D2, "learn")[0] == "draft")
check("trust: escalate never auto", d.ledger.effective_mode("escalate", U, D, "auto")[0] == "draft")

iid4 = msg(d, U, D, "1700000006.000", "send me the latest numbers?")
s = S.execute_decision(d, S.load_item(iid4), {"action": "reply", "text": "q3: https://x.io/q3"}, d.ledger, d.followups, set(), observed_tools=["Read", "mcp__notion__search"], state=d.state)
check("share gate: context-backed reply to untrusted → draft, sources inferred", "drafted" in s and S.load_item(iid4)["decision"]["sources"] == ["notion", "read"])
s = S.execute_decision(d, S.load_item(iid4), {"action": "reply", "text": "sure, 3 works"}, d.ledger, d.followups, set(), observed_tools=[], state=d.state)
check("earned auto + no context → sent", s.startswith("sent"))
d.tick()
check("tick: aged auto-send scored clean", d.ledger.rows[-1]["outcome"] == "clean")

# ----------------------------------------------------------- recall/dossier
check("resolve @handle / #channel / DM id", recall.resolve_target(d, "@priya") == ("people", U, "Priya") and recall.resolve_target(d, "#growth")[1] == C and recall.resolve_target(d, D)[1] == U)
recall.note("people", U, "Priya (@priya)", "'the deck' = Series A deck (Drive)")
txt = S.render_item(S.load_item(iid4), d.ledger, set(), d.followups)
check("show prints dossier + recall hints + track record", "Series A deck" in txt and f"history {D}" in txt and "track record with them" in txt)
h = recall.history(d, D, None, 10); sr = recall.search(d, "deck", with_user=U); ab = recall.about(d, U, d.ledger, d.followups, 3)
check("history", "send me the latest numbers" in h, h[:200])
check("search", "series A deck v3" in sr, sr[:200])
check("about", "Head of Growth" in ab, ab[:200])

# -------------------------------------------------------------- dispatch
cmd, env, cwd = S.claude_command(S.load_item(iid4), ("abc", False))
check("claude cmd: dontAsk, read scoped, api key hidden", "--permission-mode" in cmd and "dontAsk" in cmd and any(a.startswith("Read(//") for a in cmd[cmd.index("--allowedTools") + 1].split(",")) and "ANTHROPIC_API_KEY" not in env and "--session-id" in cmd)
d.dispatch = "claude"
iid5 = msg(d, U, D, "1700000007.000", "latest deck link?")
it5 = S.load_item(iid5)
check("dispatch: fake claude decided, tools captured, session new", it5["decision"]["action"] == "reply" and "mcp__drive__search" in it5["run"]["tools"] and it5["session"]["resumed"] is False)
check("dispatch: share gate caught drive use → draft", fc.posted[-1][0] == "DSELF")
iid6 = msg(d, U, D, "1700000008.000", "and the one-pager?")
check("session continuity: second message resumes", S.load_item(iid6)["session"] == {"id": it5["session"]["id"], "resumed": True})
os.environ["FAKE_FAIL_RESUME"] = "1"
iid7 = msg(d, U, D, "1700000009.000", "ping")
os.environ.pop("FAKE_FAIL_RESUME")
check("failed resume → fresh session, still decided", S.load_item(iid7)["session"]["resumed"] is False and S.load_item(iid7)["decision"]["action"] == "reply")

# hold → approve → follow-up tracked
iid8 = msg(d, U2, D2, "1700000010.000", "and the other PR?")
S.execute_decision(d, S.load_item(iid8), {"action": "hold", "text": "by end of day", "urgency": 3}, d.ledger, d.followups, set(), state=d.state)
dts = fc.posted[-1][3]
d.on_event({"type": "reaction_added", "user": "UME", "reaction": "+1", "item": {"type": "message", "channel": "DSELF", "ts": dts}})
check("approved hold → promise tracked", any(f.counterpart == U2 for f in d.followups.open()))

# ------------------------------------------------------------ CLI (subprocess, offline)
def ctl(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "slackctl.py"), *args], capture_output=True, text=True, env=dict(os.environ))

out = ctl("simulate", "can you send me the deck?", "--from", "Priya")
sim = next(l for l in out.stdout.splitlines() if l.startswith("=== inbox item sim-")).split()[3]
out = ctl("act", sim, "--action", "reply", "--text", "here: https://x.io/deck", "--sources", "drive")
check("simulate + act never sends", "[simulated] reply → would DRAFT" in out.stdout)
out = ctl("deadline", "--urgency", "4"); check("deadline cli", out.returncode == 0 and "\t" in out.stdout)
out = ctl("note", U, "prefers short answers"); check("note cli (offline id)", out.returncode == 0)
out = ctl("review-data", "--days", "1"); check("review-data cli", "Recent corrections" in out.stdout and "dossiers" in out.stdout)
out = ctl("doctor"); check("doctor exits 1 with clear FAILs when unconfigured", out.returncode == 1 and "SLACK_USER_TOKEN" in out.stdout)

# ------------------------------------------------------------------- pause
# Pause lives in state.json, not .env, so a running daemon honours it without a restart.
_st = S.State()
_st.clear_pause()
check("not paused by default", _st.pause_info() is None)
_st.set_pause(None, "on a call")
check("pause with no end stays paused", (_st.pause_info() or {}).get("reason") == "on a call")
_st.set_pause("2000-01-01T00:00:00", "stale")
check("an expired pause lifts itself", _st.pause_info() is None)
_st.set_pause((datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"), "")
check("a future pause is still in force", _st.pause_info() is not None)
_st.clear_pause()
check("resume clears the pause", _st.pause_info() is None)
check("duration parsing", S.parse_duration("90m") == 5400 and S.parse_duration("2h") == 7200
      and S.parse_duration("1d") == 86400)
try:
    S.parse_duration("banana"); _bad = False
except ValueError:
    _bad = True
check("a bad duration is rejected, not silently ignored", _bad)
out = ctl("pause", "--for", "2h", "--reason", "testing")
check("pause cli", out.returncode == 0 and "paused until" in out.stdout)
out = ctl("resume")
check("resume cli", out.returncode == 0 and "resumed" in out.stdout.lower())


# An emoji is often the most in-voice reply there is, so Solo picks which one — 👀 is only the
# fallback. And threading is a per-message choice, not a property of the channel type.
out = ctl("act", sim, "--action", "ack", "--emoji", "tada")
check("act accepts a chosen emoji", out.returncode == 0)
out = ctl("act", sim, "--action", "ack", "--emoji", "\U0001F389")
check("act rejects a raw emoji character, asking for the name",
      out.returncode != 0 and "name" in (out.stdout + out.stderr).lower())

iid_e = msg(d, U, D, "3000.000100", "shipped it")
S.execute_decision(d, S.load_item(iid_e), {"action": "ack", "text": "", "emoji": "tada"},
                   d.ledger, d.followups, set(), state=d.state)
check("ack leaves the emoji Solo chose", fc.reactions[-1][2] == "tada")

iid_t = msg(d, U, D, "3000.000200", "two questions for you")
before = len(fc.posted)
S.execute_decision(d, S.load_item(iid_t), {"action": "reply", "text": "answering here", "thread": True},
                   d.ledger, d.followups, set(), state=d.state)
check("--thread replies in a thread on their message",
      any(th == "3000.000200" for _c, _x, th, _ts in fc.posted[before:]))

# Other agents reach Solo through `say`, so one thing speaks as the owner. Whatever the ledger
# says, an agent-requested message waits for the owner — the ledger graduates what a PERSON earned.
def _agent_origin_never_auto_sends():
    iid = msg(d, U, D, "3000.000400", "placeholder")
    it = S.load_item(iid); it["origin"] = "agent:scout"; S.save_item(it)
    # Priya has earned auto by this point in the suite; the agent origin must override that
    S.execute_decision(d, S.load_item(iid), {"action": "reply", "text": "from the scout"},
                       d.ledger, d.followups, set(), state=d.state)
    pol_ = S.load_item(iid).get("policy")
    # "draft" is the mode in which nothing auto-sends, whatever the sender has earned
    return pol_ and pol_["effective"] == "draft" and "agent:scout" in pol_["reason"]

check("an agent-requested message always waits for the owner", _agent_origin_never_auto_sends())

# Routing must fail toward asking. Learned keywords alone put a Facebook-accounts report in
# #engineering-team at 0.60 — above a 0.5 bar — so the bar is 0.70 and the owner's `For:` line
# is what actually makes it reliable (the same three reports went to 1.00 once it was filled).
check("the channel bar is high enough to refuse a confident-looking wrong answer",
      S.CHANNEL_MIN_CONFIDENCE >= 0.7)

def _catalogue_prefers_the_owners_line():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "C1.md").write_text("# #acme (C1)\n\n**For:** everything about the Acme account\n"
                             "**Recurring terms:** noise, noise, noise\n")
    import recall as _r
    old_dir = _r.CHANNELS_DIR
    _r.CHANNELS_DIR = d
    try:
        desc = S.channel_catalogue()["C1"]
        # the owner's line leads; the scraped keywords trail behind it
        return desc.startswith("acme. everything about the Acme account") and "noise" in desc
    finally:
        _r.CHANNELS_DIR = old_dir

check("a channel's For: line drives routing, not the scraped keywords", _catalogue_prefers_the_owners_line())

# Routing must not file a report in a room nobody reads, or a temporary one that gets archived.
# #dev was dormant 148 days and still took "production is throwing errors" at 1.00 confidence.
def _routable_excludes_dead_and_temporary():
    import tempfile
    d = Path(tempfile.mkdtemp())
    (d / "LIVE.md").write_text("# #live (LIVE)\n\n**For:** the live one\n"
                               "**Activity:** active — last message 1d ago\n**Temporary:** no\n")
    (d / "DEAD.md").write_text("# #dead (DEAD)\n\n**For:** nobody reads this\n"
                               "**Activity:** dormant — last message 148d ago\n**Temporary:** no\n")
    (d / "TMP.md").write_text("# #tmp-thing (TMP)\n\n**For:** one-off\n"
                              "**Activity:** active — last message 0d ago\n**Temporary:** yes — spun up\n")
    import recall as _r
    old_dir = _r.CHANNELS_DIR
    _r.CHANNELS_DIR = d
    try:
        allc, routable = S.channel_catalogue(), S.channel_catalogue(routable_only=True)
        return len(allc) == 3 and set(routable) == {"LIVE"}
    finally:
        _r.CHANNELS_DIR = old_dir

check("dormant and temporary channels are not routing targets", _routable_excludes_dead_and_temporary())

# A refresh re-learns the traffic but must never clobber the owner's For: line — losing it
# silently drops routing back to keyword guessing, which was measured wrong.
def _refresh_keeps_the_owners_line():
    src = (ROOT / "scripts" / "slackctl.py").read_text()
    i = src.index("def cmd_learn_channels")
    body = src[i:src.index("def channel_meta")]
    return "existing_for" in body and body.index("existing_for or purpose") > body.index('if l.startswith("**For:**")')

check("re-learning a channel preserves the owner's For: line", _refresh_keeps_the_owners_line())

# Replies go back where the message came from. Routing exists only for agent-originated reports;
# an inbound reply must never be re-placed into a channel a model picked.
def _replies_are_never_rerouted():
    src = (ROOT / "scripts" / "slackctl.py").read_text()
    for fn in ("def on_inbound", "def execute_decision", "def approve_draft"):
        i = src.index(fn)
        body = src[i:i + 4000]
        if "pick_channel" in body:
            return False
    return True

check("a reply always goes back to the channel it arrived in", _replies_are_never_rerouted())

# The app now subscribes to message.channels/message.groups, so Slack streams every channel
# message. Acting only on the ones that tag the owner is entirely this guard's job — untested
# until now, and the whole reason subscribing to channels is affordable.
def _channel_guard(text, ctype="channel"):
    before = len(S.list_items())
    msg(d, U, C, f"{time.time():.6f}", text, ctype=ctype)
    return len(S.list_items()) > before

check("a channel message that tags you is picked up",
      _channel_guard("<@UME> can you check the zip code file?"))
check("a channel message that does not tag you is ignored",
      not _channel_guard("deploy finished, all green"))
check("a private group message that tags you is picked up",
      _channel_guard("<@UME> need you on this", ctype="group"))

# Claude Code matches a Bash prefix as `Bash(cmd:*)`. The space form silently grants nothing and
# the agent just reports "Bash denied" — which is exactly how `ask` failed, and why the daemon's
# own allowlist was quietly wrong too.
check("bash permission rules use the colon prefix form",
      any(r.endswith(":*)") for r in S.bash_rules()))
check("every bash rule is scoped to this CLI, never bare python3",
      all("slackctl.py" in r for r in S.bash_rules()))

# `ask` is the read half of the agent interface and must not be able to send.
def _ask_is_read_only():
    src = (ROOT / "scripts" / "slackctl.py").read_text()
    body = src[src.index("def cmd_ask"):src.index("def cmd_simulate")]
    return ("AUTOREPLY_DISPATCH" in body                 # sending subcommands refuse
            and "Write,Edit" in body                      # file writes denied
            and "stdin=subprocess.DEVNULL" in body        # or the allowlist does not take
            and "--strict-mcp-config" in body             # the install's own MCP servers are not its
            and "ToolSearch" in body                      # nor is anything it could search up
            and "chat_postMessage" not in body)

check("ask cannot send anything", _ask_is_read_only())

# Typing to Solo in its own DM is an instruction to Solo — never something to reply to, and never
# material to learn the owner's voice from.
def _console(text):
    d.bot = FakeClient(); d.bot_dm = "DBOT"
    before = len(voice.load_samples())
    d.on_event({"type": "message", "user": "UME", "channel": "DBOT", "channel_type": "im",
                "ts": f"{time.time():.6f}", "text": text})
    answers = [p[1] for p in d.bot.posted]
    return (answers[-1] if answers else ""), len(voice.load_samples()) == before

ans, no_sample = _console("status")
check("`status` in the console answers", "MODE=" in ans)
check("console chatter is never learned as the owner's voice", no_sample)
ans, _ = _console("pause 90m")
check("`pause 90m` pauses from Slack", "paused until" in ans and d.state.pause_info() is not None)
ans, _ = _console("resume")
check("`resume` lifts it from Slack", "resumed" in ans and d.state.pause_info() is None)
ans, _ = _console("pause banana")
check("a bad duration is explained, not swallowed", "duration" in ans.lower())
ans, _ = _console("too formal — keep it shorter with Sri")
check("free text is kept as feedback for review", "noted" in ans.lower()
      and "shorter with Sri" in (S.config.p("console_feedback.jsonl").read_text()))

# "approve everything" has to include the reaction, or the one unapproved thing is the one that
# goes wrong. ignore stays automatic because it sends nothing at all.
check("MODE=draft makes even an ack wait",
      d.ledger.effective_mode("ack", U, D, "draft")[0] == "draft")
check("MODE=learn leaves a bare reaction automatic",
      d.ledger.effective_mode("ack", U, D, "learn")[0] == "auto")
check("ignore is automatic in every mode — it sends nothing",
      d.ledger.effective_mode("ignore", U, D, "draft")[0] == "auto")

check("without --thread a DM reply stays flat",
      (lambda i: (S.execute_decision(d, S.load_item(i), {"action": "reply", "text": "flat"},
                                     d.ledger, d.followups, set(), state=d.state),
                  fc.posted[-1][2] is None)[1])(msg(d, U, D, "3000.000300", "one question")))

# Handing work to an agent is the one place a Slack message could start something on the owner's
# machines, so it is gated the same way sending is: Solo proposes, the owner's 🤖 dispatches, and
# the record of both is what a later promotion to automatic would have to rest on.
def _writer_cannot_dispatch():
    i = msg(d, U, D, "3000.000500", "can you restart the warmup job")
    it = S.load_item(i); it["decision"] = {"action": "escalate", "task": "find out why it stalled", "role": "r"}
    os.environ["AUTOREPLY_DISPATCH"] = "1"
    try:
        return S.dispatch_task(it, "Sri", "a DM")
    finally:
        os.environ.pop("AUTOREPLY_DISPATCH", None)


def _proposal_is_shown_with_how_to_act():
    i = msg(d, U, D, "3000.000501", "why did the overnight run stall?")
    S.execute_decision(d, S.load_item(i), {"action": "escalate", "text": "Checking",
                                           "task": "read last night's run logs", "role": "scout"},
                       d.ledger, d.followups, set(), state=d.state)
    return next((p[1] for p in d.bot.posted if "Proposed for an agent" in (p[1] or "")), "")


check("the drafting agent cannot hand itself work",
      "refused" in _writer_cannot_dispatch())
check("a proposal with no task dispatches nothing",
      "nothing proposed" in S.dispatch_task({"id": "x", "decision": {}, "text": ""}, "Sri", "a DM"))
check("a proposal with nowhere to send refuses rather than guessing a machine",
      "no peer" in S.dispatch_task({"id": "x", "decision": {"task": "look"}, "text": ""}, "Sri", "a DM"))
_shown = _proposal_is_shown_with_how_to_act()
check("the owner sees the proposed investigation", "read last night's run logs" in _shown)
check("the owner is told the reaction that hands it over", "react 🤖" in _shown)
check("the owner is told the agent only reads", "read-only" in _shown)

# A report has to come back to the conversation that asked. Without an id in the brief the agent
# has nothing to put in `--to`, so every report fell through to channel guessing — which is how a
# question asked in a DM gets answered in a room. `--re` carries the request's id both ways.
def _say_args(intent, **kw):
    return SimpleNamespace(intent=intent, target=kw.get("to"), from_=kw.get("frm", "scout"),
                           why=kw.get("why"), thread_ts=None, re_item=kw.get("re"))


def _refusal(**kw):
    try:
        S.cmd_say(_say_args(kw.pop("intent", "the run finished clean"), **kw))
    except SystemExit as e:
        return str(e)
    return ""


_src = msg(d, U, D, "3000.000600", "did last night's RH-1 run finish?")
_brief_item = S.load_item(_src)
_brief_item["decision"] = {"action": "escalate", "task": "read the RH-1 run log", "role": "scout"}
os.environ["DELEGATE_PEER"] = "somepeer"
S.CFG["delegate_peer"] = "somepeer"
S.CFG["claudectl_bin"] = "/nonexistent-claudectl"
_failed = S.dispatch_task(_brief_item, "Priya", "a DM")
check("a dispatch that cannot reach claudectl says so rather than claiming success",
      "could not reach claudectl" in _failed)

check("the brief tells the agent the id to report against",
      "--re {item_id}" in S.AGENT_BRIEF and "--re " in S.AGENT_BRIEF.format(
          owner="o", task="t", who="w", where="x", quoted="q", role="r", item_id=_src))
check("the id in the brief is the request's own id",
      _src in S.AGENT_BRIEF.format(owner="o", task="t", who="w", where="x", quoted="q",
                                   role="r", item_id=_src))

_dm_target = S.report_target(S.load_item(_src))
check("a report on a DM goes back to that DM",
      _dm_target["channel"] == D and _dm_target["sender"] == U)
check("a DM with no thread is answered flat, like any other reply",
      _dm_target["reply_thread_ts"] is None)
_in_thread = S.report_target({"channel": C, "channel_type": "channel", "channel_name": "growth",
                              "sender": U, "sender_name": "Priya", "reply_thread_ts": "3000.000100"})
check("a report on a channel message goes back to that channel",
      _in_thread["channel"] == C and _in_thread["channel_type"] == "channel")
check("a question asked in a thread is answered in that thread",
      _in_thread["reply_thread_ts"] == "3000.000100")

check("--re and --to together is refused, not silently resolved one way",
      "not both" in _refusal(re=_src, to="@priya"))
check("an unknown --re id is refused", "no such request" in _refusal(re="say-nope"))
check("an agent cannot --re work the owner never handed over",
      "nothing was dispatched" in _refusal(re=_src))

S.record_delegation(item=_src, peer="somepeer", role="scout", task="read the RH-1 run log",
                    outcome="dispatched", counterpart="Priya")
S.cmd_say(_say_args("the RH-1 run finished at 02:14, no timeouts", re=_src))
_rows = [json.loads(l) for l in S.DELEGATIONS.read_text().splitlines() if l.strip()]
# Scoped to this item: other checks in this file legitimately close loops of their own.
_reported = [r for r in _rows if r.get("outcome") == "reported" and r.get("item") == _src]
check("a report closes the loop in the record", len(_reported) == 1 and _reported[0]["item"] == _src)
_say = S.load_item(_reported[0]["say"])
check("the report is addressed to the conversation that asked, not a guessed channel",
      _say["channel"] == D and _say["sender"] == U)
check("the report carries the id of the request it answers", _say.get("re") == _src)
check("the writer is told what was originally asked",
      "did last night's RH-1 run finish?" in _say["context"])
# The owner's standing rule: nothing an agent causes goes out without their approval, whatever
# MODE says and whatever the ledger has learned. This is that rule, as a test.
check("an agent's report still waits for the owner's ✅",
      _say["policy"]["effective"] == "draft")
check("a report is threaded against the message it answers, not a local clock reading",
      _say["ts"] == S.load_item(_src)["ts"])

# `reported back` is the one state this whole round trip exists to state truthfully. Written when
# the report arrives rather than when a draft exists, it would say "came back" in precisely the
# case worth catching — a compose that failed. Today's weekly rate limit does exactly this.
def _report_with_no_draft():
    _src2 = msg(d, U, D, "3000.000602", "and did UC-2 finish too?")
    S.record_delegation(item=_src2, peer="somepeer", role="scout", task="read the UC-2 log",
                        outcome="dispatched", counterpart="Priya")
    os.environ["CLAUDE_BIN"] = "/nonexistent-claude"
    S.CFG["claude_bin"] = "/nonexistent-claude"
    try:
        S.cmd_say(_say_args("UC-2 finished at 03:40", re=_src2))
    except SystemExit:
        pass
    finally:
        os.environ["CLAUDE_BIN"] = str(FAKE_CLAUDE)
        S.CFG["claude_bin"] = str(FAKE_CLAUDE)
    rows = [json.loads(l) for l in S.DELEGATIONS.read_text().splitlines() if l.strip()]
    return [r for r in rows if r.get("item") == _src2]


_no_draft = _report_with_no_draft()
check("a report Solo could not compose is not recorded as having come back",
      not any(r.get("outcome") == "reported" for r in _no_draft))
check("and the failure is recorded, rather than the task just going quiet",
      any(r.get("outcome") == "report_failed" for r in _no_draft))

# The first report ever to come back through this loop was refused at 3.5/5 for naming HubSpot
# contact record ids — correctly, and then silently. It survived only in a relay task record the
# owner has no reason to open, while `delegations` read "handed over, nothing back yet". Declining
# to pick a room is the gate working; losing the report is not.
def _withheld():
    src3 = msg(d, U, D, "3000.000603", "did the testers ever reply?")
    S.record_delegation(item=src3, peer="somepeer", role="scout", task="check the threads",
                        outcome="dispatched", counterpart="Priya")
    real, real_console = S.sensitivity_of, S.Slack.console_post
    seen = []
    S.sensitivity_of = lambda text: 4.2
    S.Slack.console_post = lambda self, text: (seen.append(text), "1.0")[1]
    try:
        S.cmd_say(_say_args("Danielle's record 549576801980 is opted out", re=src3))
    except SystemExit as e:
        err = str(e)
    finally:
        S.sensitivity_of, S.Slack.console_post = real, real_console
    rows = [json.loads(l) for l in S.DELEGATIONS.read_text().splitlines() if l.strip()]
    return err, [r for r in rows if r.get("item") == src3], seen


_err, _refused_rows, _console = _withheld()
check("a report Solo will not place still reaches the owner",
      "in your Solo DM" in _err and "nothing was sent" in _err)
check("the withheld report is recorded as refused, not as never having come back",
      any(r.get("outcome") == "report_refused" for r in _refused_rows))
check("a withheld report is never recorded as reported back",
      not any(r.get("outcome") == "reported" for r in _refused_rows))
check("the owner is handed the text itself, not just told one exists",
      any("549576801980" in t for t in _console))
check("and is told plainly that nobody else received it",
      any("nothing went to anyone" in t for t in _console))

# Inbound <@U123> is flattened to "@Sri" for the writer to read, so a draft that mirrored the name
# it had just read went out as literal grey text that pinged nobody. Slack renders a mention only
# from <@ID> and will not linkify a bare @name posted through the API.
_names = {"sri": "U0SRI", "srivatsan": "U0SRI", "nick": "U0NICK"}
_look = lambda n: _names.get(n.lower())

check("a bare @name is restored to a real mention",
      S.restore_mentions("@Sri on the 3", _look) == "<@U0SRI> on the 3")
check("an email address is not mistaken for a mention",
      S.restore_mentions("mail me at barada@kubric.io", _look) == "mail me at barada@kubric.io")
check("an @name inside a URL is left alone",
      S.restore_mentions("see https://x.com/@sri", _look) == "see https://x.com/@sri")
check("a possessive keeps its apostrophe",
      S.restore_mentions("ask @Sri's team", _look) == "ask <@U0SRI>'s team")
check("a name nobody answers to stays plain text",
      S.restore_mentions("@Nobody there", _look) == "@Nobody there")
check("@here is left inert rather than made to blast",
      S.restore_mentions("@here please", _look) == "@here please")
check("an already-formed mention is untouched",
      S.restore_mentions("already <@U0SRI> tagged", _look) == "already <@U0SRI> tagged")

# The guard above only catches an option shipped undocumented. It would not have caught this one
# once the skill mentioned it — "the writer can find it and still never uses it" is not a thing a
# unit test can see. That needs a number from live traffic: of the moments where a proposal was the
# right move, how many were taken. Zero proposals is fine when nothing warranted one; zero out of
# twelve is the alarm.
def _uptake(cases):
    import importlib
    items = TMP / "uptake"
    shutil.rmtree(items, ignore_errors=True)
    items.mkdir(parents=True, exist_ok=True)
    for n, (action, can_answer, task, *topic) in enumerate(cases):
        (items / f"i{n}.json").write_text(json.dumps({
            "id": f"i{n}", "created": datetime.now().isoformat(timespec="seconds"),
            "decision": {"action": action, "task": task},
            "triage": {"can_answer": can_answer, "topic": topic[0] if topic else "action_request"}}),
            encoding="utf-8")
    real = S.config.p
    S.config.p = lambda name: items if name == "inbox" else real(name)
    try:
        return S.proposal_uptake(14)
    finally:
        S.config.p = real


_none = _uptake([("hold", 0.2, ""), ("escalate", 0.1, ""), ("hold", 0.9, "")])
check("a chance missed every time is called out, not reported as a clean zero",
      "0/2" in _none and "never once" in _none, _none)
_some = _uptake([("hold", 0.2, "go read the logs"), ("escalate", 0.1, "")])
check("a proposal that was made counts as a chance taken",
      "1/2" in _some and "never once" not in _some, _some)
check("a message the writer could answer itself is not counted as a chance",
      "0/2" in _uptake([("hold", 0.2, ""), ("hold", 0.1, ""), ("reply", 0.1, "")]))
check("a topic no lookup could settle is not a chance \u2014 an apology is not agent work",
      "0/1" in _uptake([("hold", 0.2, "", "action_request"), ("escalate", 0.1, "", "problem_report")]))
check("credentials are never an agent's, however unanswerable",
      "0/1" in _uptake([("hold", 0.2, "", "status_request"), ("escalate", 0.1, "", "credentials_access")]))
check("a proposal that was made counts even on an off-list topic",
      "1/1" in _uptake([("escalate", 0.1, "go read the logs", "problem_report")]))

# Three bugs in two days shared one shape: the mechanism worked, and whoever had to use it never
# met it. A `hold` note rendered only on the escalate branch, so the owner never read it. `⏎` sat
# in the worked examples with no rule and no way to send two messages. `--task` lived only in
# `act --help`, which the writer has no reason to run. Each had a test for the part that worked
# and none for "can the actor find this" — so each shipped green and did nothing.
#
# This is the standing guard for the writer's half: every option it may pass to `act` has to be
# named in the skill it actually follows, or listed below with the reason it is not the writer's
# to reach for. Adding one without doing either fails here, at commit time.
import re as _re

_ACT_NOT_THE_WRITERS = {
    "--help": "argparse's own",
    "--text-file": "the daemon's path for text too long to pass inline; the writer uses --text",
    "--urgency": "comes from triage, never chosen by the writer",
    "--role": "only meaningful beside --task, which the skill does document; defaults to DELEGATE_ROLE",
}
_act_help = subprocess.run([sys.executable, str(SCRIPTS / "slackctl.py"), "act", "--help"],
                           capture_output=True, text=True, env=dict(os.environ)).stdout
_act_opts = set(_re.findall(r"(--[a-z][a-z-]+)", _act_help))
_skill_md = (ROOT / "SKILL.md").read_text(encoding="utf-8")
_unreachable = sorted(o for o in _act_opts if o not in _ACT_NOT_THE_WRITERS and o not in _skill_md)

check("the act options are discoverable at all", len(_act_opts) >= 8, f"parsed: {sorted(_act_opts)}")
check("every act option the writer may use is named in the skill it follows",
      not _unreachable, f"reachable only from --help: {_unreachable}")

# Same guard on the other side of the interface. `say` is read by a coding agent following
# skills/solo/SKILL.md, and an option that exists only in `--help` is an option no agent will ever
# pass — `--re` is the whole round trip, so it failing this check would mean reports keep getting
# routed by guesswork while the flag sits there working.
_SAY_NOT_THE_AGENTS = {
    "--help": "argparse's own",
    "--to": "withheld on purpose — the skill tells agents not to pick a room; --re or Solo decides",
    "--thread-ts": "the owner's and the daemon's; an agent has no thread ts to pass",
    "--why": "optional colour for the owner; documented in the brief, not worth skill space",
}
_say_help = subprocess.run([sys.executable, str(SCRIPTS / "slackctl.py"), "say", "--help"],
                           capture_output=True, text=True, env=dict(os.environ)).stdout
_say_opts = set(_re.findall(r"(--[a-z][a-z-]+)", _say_help))
_agent_skill = (ROOT / "skills" / "solo" / "SKILL.md").read_text(encoding="utf-8")
_agent_unreachable = sorted(o for o in _say_opts if o not in _SAY_NOT_THE_AGENTS and o not in _agent_skill)
check("the say options are discoverable at all", len(_say_opts) >= 5, f"parsed: {sorted(_say_opts)}")
check("every say option an agent may pass is named in the skill agents follow",
      not _agent_unreachable, f"reachable only from --help: {_agent_unreachable}")

# `--task` existed, was wired, and rendered on the escalation — and fired zero times in a day of
# real traffic. It appeared only in `act --help`, which the writer has no reason to run: not in the
# route menu it follows, and not in what `show` prints at decision time. Measured on the real
# writer: given "send me the list of appointments", it reasoned "I don't have access here, so
# you'll need to pull the list yourself" and still never proposed one. Same dead-channel shape as
# the hold note — an instruction pointing somewhere nobody reads.
import learning as _learning


class _FakeLedger:
    def stats(self, **kw):
        return _learning.Stats()

    def corrections(self, limit=50):
        return []

    def share_ok(self, sender):
        return False

    def effective_mode(self, route, sender, channel, base_mode):
        return "draft", "test"


def _render(can_answer):
    item = {"id": "t1", "sender": "U1", "sender_name": "Sri", "sender_handle": "sri", "text": "send me the list",
            "channel": "C1", "channel_type": "channel", "channel_name": "#x", "created": "now", "context": "",
            "triage": {"source": "test", "route": "hold", "confidence": 0.6, "topic": "document_request",
                       "sensitivity": 2.0, "urgency": 3.0, "needs_context": 0.9, "asks_for_content": 0.9,
                       "can_answer": can_answer}}
    return S.render_item(item, _FakeLedger(), set())


check("the writer is offered a proposal exactly where it is told it lacks the answer",
      "--task" in _render(0.25))

# Solo's read of the owner's projects was removed — the agents hold that access — and
# --strict-mcp-config means an unlisted server is not merely unapproved, it is absent. The skill
# was still telling the writer to search "the directories you were given" and "any mcp__* tools you
# have". An instruction to look somewhere unreachable yields a guess or a bare hold, so the reach is
# computed from the same config that builds the invocation and printed where the decision is made.
_reach = _render(0.9)
check("the writer is told what it can actually reach", "what you can reach" in _reach)
check("and told plainly when it has no connected service",
      "none — you have no connected service" in _reach or "services:" in _reach)
check("and told that anything beyond it belongs to an agent",
      "belongs to an agent, not to you" in _reach)
check("the reach is read off the same config that builds the invocation",
      all(d.strip() in _reach for d in S.CFG["context_dirs"] if d.strip()))
_skill_md_now = (ROOT / "SKILL.md").read_text(encoding="utf-8")
check("the writer is told that being denied is not the same as finding nothing",
      "Being denied is not the same as finding nothing" in _skill_md_now)
check("it is not offered when the writer can answer unaided",
      "--task" not in _render(0.9))

# `replaced` meant "a draft was pending and you replied yourself", and always scored bad. But the
# owner answering first is the normal case for a present owner, and 4 of 15 replacements were a
# correct bare hold they simply beat. Counting those against Solo meant no route could graduate
# while the owner was active — the metric punished Solo for the owner being responsive.
import learning as _L

check("a bare hold the owner beat is not scored against Solo",
      _L.is_bare_hold("Checking") and _L.is_bare_hold("Yeah am on it") and _L.is_bare_hold("Ok"))
check("a hold that invented a time is not bare — that one really was replaced",
      not _L.is_bare_hold("Checking - will update by 11am"))
check("a draft with substance is not a hold",
      not _L.is_bare_hold("Yes please") and not _L.is_bare_hold(""))
check("overtaken is neutral: it neither rewards nor punishes",
      "overtaken" not in _L.GOOD and "overtaken" not in _L.BAD)


def _overtaken_is_excluded():
    led = _L.Ledger.__new__(_L.Ledger)
    led.rows = [{"route": "reply", "sender": "U1", "channel": None, "good": None, "outcome": "overtaken"},
                {"route": "reply", "sender": "U1", "channel": None, "good": True, "outcome": "approved"}]
    st = led.stats(route="reply", sender="U1")
    return st.n == 1 and st.good == 1


check("an overtaken draft does not drag the trust score down",
      _overtaken_is_excluded())

# The owner sends in beats — 53% of 305 real messages are part of a burst, median 14s apart, most
# often a short beat then the substance ("Yes please" / "go ahead"). The writer could only ever
# produce one message, so that shape was unreachable no matter what VOICE.md said.
class _Rec:
    def __init__(self):
        self.sent = []

    def chat_postMessage(self, channel, text, thread_ts=None):
        self.sent.append((channel, text, thread_ts))
        return {"ts": "%d.0" % len(self.sent)}


class _Poster:
    def __init__(self):
        self.client = _Rec()
        self.self_dm = "D0OWNER"
        self.mention_id = lambda n: None


_p = _Poster()
_first = S.Slack.post(_p, "C1", "Yes please \u23ce go ahead", None)
check("a beat marker sends two messages, in order",
      [t for _, t, _ in _p.client.sent] == ["Yes please", "go ahead"])
check("both beats land in the same thread",
      {th for _, _, th in _p.client.sent} == {None})
check("the first beat's ts is what comes back, so outcomes anchor on it",
      _first == "1.0")

_p2 = _Poster()
S.Slack.post(_p2, "C1", "just the one thing", None)
check("a draft with no beat marker is still a single message",
      [t for _, t, _ in _p2.client.sent] == ["just the one thing"])

_p3 = _Poster()
S.Slack.post(_p3, "C1", "Yes please \u23ce go ahead", None, split=False)
check("the console keeps the marker visible, so \u2705 is informed",
      [t for _, t, _ in _p3.client.sent] == ["Yes please \u23ce go ahead"])

# VOICE.md tells Solo to write the bare hold and flag that the draft needs a time from the owner.
# That note was rendered only on the escalate branch, so on a hold it went into the item and
# nowhere the owner would ever read it — the instruction pointed at a dead channel.
def _hold_note_is_shown():
    i = msg(d, U, D, "3000.000400", "when will the handover note be ready?")
    S.execute_decision(d, S.load_item(i), {"action": "hold", "text": "Checking",
                                           "note": "needs a time from you before it sends"},
                       d.ledger, d.followups, set(), state=d.state)
    return any("needs a time from you" in (p[1] or "") for p in d.bot.posted)


check("a hold's note reaches the owner, not just the item file", _hold_note_is_shown())

# `total_cost_usd` is the session running total, not the run's. Stored raw and then summed, four
# days of drafts added up to $124 against a real spend of $36 — and the per-message figure it
# implied ($3.91 for the word "Will do") pointed straight at the wrong problem.
def _marginal_costs():
    seen = []
    real = S._run_claude_once

    def fake(item, session, prompt=None):
        seen.append(session)
        total = 0.40 + 0.20 * len(seen)   # a session total that only ever climbs
        return [], {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": total}, ""

    S._run_claude_once = fake
    try:
        it = S.load_item(iid)
        first = S.run_claude(it, d.state)[1]["marginal_cost_usd"]
        second = S.run_claude(S.load_item(iid), d.state)[1]["marginal_cost_usd"]
    finally:
        S._run_claude_once = real
    return first, second


_m1, _m2 = _marginal_costs()
check("the first run in a session costs what the session has cost", abs(_m1 - 0.60) < 1e-6)
check("the second is charged the delta, not the running total again", abs(_m2 - 0.20) < 1e-6)

# 13KB of worked examples in every dump, to produce "Checking". They are the single biggest block
# in `show` after VOICE.md, and four routes in five never write prose at all.
TRIAGE = {"source": "jev:test", "confidence": 0.6, "topic": "status_request", "sensitivity": 2.0,
          "urgency": 3.0, "needs_context": 0.8, "asks_for_content": 0.2, "can_answer": 0.3}


def _rendered(route):
    it = dict(S.load_item(iid), triage=dict(TRIAGE, route=route))
    return S.render_item(it, d.ledger, set())


check("the worked examples are there when the writer actually composes a reply",
      "<them>" in _rendered("reply"))
check("and are not carried on a route that ends in a word or two", "<them>" not in _rendered("hold"))
check("the writer is told how to fetch them if it needs them anyway",
      "voice --user" in _rendered("escalate"))

# A standing dead state — logged out, rate-limited — used to sit unreported until a real message
# paid to discover it. Both of today's outages were found by hand. The probe is the fix, and the
# thing that can go wrong with it is noise: an alert on every tick is an alert nobody reads.
DOWN = {"type": "result", "is_error": True, "result": "Not logged in · Please run /login"}
UP = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0004}


def _probe(result, health=None, mins=30, dispatch="claude", logged_in=(True, "")):
    """Run one probe against a scripted writer outcome → (notifications, health, prompts run)."""
    calls, notes = [], []
    real_run, real_notify, real_auth = S._run_claude_once, S.Daemon.notify, S.writer_logged_in
    S._run_claude_once = lambda item, session, prompt=None: (calls.append(prompt), ([], result, ""))[1]
    S.Daemon.notify = lambda self, text: notes.append(text)
    S.writer_logged_in = lambda: logged_in
    old_dispatch, old_mins = d.dispatch, S.CFG["health_probe_minutes"]
    d.dispatch, S.CFG["health_probe_minutes"] = dispatch, mins
    d.state.update(lambda x: x.update(health=health or {}))
    try:
        d.probe_writer()
    finally:
        S._run_claude_once, S.Daemon.notify, S.writer_logged_in = real_run, real_notify, real_auth
        d.dispatch, S.CFG["health_probe_minutes"] = old_dispatch, old_mins
    return notes, (d.state.read().get("health") or {}), calls


_notes, _health, _ = _probe(DOWN)
check("a writer that cannot draft is reported without waiting for a message to fail",
      len(_notes) == 1 and "cannot draft" in _notes[0])
check("and the reason is the writer's own words, not a guessed one",
      "Not logged in" in _notes[0] and not _health["ok"])
check("approvals are not implied to be broken too — only drafting is",
      "✅" in _notes[0])

_notes2, _, _ = _probe(DOWN, health={"ok": False, "why": "Not logged in", "since": "2026-10-16T17:19:00"})
check("a still-broken writer is not re-announced every five minutes", not _notes2)

_notes3, _health3, _ = _probe(UP, health={"ok": False, "why": "Not logged in", "since": "2026-10-16T17:19:00"})
check("coming back is worth one line too", len(_notes3) == 1 and "can draft again" in _notes3[0])
check("and recovery stamps a fresh last_ok", _health3["ok"] and _health3["last_ok"])

# The probe only costs anything when nothing else has proved the writer works. A real run stamps
# last_ok, so a day with traffic never pays for one.
_, _, _calls = _probe(UP, health={"ok": True, "last_ok": S.now_iso()})
check("a writer that drafted just now is not probed again", not _calls)
_, _, _calls0 = _probe(DOWN, mins=0)
check("HEALTH_PROBE_MINUTES=0 switches off the paid probe, which costs ~$0.21 a go", not _calls0)

# Being logged out is the failure that actually happened, twice, and `claude auth status` reports
# it for nothing — so that half runs every tick whether or not the paid probe is on.
_lo_notes, _lo_health, _lo_calls = _probe(UP, mins=0, logged_in=(False, "Not logged in to Claude Code"))
check("a logged-out node is caught by the free check, with the paid probe off",
      len(_lo_notes) == 1 and "cannot draft" in _lo_notes[0] and not _lo_health["ok"])
check("and catching it that way costs no model call at all", not _lo_calls)
_up_notes, _, _ = _probe(UP, health={"ok": False, "why": "Not logged in"}, mins=0)
check("logging back in is noticed for free too", len(_up_notes) == 1 and "can draft again" in _up_notes[0])
_quiet, _, _ = _probe(UP, health={"ok": True}, mins=0)
check("and a healthy node stays quiet every five minutes forever", not _quiet)

# An auth check that cannot be read is not evidence of being logged out. The credentials *file*
# would have made exactly that mistake — the laptop has none and works, via the Keychain.
def _auth_unreadable():
    real = S.CFG["claude_bin"]
    S.CFG["claude_bin"] = "/nonexistent-claude"
    try:
        return S.writer_logged_in()
    finally:
        S.CFG["claude_bin"] = real


check("an unreadable auth check does not cry wolf", _auth_unreadable()[0])
_, _, _calls_n = _probe(DOWN, dispatch="none")
check("and DISPATCH=none has no writer to probe", not _calls_n)

# The predicate itself. Today's logged-out run came back *with* a result object, so a check that
# reads only the exit code calls it healthy.
check("a logged-out run is not healthy, however it exits", not S.writer_healthy(DOWN)[0])
check("a successful run is healthy", S.writer_healthy(UP)[0])
check("no result at all falls back to what the process said",
      S.writer_healthy(None, "exit 1: boom")[1] == "exit 1: boom")

# The voice harness scores drafts against what the owner actually wrote. An empty draft passed
# every regex at first — no greeting and no full stop because it has nothing — so silence in reply
# to a direct question scored perfect. That is the one failure the harness exists to catch.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from voice_eval import score as vscore  # noqa: E402

check("an empty draft fails when they actually asked something",
      not vscore("", {"inbound": "Can you share the OTP?", "actual": "Checking"})["clean"])
check("an empty draft is fine on a statement with no ask",
      vscore("", {"inbound": "these are test users fyi", "actual": "Cool"})["clean"])
check("a greeting fails — there are zero in 200 of their messages",
      not vscore("Hi Sri! Thanks for reaching out.", {"inbound": "when?", "actual": "morrow"})["clean"])
check("an invented time fails — Solo has no calendar and must not promise one",
      not vscore("Checking, back by 3pm", {"inbound": "when will it be done?", "actual": "Checking"})["clean"])
check("the bare hold passes — it is what they actually write",
      vscore("Checking", {"inbound": "when will it be done?", "actual": "Checking"})["clean"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} checks passed")
