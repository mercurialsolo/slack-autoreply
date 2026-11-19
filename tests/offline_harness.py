"""Shared fixture for the offline suites (test_offline.py, test_offline_intake.py): a temp HOME,
a fake `claude` binary, a fake Slack client, and the `check` counter. Importing it sets the
environment before slackctl is imported, so import it first.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
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
printf 'CCD=%s %s\\n' "${{CLAUDE_CONFIG_DIR:-}}" "$*" >> {TMP}/claude-calls.log
_auth='{{"loggedIn":true,"authMethod":"claude.ai"}}'
if [ "$1 $2" = "auth status" ]; then echo "${{FAKE_AUTH:-$_auth}}"; exit 0; fi
if [ "$1 $2" = "plugin list" ]; then [ -f "${{FAKE_PLUGINS_FILE:-/nonexistent}}" ] && cat "$FAKE_PLUGINS_FILE" && exit 0; exit 1; fi
if [ "$1 $2" = "plugin enable" ] || [ "$1 $2" = "plugin install" ]; then [ -f "${{FAKE_HEAL_TO:-/nonexistent}}" ] && cp "$FAKE_HEAL_TO" "$FAKE_PLUGINS_FILE"; exit 0; fi
if [ "$1" = "-p" ] && [ -f "${{FAKE_PLUGINS_FILE:-/nonexistent}}" ] && ! grep -q '"enabled": true' "$FAKE_PLUGINS_FILE"; then echo '{{"type":"result","subtype":"success","is_error":false,"num_turns":1,"total_cost_usd":0,"result":"/solo:slack-autoreply isn'"'"'t available in this session"}}'; exit 0; fi
if [ -n "${{FAKE_FAIL_RESUME:-}}" ] && printf '%s ' "$@" | grep -q -- '--resume'; then echo "No conversation found" >&2; exit 1; fi
if [ -f "${{FAKE_500:-/nonexistent}}" ]; then echo '{{"type":"result","subtype":"success","is_error":true,"num_turns":1,"total_cost_usd":0,"result":"API Error: 500 Internal server error"}}'; exit 1; fi
if [ -f "${{FAKE_LIMIT:-/nonexistent}}" ] && ! {{ [ -n "${{FAKE_LIMIT_SPARE:-}}" ] && printf '%s ' "$@" | grep -q -- "$FAKE_LIMIT_SPARE"; }}; then echo '{{"type":"result","subtype":"success","is_error":true,"num_turns":1,"total_cost_usd":0,"terminal_reason":"api_error","result":"You'"'"'ve hit your weekly limit · resets 12pm (America/Los_Angeles)"}}'; exit 1; fi
if [ -f "${{FAKE_SKIP_ONCE:-/nonexistent}}" ]; then rm -f "$FAKE_SKIP_ONCE"; printf '%s\\n' "$@" > "$FAKE_SKIP_ONCE.argv"; echo '{{"type":"result","subtype":"success","is_error":false,"num_turns":1,"total_cost_usd":0.0,"result":"nothing to do"}}'; exit 0; fi
{sys.executable} {SCRIPTS}/slackctl.py act "$item" --action reply --text "yep, latest one is in Drive: https://x.io/deck\\nping me if the link doesn't open" --sources drive >/dev/null
echo '{{"type":"system","subtype":"init","mcp_servers":[{{"name":"drive","status":"connected"}}]}}'
echo '{{"type":"assistant","message":{{"content":[{{"type":"tool_use","id":"t1","name":"mcp__drive__search","input":{{}}}},{{"type":"text","text":"ok"}}]}}}}'
echo '{{"type":"result","subtype":"success","is_error":false,"num_turns":3,"total_cost_usd":0.0,"result":"sent"}}'
""")
FAKE_CLAUDE.chmod(0o755)

ENV = {
    "HOME": str(HOME), "AUTOREPLY_HOME": str(STATE), "REPLY_DELAY_MIN": "0", "REPLY_DELAY_MAX": "0", "OBSERVE_HOURS": "0",
    "CLAUDE_BIN": str(FAKE_CLAUDE), "CONTEXT_DIRS": str(HOME / "proj"), "MODE": "learn", "LOG_LEVEL": "WARNING",
    "NOTE_SHAPER": "off",  # the fake claude would answer a shaping prompt by acting
    "WRITER_POOL": "off",  # this host may have the cluster CLI: never lease a real account from a test
}
for k in ("SLACK_USER_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
    os.environ.pop(k, None)
os.environ.update(ENV)
sys.path.insert(0, str(SCRIPTS))

import slackctl as S  # noqa: E402

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
    def users_list(self, **k): return {"members": [{"id": U, "name": "priya", "profile": {"display_name": "Priya"}, "real_name": "Priya"},
                                                   {"id": "UME", "name": "barada", "profile": {"display_name": "Barada"}, "real_name": "Barada"}], "response_metadata": {}}
    def users_lookupByEmail(self, email):
        self.email_lookups = getattr(self, "email_lookups", 0) + 1
        if email == "priya@example.com": return {"user": {"id": U, "name": "priya"}}
        raise RuntimeError("The request to the Slack API failed.\nThe server responded with: {'ok': False, 'error': 'users_not_found'}")
    def conversations_list(self, **k): return {"channels": [{"id": C, "name": "growth"}], "response_metadata": {}}
    def conversations_history(self, channel, **k):
        msgs = self.history.get(channel, [])
        if k.get("latest"): msgs = [m for m in msgs if float(m["ts"]) < float(k["latest"]) or (k.get("inclusive") and m["ts"] == k["latest"])]
        if k.get("oldest"): msgs = [m for m in msgs if float(m["ts"]) > float(k["oldest"])]
        return {"messages": list(reversed(msgs))[: k.get("limit", 50)], "response_metadata": {}}
    def conversations_replies(self, channel, ts, **k): return {"messages": getattr(self, "replies", {}).get(ts, []), "response_metadata": {}}
    def chat_postMessage(self, channel, text, thread_ts=None, blocks=None):
        self.counter += 1; ts = f"{2000000000 + self.counter}.000000"; self.posted.append((channel, text, thread_ts, ts))
        if blocks: self.blocks = getattr(self, "blocks", {}); self.blocks[ts] = blocks
        return {"ts": ts}
    def files_upload_v2(self, **k):
        if getattr(self, "upload_error", None):
            raise RuntimeError(self.upload_error)
        self.counter += 1; ts = f"{2000000000 + self.counter}.000000"
        self.uploads = getattr(self, "uploads", []); self.uploads.append(dict(k, ts=ts))
        return {"files": [{"id": f"F{self.counter}", "name": f["filename"],
                           "shares": {"public": {k["channel"]: [{"ts": ts}]}}} for f in k["file_uploads"]]}
    def reactions_add(self, channel, timestamp, name): self.reactions.append((channel, timestamp, name)); return {}
    def chat_update(self, channel, ts, text, blocks=None):
        self.updates.append((channel, ts, text)); self.update_blocks = getattr(self, "update_blocks", {}); self.update_blocks[ts] = blocks
        return {}
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
