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
check("policy: confident reply but not answerable→hold",
      pol.apply(triage.Triage("reply", 0.9, 2, 2, 0, 0, "t", can_answer=0.2)).route == "hold")
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
check("CONTEXT_MCP_ALLOW=none grants no mcp servers", S.discover_mcp_names(None, ["none"]) == [])

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

iid3 = msg(d, U, D, "1700000004.000", "fyi shipped it")
S.execute_decision(d, S.load_item(iid3), {"action": "ignore", "text": ""}, d.ledger, d.followups, set(), state=d.state)
msg(d, "UME", D, "1700000005.000", "nice, thanks!")
check("answering an ignored message → missed", d.ledger.rows[-1]["outcome"] == "missed")

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

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} checks passed")
