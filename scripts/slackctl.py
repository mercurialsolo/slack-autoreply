#!/usr/bin/env python3
"""slackctl — the Slack side of the slack-autoreply skill.

Claude Code is the brain; this is the nervous system. It never writes a reply.

  listen        daemon: Socket Mode → guards → Jev triage → inbox item → delay +
                stand-down → hands the item to Claude Code (`claude -p "/slack-autoreply handle …"`)
                → executes Claude's decision under policy. Also: ✅/❌ on drafts,
                follow-up reminders, learning signals, keeps your voice samples fresh.
  inbox         queued items (for `/slack-autoreply run` when DISPATCH=none)
  show ID       everything Claude needs to compose: message, thread, triage, policy, voice
  act ID …      Claude's decision: --action ignore|ack|hold|reply|escalate [--text …] [--sources …]
  simulate TXT  make a pretend inbound message (nothing is sent) to dry-run the skill
  deadline      --urgency N → the phrase + time a hold should promise
  status / followups / drafts / approve / drop / review-data / build-voice / voice / done

All state lives in ~/.slack-autoreply (AUTOREPLY_HOME).
"""
from __future__ import annotations

import argparse
import fcntl
import html
import json
import logging
import os
import random
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import recall  # noqa: E402
import voice  # noqa: E402
from config import CFG, DROP_REACTIONS, OK_SUBTYPES, SEND_REACTIONS  # noqa: E402
from followups import Followup, Followups, hold_deadline  # noqa: E402
from learning import Ledger  # noqa: E402
from triage import Policy, Triager  # noqa: E402

log = logging.getLogger("slackctl")
SCRIPT = os.path.abspath(__file__)                 # as installed (may be a symlink into a repo)
SCRIPT_REAL = str(Path(__file__).resolve())        # the real file
STATE_FILE = config.p("state.json")
AUDIT = config.p("replies.jsonl")

# ------------------------------------------------------------ pure helpers


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def in_quiet_hours(spec: str, now: datetime | None = None) -> bool:
    if not spec:
        return False
    try:
        a, b = (int(x) for x in spec.split("-"))
    except ValueError:
        return False
    h = (now or datetime.now()).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def clean_text(text: str, name_of) -> str:
    text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + name_of(m.group(1)), text or "")
    text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<!(here|channel|everyone)(?:\|[^>]*)?>", r"@\1", text)
    return html.unescape(text).strip()


def quote(text: str) -> str:
    return "\n".join("> " + line for line in (text or "").splitlines())


def looks_like_user_id(s: str) -> bool:
    return bool(re.fullmatch(r"[UW][A-Z0-9]{6,}", s))


def looks_like_channel_id(s: str) -> bool:
    return bool(re.fullmatch(r"[CDG][A-Z0-9]{6,}", s))


def item_path(item_id: str) -> Path:
    return config.INBOX_DIR / f"{item_id}.json"


def load_item(ref: str) -> dict:
    p = Path(ref)
    if not p.exists():
        p = item_path(ref)
    if not p.exists():
        sys.exit(f"no such inbox item: {ref}")
    return json.loads(p.read_text(encoding="utf-8"))


def save_item(item: dict) -> None:
    config.ensure_dirs()
    p = item_path(item["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(item, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def list_items(statuses: set[str] | None = None) -> list[dict]:
    config.ensure_dirs()
    out = []
    for p in sorted(config.INBOX_DIR.glob("*.json")):
        try:
            it = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if statuses is None or it.get("status") in statuses:
            out.append(it)
    return out


def audit(**row) -> None:
    row["at"] = now_iso()
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class State:
    """Daemon bookkeeping shared with interactive `act` via state.json (file-locked)."""

    def __init__(self):
        self.path = STATE_FILE

    def _read(self) -> dict:
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        for k in ("pending", "sent", "ignored", "last_auto", "sessions"):
            data.setdefault(k, {})
        return data

    def update(self, fn):
        config.ensure_dirs()
        with open(self.path, "a+", encoding="utf-8") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            data = self._read()
            result = fn(data)
            # trim
            for k in ("sent", "ignored", "last_auto"):
                d = data.get(k, {})
                if len(d) > 800:
                    for key in sorted(d, key=lambda x: d[x].get("at", ""))[: len(d) - 800]:
                        d.pop(key, None)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
            fcntl.flock(lockf, fcntl.LOCK_UN)
        return result

    def read(self) -> dict:
        return self._read()


# ------------------------------------------------------------------ Slack


class Slack:
    def __init__(self):
        from slack_sdk import WebClient
        from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

        token = os.getenv("SLACK_USER_TOKEN", "")
        if not token.startswith("xoxp-"):
            sys.exit("SLACK_USER_TOKEN missing or not a *User* OAuth Token (xoxp-…). See README.")
        self.client = WebClient(token=token)
        self.client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
        who = self.client.auth_test()
        self.me: str = who["user_id"]
        self.team: str = who.get("team", "")
        prof = self.client.users_info(user=self.me)["user"]
        self.me_handle: str = prof["name"]
        self.me_name: str = (prof.get("profile") or {}).get("display_name") or prof.get("real_name") or self.me_handle
        self.self_dm: str = self.client.conversations_open(users=self.me)["channel"]["id"]
        self.names: dict[str, str] = {self.me: self.me_name}
        self.handles: dict[str, str] = {self.me: self.me_handle}
        self.channel_names: dict[str, str] = {}

    def name_of(self, uid: str) -> str:
        if uid not in self.names:
            self.names[uid], self.handles[uid] = uid, uid
            try:
                u = self.client.users_info(user=uid)["user"]
                self.names[uid] = (u.get("profile") or {}).get("display_name") or u.get("real_name") or u.get("name") or uid
                self.handles[uid] = u.get("name") or uid
            except Exception:
                pass
        return self.names[uid]

    def handle_of(self, uid: str) -> str:
        self.name_of(uid)
        return self.handles.get(uid, uid)

    def channel_name(self, cid: str) -> str:
        if cid not in self.channel_names:
            self.channel_names[cid] = cid
            try:
                self.channel_names[cid] = self.client.conversations_info(channel=cid)["channel"].get("name") or cid
            except Exception:
                pass
        return self.channel_names[cid]

    def permalink(self, channel: str, ts: str) -> str | None:
        try:
            return self.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
        except Exception:
            return None

    def presence_active(self) -> bool:
        try:
            return self.client.users_getPresence(user=self.me)["presence"] == "active"
        except Exception:
            return False

    def resolve_users(self, entries: list[str]) -> set[str]:
        ids, handles = set(), set()
        for e in entries:
            e = e.lstrip("@")
            if looks_like_user_id(e):
                ids.add(e)
            else:
                handles.add(e.lower())
        if handles:
            cursor = None
            while True:
                res = self.client.users_list(limit=500, cursor=cursor)
                for u in res["members"]:
                    keys = {(u.get("name") or "").lower(), ((u.get("profile") or {}).get("display_name") or "").lower(),
                            (u.get("real_name") or "").lower()}
                    if keys & handles:
                        ids.add(u["id"])
                cursor = (res.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        return ids

    def resolve_channels(self, entries: list[str]) -> set[str]:
        ids, names = set(), set()
        for e in entries:
            e = e.lstrip("#")
            if looks_like_channel_id(e):
                ids.add(e)
            else:
                names.add(e.lower())
        if names:
            cursor = None
            while True:
                res = self.client.conversations_list(types="public_channel,private_channel", exclude_archived=True, limit=999, cursor=cursor)
                for c in res["channels"]:
                    if (c.get("name") or "").lower() in names:
                        ids.add(c["id"])
                cursor = (res.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        return ids

    def transcript(self, channel: str, thread_ts: str | None, n: int) -> str:
        if thread_ts:
            msgs = self.client.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
            msgs = msgs[:1] + msgs[1:][-n:]
        else:
            msgs = self.client.conversations_history(channel=channel, limit=n)["messages"][::-1]
        lines = []
        for m in msgs:
            if m.get("subtype") not in OK_SUBTYPES | {"thread_broadcast"}:
                continue
            uid = m.get("user") or m.get("bot_id") or "?"
            who = f"{self.me_name} (me)" if uid == self.me else self.name_of(uid)
            text = clean_text(m.get("text", ""), self.name_of)
            if m.get("files") and not text:
                text = "[shared a file]"
            if text:
                lines.append(f"[{who}]: {text}")
        return "\n".join(lines)

    def newer_messages(self, channel: str, thread_ts: str | None, since_ts: str) -> list[dict]:
        if thread_ts:
            msgs = self.client.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
        else:
            msgs = self.client.conversations_history(channel=channel, oldest=since_ts, limit=50)["messages"]
        return [m for m in msgs if float(m.get("ts", 0)) > float(since_ts) and m.get("subtype") in OK_SUBTYPES]

    def previous_other_message(self, channel: str, ts: str) -> dict | None:
        try:
            msgs = self.client.conversations_history(channel=channel, latest=ts, inclusive=False, limit=3)["messages"]
        except Exception:
            return None
        for m in msgs:
            if m.get("user") and m["user"] != self.me and m.get("subtype") in OK_SUBTYPES and m.get("text"):
                return m
        return None

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        return self.client.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)["ts"]

    def react(self, channel: str, ts: str, emoji: str) -> None:
        self.client.reactions_add(channel=channel, timestamp=ts, name=emoji)

    def update(self, channel: str, ts: str, text: str) -> None:
        self.client.chat_update(channel=channel, ts=ts, text=text)

    def notify(self, text: str) -> str:
        return self.post(self.self_dm, text)


# ------------------------------------------------------------------ core


def load_services():
    """Ledger + follow-ups + triager, shared by the daemon and the CLI."""
    config.ensure_dirs()
    ledger = Ledger(config.p("outcomes.jsonl"), config.p("corrections.jsonl"), CFG)
    try:
        import anthropic
        llm = anthropic.Anthropic() if config.env("ANTHROPIC_API_KEY") else None
    except ImportError:
        llm = None
    return ledger, llm


def make_triager(me_name: str, llm) -> Triager:
    return Triager(me_name, Policy(min_confidence=CFG["triage_min_confidence"], sensitivity_escalate=CFG["sensitivity_escalate"]),
                   llm, CFG["triage_fallback_model"])


def describe_where(item: dict) -> str:
    ct = item["channel_type"]
    if ct == "im":
        return f"DM with {item['sender_name']}"
    if ct == "mpim":
        return "group DM"
    return f"#{item.get('channel_name') or item['channel']}" + (" (private)" if ct == "group" else "")


def policy_preview(item: dict, ledger: Ledger, share_trusted: set[str]) -> dict:
    t = item.get("triage") or {}
    route = t.get("route") or "reply"
    mode, why = ledger.effective_mode(route if route in ("reply", "hold") else "reply", item["sender"], item["channel"], CFG["mode"])
    share_ok = item["sender"] in share_trusted or ledger.share_ok(item["sender"])
    return {"mode": CFG["mode"], "effective_for_reply": mode, "reason": why, "share_trusted": share_ok}


def render_item(item: dict, ledger: Ledger, share_trusted: set[str], followups: Followups | None = None) -> str:
    t = item.get("triage")
    pol = policy_preview(item, ledger, share_trusted)
    out = [f"=== inbox item {item['id']}{'  (SIMULATED — nothing will be sent)' if item.get('simulated') else ''} ===",
           f"from: {item['sender_name']} (@{item.get('sender_handle', '?')}) · {describe_where(item)}"
           f"{' · in a thread' if item.get('thread_ts') else ''} · {item.get('created', '')}",
           f"message: {item['text']}",
           "", "--- recent conversation ---", item.get("context") or "(none)", ""]
    if t:
        out += ["--- triage (" + t.get("source", "?") + ") ---",
                f"route: {t['route']}{' (forced: ' + t['forced'] + ')' if t.get('forced') else ''} · confidence {t['confidence']:.2f} · "
                f"topic: {t.get('topic', 'other')} · "
                f"sensitivity {t['sensitivity']:.1f}/5 · urgency {t['urgency']:.1f}/5 · "
                f"needs your files/accounts {t['needs_context']:.2f} · asks for material {t['asks_for_content']:.2f} · "
                f"answerable now {t.get('can_answer', 0.5):.2f}"]
        if float(t.get("can_answer", 0.5)) < 0.5:
            out.append("note: triage says you probably do NOT have what a good answer needs — look it up, "
                       "or promise a time. Do not invent a number, date or status.")
        phrase, due = hold_deadline(t["urgency"])
        out.append(f"if hold: promise '{phrase}' (due {due:%a %-I:%M%p})")
    else:
        out += ["--- triage ---", "unavailable — decide the route yourself (see SKILL.md); if hold, run: deadline --urgency N"]
    out += [f"policy: MODE={pol['mode']} · a reply/hold to {item['sender_name']} currently goes {pol['effective_for_reply'].upper()}"
            f" ({pol['reason']}) · context replies to them without review: {'yes' if pol['share_trusted'] else 'no → draft'}"]
    if followups:
        opens = [f for f in followups.open() if f.counterpart == item["sender"]]
        if opens:
            out.append("open promises to them: " + "; ".join(f"'{f.phrase}' re {f.summary[:60]}" for f in opens))
    person = recall.load_dossier("people", item["sender"])
    chan = recall.load_dossier("channels", item["channel"]) if item["channel_type"] in ("channel", "group", "mpim") else ""
    out += ["", f"--- what you know about {item['sender_name']} ---", person or "(nothing on file yet — if you learn something durable, note it)"]
    if item["channel_type"] in ("channel", "group", "mpim"):
        out += [f"--- about this channel ---", chan or "(nothing on file yet)"]
    st = ledger.stats(sender=item["sender"])
    cs = [c for c in ledger.corrections(50) if c.get("sender") == item["sender"]][-2:]
    out.append(f"track record with them: {st}" + ("" if not cs else "; last corrections: " + " | ".join(f"bot '{(c.get('draft') or '')[:60]}' → you '{(c.get('actual') or '')[:60]}'" for c in cs)))
    out += ["", "need more? (run before deciding if the message refers to something not in view)",
            f"  python3 {SCRIPT} history {item['channel']} --limit 100" + (f" --thread {item['thread_ts']}" if item.get("thread_ts") else ""),
            f"  python3 {SCRIPT} search \"<words>\" --with {item['sender']}",
            f"  python3 {SCRIPT} about {item['sender']}",
            f"  python3 {SCRIPT} note {item['sender']} \"<one durable fact about them>\""]
    out += ["", "--- your voice (VOICE.md) ---", voice.load_notes() or "(VOICE.md is empty — run /slack-autoreply setup)",
            "", "--- how you actually write (real messages; <them> is what you were replying to) ---",
            voice.format_samples(voice.pick_samples(voice.load_samples(), item["channel"], item["sender"]))]
    return "\n".join(out)


def execute_decision(slack: Slack, item: dict, decision: dict, ledger: Ledger, followups: Followups,
                     share_trusted: set[str], observed_tools: list[str] | None = None, state: State | None = None) -> str:
    """Carry out Claude's decision under policy. Returns a one-line summary."""
    state = state or State()
    action = decision["action"]
    text = (decision.get("text") or "").strip()
    observed_tools = observed_tools or []
    context_tools = [t for t in observed_tools if t in ("Read", "Glob", "Grep", "WebFetch", "WebSearch") or t.startswith("mcp__")]
    used_context = bool(decision.get("used_context")) or bool(context_tools)
    decision["used_context"] = used_context
    if context_tools and not decision.get("sources"):
        decision["sources"] = sorted({t.split("__")[1] if t.startswith("mcp__") else t.lower() for t in context_tools})
    item["decision"] = decision
    item["status"] = "done"
    item["decided"] = now_iso()
    channel, thread_ts, sender, who = item["channel"], item.get("reply_thread_ts"), item["sender"], item["sender_name"]

    if item.get("simulated"):
        mode, why = ledger.effective_mode(action, sender, channel, CFG["mode"])
        gate = used_context and not (sender in share_trusted or ledger.share_ok(sender))
        verdict = {"ignore": "nothing", "ack": f":{CFG['ack_emoji']}: reaction",
                   "escalate": "⚠️ alert + draft in your self-DM",
                   "hold": f"{'SEND' if mode == 'auto' and not gate else 'DRAFT'} (hold)",
                   "reply": f"{'SEND' if mode == 'auto' and not gate else 'DRAFT'} (reply)"}[action]
        item["outcome"] = {"status": "simulated", "would": verdict}
        save_item(item)
        return f"[simulated] {action} → would {verdict}" + (f"\n{text}" if text else "")

    if action == "ignore":
        item["outcome"] = {"status": "ignored"}
        save_item(item)
        state.update(lambda d: d["ignored"].__setitem__(channel, {"item": item["id"], "at": now_iso(), "ts": item["ts"]}))
        audit(item=item["id"], counterpart=who, status="ignored", trigger=item["text"][:120])
        return f"ignored (from {who})"

    if action == "ack":
        if CFG["ack_style"] == "reaction" or not text:
            slack.react(channel, item["ts"], CFG["ack_emoji"])
            item["outcome"] = {"status": "acked", "emoji": CFG["ack_emoji"]}
            save_item(item)
            audit(item=item["id"], counterpart=who, status="ack_reaction", trigger=item["text"][:120])
            return f"acked {who} with :{CFG['ack_emoji']}:"
        ts = slack.post(channel, text, thread_ts)
        item["outcome"] = {"status": "sent", "ts": ts}
        save_item(item)
        audit(item=item["id"], counterpart=who, status="sent_ack", reply=text)
        return f"acked {who}: {text}"

    if action == "escalate":
        t = item.get("triage") or {}
        note = (f"⚠️ *Needs you* — *{who}* in {describe_where(item)}"
                + (f" (sensitivity {t['sensitivity']:.1f}/5, urgency {t['urgency']:.1f}/5)" if t else "")
                + (f"\n{decision['note']}" if decision.get("note") else "")
                + f"\n{quote(item['text'][:400])}" + (f"\n{item['permalink']}" if item.get("permalink") else ""))
        if text:
            draft_ts = slack.notify(f"{note}\n*Draft* — react ✅ to send as-is, ❌ to drop\n{quote(text)}")
            item["outcome"] = {"status": "escalated_draft", "draft_ts": draft_ts}
            state.update(lambda d: d["pending"].__setitem__(draft_ts, item["id"]))
        else:
            slack.notify(note)
            item["outcome"] = {"status": "escalated"}
        item["policy"] = {"effective": "draft", "reason": "escalate"}
        save_item(item)
        audit(item=item["id"], counterpart=who, status="escalated", reply=text, trigger=item["text"][:120])
        return f"escalated to you (from {who})"

    # hold / reply
    if not text:
        item["outcome"] = {"status": "no_reply"}
        save_item(item)
        return f"no reply (from {who})"
    mode, why = ledger.effective_mode(action, sender, channel, CFG["mode"])
    gate = used_context and not (sender in share_trusted or ledger.share_ok(sender))
    item["policy"] = {"effective": "draft" if gate else mode, "reason": ("share gate: " + why) if gate else why, "used_context": used_context}
    fu = None
    if action == "hold":
        t = item.get("triage") or {}
        phrase, due = hold_deadline(float(decision.get("urgency") or t.get("urgency") or 3))
        fu = dict(channel=channel, thread_ts=thread_ts, counterpart=sender, counterpart_name=who, summary=item["text"],
                  phrase=phrase, due=due, permalink=item.get("permalink"))
        item["followup"] = {**fu, "due": due.isoformat(timespec="minutes")}

    if mode == "auto" and not gate:
        ts = slack.post(channel, text, thread_ts)
        item["outcome"] = {"status": "sent", "ts": ts, "at": now_iso()}
        save_item(item)
        state.update(lambda d: (d["sent"].__setitem__(ts, {"item": item["id"], "channel": channel, "at": now_iso(), "text": text}),
                                d["last_auto"].__setitem__(channel, {"item": item["id"], "at": now_iso()})))
        if fu:
            followups.add(**fu)
        audit(item=item["id"], counterpart=who, status=f"sent_{action}", reply=text, sources=decision.get("sources"), mode=why)
        return f"sent ({action}) to {who}: {text[:100]}"

    where = describe_where(item)
    note = f"*Draft {action} → {where}* — react ✅ to send, ❌ to drop\n_{'needs your ok: pulled from ' + ', '.join(decision.get('sources') or ['your context']) if gate else why}_"
    draft_ts = slack.notify(f"{note}\n{quote(text)}")
    item["outcome"] = {"status": "drafted", "draft_ts": draft_ts, "at": now_iso()}
    save_item(item)
    state.update(lambda d: d["pending"].__setitem__(draft_ts, item["id"]))
    audit(item=item["id"], counterpart=who, status=f"drafted_{action}", reply=text, sources=decision.get("sources"), mode=why)
    return f"drafted ({action}) for your ✅ — {who}: {text[:100]}"


def approve_draft(slack: Slack, item: dict, ledger: Ledger, followups: Followups, state: State, draft_ts: str) -> str:
    d = item["decision"]
    ts = slack.post(item["channel"], d["text"], item.get("reply_thread_ts"))
    if item.get("followup"):
        fu = dict(item["followup"])
        fu["due"] = datetime.fromisoformat(fu["due"])
        followups.add(**fu)
    item["outcome"] = {**item.get("outcome", {}), "status": "sent_from_draft", "ts": ts, "approved": now_iso()}
    save_item(item)
    state.update(lambda d_: (d_["pending"].pop(draft_ts, None), d_["sent"].__setitem__(ts, {"item": item["id"], "channel": item["channel"], "at": now_iso(), "text": d["text"]})))
    ledger.record(item=item, outcome="approved")
    try:
        slack.update(slack.self_dm, draft_ts, f"✅ Sent to {item['sender_name']}\n{quote(d['text'])}")
    except Exception:
        pass
    audit(item=item["id"], counterpart=item["sender_name"], status="sent_from_draft", reply=d["text"])
    return f"sent to {item['sender_name']}"


def drop_draft(slack: Slack, item: dict, ledger: Ledger, state: State, draft_ts: str) -> str:
    item["outcome"] = {**item.get("outcome", {}), "status": "dropped", "dropped": now_iso()}
    save_item(item)
    state.update(lambda d_: d_["pending"].pop(draft_ts, None))
    ledger.record(item=item, outcome="dropped", correction={"draft": item["decision"].get("text"), "actual": None})
    try:
        slack.update(slack.self_dm, draft_ts, f"❌ Dropped\n{quote(item['decision'].get('text', ''))}")
    except Exception:
        pass
    audit(item=item["id"], counterpart=item["sender_name"], status="dropped", reply=item["decision"].get("text"))
    return f"dropped draft for {item['sender_name']}"


# ----------------------------------------------------------------- daemon


def discover_mcp_names(cwd: str | None, allow: list[str]) -> list[str]:
    names: set[str] = set()
    cj = Path.home() / ".claude.json"
    if cj.exists():
        try:
            data = json.loads(cj.read_text(encoding="utf-8"))
            names |= set((data.get("mcpServers") or {}).keys())
            if cwd:
                names |= set((((data.get("projects") or {}).get(cwd) or {}).get("mcpServers") or {}).keys())
        except (json.JSONDecodeError, OSError):
            pass
    if cwd and (Path(cwd) / ".mcp.json").exists():
        try:
            names |= set((json.loads((Path(cwd) / ".mcp.json").read_text(encoding="utf-8")).get("mcpServers") or {}).keys())
        except (json.JSONDecodeError, OSError):
            pass
    if allow and allow != ["*"]:
        names = {n for n in names if n in allow}
    return sorted(names)


def session_key(item: dict) -> str:
    return f"{item['channel']}:{item['reply_thread_ts']}" if item.get("reply_thread_ts") else item["channel"]


def pick_session(item: dict, state: "State") -> tuple[str, bool]:
    """(session id, resume?) — one Claude session per conversation, renewed when old or long."""
    key = session_key(item)
    s = state.read().get("sessions", {}).get(key)
    if CFG["session_continuity"] and s:
        age_ok = datetime.now() - datetime.fromisoformat(s["last"]) < timedelta(hours=CFG["session_max_age_hours"])
        if age_ok and s.get("runs", 0) < CFG["session_max_runs"]:
            return s["id"], True
    return str(uuid.uuid4()), False


def claude_command(item: dict, session: tuple[str, bool] | None = None) -> tuple[list[str], dict, str]:
    """The headless Claude Code invocation for one inbox item."""
    dirs = [str(Path(os.path.expanduser(d)).resolve()) for d in CFG["context_dirs"] if d.strip() and d.strip() != "*"]
    any_dir = any(d.strip() == "*" for d in CFG["context_dirs"])
    cwd = CFG["claude_cwd"] or (dirs[0] if dirs else str(config.STATE_DIR))
    mcp = discover_mcp_names(cwd, CFG["mcp_allow"])
    allowed = [f"Bash(python3 {SCRIPT} *)"] + ([f"Bash(python3 {SCRIPT_REAL} *)"] if SCRIPT_REAL != SCRIPT else [])
    allowed += ["Glob", "Grep", "WebFetch", "WebSearch"]
    allowed += ["Read"] if any_dir else [f"Read(//{d.lstrip('/')}/**)" for d in dirs]
    allowed += [f"mcp__{n}__*" for n in mcp]
    cmd = [CFG["claude_bin"], "-p", f"/slack-autoreply handle {item_path(item['id'])}",
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "dontAsk",
           "--allowedTools", ",".join(allowed),
           "--disallowedTools", "Write,Edit,MultiEdit,NotebookEdit,Agent",
           "--max-turns", str(CFG["claude_max_turns"])]
    for d in dirs:
        cmd += ["--add-dir", d]
    if CFG["claude_model"]:
        cmd += ["--model", CFG["claude_model"]]
    if session:
        sid, resume = session
        cmd += (["--resume", sid] if resume else ["--session-id", sid])
    env = dict(os.environ)
    env["AUTOREPLY_DISPATCH"] = "1"
    env["AUTOREPLY_HOME"] = str(config.STATE_DIR)
    if not CFG["dispatch_use_api_key"]:
        env.pop("ANTHROPIC_API_KEY", None)   # bill the subscription login, not the API key
    return cmd, env, cwd


def run_claude(item: dict, state: "State | None" = None) -> tuple[list[str], dict | None, str]:
    """Run Claude Code on an item in that conversation's session. Returns (tools used, result object, stderr tail)."""
    state = state or State()
    session = pick_session(item, state)
    tools, result, err = _run_claude_once(item, session)
    if result is None and session[1]:
        log.warning("resume of session %s failed (%s) — starting a fresh one", session[0][:8], err[:120])
        session = (str(uuid.uuid4()), False)
        tools, result, err = _run_claude_once(item, session)
    if result is not None:
        key, sid = session_key(item), session[0]

        def rec(d):
            prev = d["sessions"].get(key) or {}
            runs = prev.get("runs", 0) + 1 if prev.get("id") == sid else 1
            d["sessions"][key] = {"id": sid, "runs": runs, "last": now_iso()}
            if len(d["sessions"]) > 300:
                for k in sorted(d["sessions"], key=lambda x: d["sessions"][x]["last"])[: len(d["sessions"]) - 300]:
                    d["sessions"].pop(k, None)
        state.update(rec)
        item["session"] = {"id": sid, "resumed": session[1]}
    return tools, result, err


def _run_claude_once(item: dict, session: tuple[str, bool]) -> tuple[list[str], dict | None, str]:
    cmd, env, cwd = claude_command(item, session)
    log.info("dispatch %s → %s (%s session %s)", item["id"], shlex.join(cmd[:3]) + " …", "resume" if session[1] else "new", session[0][:8])
    tools: list[str] = []
    result = None
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=CFG["claude_timeout"])
    except subprocess.TimeoutExpired:
        return tools, None, "timeout"
    except FileNotFoundError:
        return tools, None, f"claude binary not found: {CFG['claude_bin']}"
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for b in ((ev.get("message") or {}).get("content") or []):
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                    tools.append(b["name"])
        elif ev.get("type") == "result":
            result = ev
    err = (proc.stderr or "")[-600:]
    if proc.returncode != 0 and not result:
        err = f"exit {proc.returncode}: {err}"
    return tools, result, err


class Daemon(Slack):
    def __init__(self, dispatch: str):
        super().__init__()
        config.ensure_dirs()
        self.dispatch = dispatch
        self.ledger, self.llm = load_services()
        self.triager = make_triager(self.me_name, self.llm)
        self.followups = Followups(str(config.p("followups.json")), self._remind, CFG["followup_lead"])
        self.state = State()
        self.allow_users = self.resolve_users(CFG["allow_users"])
        self.block_users = self.resolve_users(CFG["block_users"])
        self.share_trusted = self.resolve_users(CFG["share_trusted"])
        self.allow_channels = self.resolve_channels(CFG["allow_channels"])
        self.block_channels = self.resolve_channels(CFG["block_channels"])
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.sem = threading.Semaphore(CFG["dispatch_parallel"])
        self.chan_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.lock = threading.Lock()
        self.seen: OrderedDict[str, bool] = OrderedDict()
        self.recent: dict[str, deque] = defaultdict(deque)
        voice.ensure_voice_file()

    # ------------------------------------------------------------ plumbing

    def _submit(self, fn, *a):
        def w():
            try:
                fn(*a)
            except Exception:
                log.exception("handler failed")
        self.pool.submit(w)

    def _dup(self, event_id: str | None) -> bool:
        if not event_id:
            return False
        with self.lock:
            if event_id in self.seen:
                return True
            self.seen[event_id] = True
            while len(self.seen) > 5000:
                self.seen.popitem(last=False)
        return False

    def _over_limit(self, channel: str) -> bool:
        now = time.time()
        with self.lock:
            dq = self.recent[channel]
            while dq and now - dq[0] > 3600:
                dq.popleft()
            return len(dq) >= CFG["max_per_hour"]

    def _count(self, channel: str) -> None:
        with self.lock:
            self.recent[channel].append(time.time())

    def _allowed(self, sender: str, channel: str) -> bool:
        if sender in self.block_users or channel in self.block_channels:
            return False
        if self.allow_users and sender not in self.allow_users:
            return False
        if self.allow_channels and channel not in self.allow_channels:
            return False
        return True

    def _remind(self, f: Followup) -> None:
        due = datetime.fromisoformat(f.due).strftime("%a %-I:%M%p").lower()
        self.notify(f"⏰ You told *{f.counterpart_name}* you'd get back {f.phrase} (due {due}) about:\n{quote(f.summary)}"
                    + (f"\n{f.permalink}" if f.permalink else ""))

    # -------------------------------------------------------------- events

    def on_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "reaction_added":
            self.on_reaction(ev)
            return
        if t != "message":
            return
        sub = ev.get("subtype")
        if sub == "message_changed":
            self.on_changed(ev)
            return
        if sub == "message_deleted":
            self.on_deleted(ev)
            return
        if sub not in OK_SUBTYPES:
            return
        sender, channel = ev.get("user"), ev.get("channel")
        if not sender or not channel or ev.get("bot_id") or sender == "USLACKBOT":
            return
        if sender == self.me:
            self.on_owner_message(ev)
            return
        self.on_inbound(ev)

    def on_owner_message(self, ev: dict) -> None:
        """You typed something yourself: close promises, learn from it, keep your voice fresh."""
        channel, ts, thread_ts = ev["channel"], ev["ts"], ev.get("thread_ts")
        st = self.state.read()
        if ts in st.get("sent", {}):
            return  # our own send
        for f in self.followups.resolve(channel, thread_ts):
            log.info("follow-up to %s closed — you replied", f.counterpart_name)
        text = clean_text(ev.get("text", ""), self.name_of)
        # a pending draft for this conversation → you replied instead of ✅: learn the difference
        for draft_ts, item_id in list(st.get("pending", {}).items()):
            try:
                item = load_item(item_id)
            except SystemExit:
                continue
            if item["channel"] != channel or (item.get("reply_thread_ts") and item["reply_thread_ts"] != thread_ts):
                continue
            item["outcome"] = {**item.get("outcome", {}), "status": "replaced", "actual": text, "at": now_iso()}
            save_item(item)
            self.state.update(lambda d: d["pending"].pop(draft_ts, None))
            self.ledger.record(item=item, outcome="replaced", correction={"draft": item["decision"].get("text"), "actual": text})
            try:
                self.update(self.self_dm, draft_ts, f"↩️ You replied yourself to {item['sender_name']} — learned from it\n{quote(item['decision'].get('text', ''))}")
            except Exception:
                pass
            log.info("draft for %s replaced by your own reply", item["sender_name"])
        # we ignored something you then answered → missed
        ig = st.get("ignored", {}).get(channel)
        if ig and datetime.now() - datetime.fromisoformat(ig["at"]) < timedelta(hours=2):
            try:
                item = load_item(ig["item"])
                item["outcome"] = {**item.get("outcome", {}), "status": "missed", "actual": text}
                save_item(item)
                self.ledger.record(item=item, outcome="missed", correction={"draft": None, "actual": text})
                log.info("missed: you replied to a message triage ignored (%s)", item["sender_name"])
            except SystemExit:
                pass
            self.state.update(lambda d: d["ignored"].pop(channel, None))
        # you followed up soon after an auto-reply → neutral signal, shown in review
        la = st.get("last_auto", {}).get(channel)
        if la and datetime.now() - datetime.fromisoformat(la["at"]) < timedelta(minutes=30):
            try:
                self.ledger.record(item=load_item(la["item"]), outcome="followed_up", note=text[:200])
            except SystemExit:
                pass
            self.state.update(lambda d: d["last_auto"].pop(channel, None))
        # fresh voice sample
        if channel != self.self_dm and voice.usable(text) and ev.get("channel_type") in ("im", "mpim", "channel", "group"):
            prev = self.previous_other_message(channel, ts)
            sample = {"text": text, "channel": channel, "ts": ts,
                      "channel_name": self.channel_name(channel) if ev.get("channel_type") in ("channel", "group") else None}
            if prev:
                sample["prompt"] = clean_text(prev.get("text", ""), self.name_of)
                sample["reply_to_user"] = prev.get("user")
            voice.append_sample(sample, CFG["voice_cap"])

    def on_changed(self, ev: dict) -> None:
        msg, prev = ev.get("message") or {}, ev.get("previous_message") or {}
        if msg.get("user") != self.me or (msg.get("text") or "") == (prev.get("text") or ""):
            return
        st = self.state.read()
        s = st.get("sent", {}).get(msg.get("ts"))
        if not s:
            return
        try:
            item = load_item(s["item"])
        except SystemExit:
            return
        actual = clean_text(msg.get("text", ""), self.name_of)
        item["outcome"] = {**item.get("outcome", {}), "status": "corrected", "actual": actual}
        save_item(item)
        self.ledger.record(item=item, outcome="corrected", correction={"draft": s.get("text"), "actual": actual})
        self.state.update(lambda d: d["sent"].pop(msg.get("ts"), None))
        log.info("corrected: you edited the reply to %s", item["sender_name"])

    def on_deleted(self, ev: dict) -> None:
        ts = (ev.get("previous_message") or {}).get("ts") or ev.get("deleted_ts")
        st = self.state.read()
        s = st.get("sent", {}).get(ts)
        if not s:
            return
        try:
            item = load_item(s["item"])
        except SystemExit:
            return
        item["outcome"] = {**item.get("outcome", {}), "status": "deleted"}
        save_item(item)
        self.ledger.record(item=item, outcome="deleted", correction={"draft": s.get("text"), "actual": None})
        self.state.update(lambda d: d["sent"].pop(ts, None))
        log.info("deleted: you removed the reply to %s", item["sender_name"])

    def on_reaction(self, ev: dict) -> None:
        item_ref = ev.get("item") or {}
        if ev.get("user") != self.me or item_ref.get("type") != "message" or item_ref.get("channel") != self.self_dm:
            return
        draft_ts = item_ref.get("ts")
        st = self.state.read()
        item_id = st.get("pending", {}).get(draft_ts)
        if not item_id:
            return
        item = load_item(item_id)
        r = ev.get("reaction", "")
        if r in SEND_REACTIONS:
            log.info(approve_draft(self, item, self.ledger, self.followups, self.state, draft_ts))
        elif r in DROP_REACTIONS:
            log.info(drop_draft(self, item, self.ledger, self.state, draft_ts))

    # ------------------------------------------------------------ inbound

    def on_inbound(self, ev: dict) -> None:
        sender, channel, ctype = ev["user"], ev["channel"], ev.get("channel_type", "")
        text = ev.get("text", "") or ""
        mentioned = f"<@{self.me}>" in text
        if ctype in ("channel", "group"):
            if CFG["channels"] != "mentions" or not mentioned:
                return
        elif ctype == "mpim":
            if CFG["group_dms"] == "off" or (CFG["group_dms"] == "mentions" and not mentioned):
                return
        elif ctype != "im":
            return
        who = self.name_of(sender)
        clean = clean_text(text, self.name_of)
        if not self._allowed(sender, channel):
            log.info("skip (allow/block) %s: %s", who, clean[:80]); return
        if in_quiet_hours(CFG["quiet_hours"]):
            log.info("skip (quiet hours) %s: %s", who, clean[:80]); return
        if CFG["only_when_away"] and self.presence_active():
            log.info("skip (you're active) %s: %s", who, clean[:80]); return
        if self._over_limit(channel):
            log.info("skip (rate limit) %s: %s", who, clean[:80]); return

        thread_ts = ev.get("thread_ts")
        reply_thread_ts = thread_ts or (ev["ts"] if ctype in ("channel", "group") else None)
        item = {
            "id": f"{channel}-{ev['ts'].replace('.', '_')}", "created": now_iso(), "status": "queued", "simulated": False,
            "channel": channel, "channel_type": ctype,
            "channel_name": self.channel_name(channel) if ctype in ("channel", "group") else None,
            "ts": ev["ts"], "thread_ts": thread_ts, "reply_thread_ts": reply_thread_ts,
            "sender": sender, "sender_name": who, "sender_handle": self.handle_of(sender),
            "text": clean, "permalink": self.permalink(channel, ev["ts"]),
            "context": self.transcript(channel, thread_ts, 8),
        }
        state = {"owner": self.me_name, "owner_notes": voice.load_notes(), "owner_presence": "active" if self.presence_active() else "away",
                 "local_time": datetime.now().strftime("%A %H:%M"), "channel_type": ctype, "channel_name": item["channel_name"],
                 "sender": who, "sender_trusted_for_sharing": sender in self.share_trusted,
                 "what_owner_knows_about_sender": recall.trim(recall.load_dossier("people", sender)),
                 "what_owner_knows_about_channel": recall.trim(recall.load_dossier("channels", channel)) if ctype != "im" else "",
                 "recent_conversation": item["context"], "latest_message": clean}
        t = self.triager.run(state)
        item["triage"] = None if t.source == "default" else t.__dict__ | {"raw": None}
        save_item(item)
        log.info("%s in %s: %s → %s", who, ctype, clean[:80], t.summary() if item["triage"] else "no triage")
        if item["triage"] and t.route == "ignore":
            execute_decision(self, item, {"action": "ignore", "text": ""}, self.ledger, self.followups, self.share_trusted, state=self.state)
            return

        delay = CFG["delay_min"] if (item["triage"] and t.urgency >= 4) else random.uniform(CFG["delay_min"], CFG["delay_max"])
        log.info("…%s in %.0fs", "handing to Claude" if self.dispatch == "claude" else "queued; drain with /slack-autoreply run", delay)
        time.sleep(delay)
        newer = self.newer_messages(channel, reply_thread_ts, ev["ts"])
        if any(m.get("user") == self.me for m in newer):
            item["status"], item["outcome"] = "done", {"status": "stood_down", "why": "you replied"}
            save_item(item); log.info("stand down — you already replied to %s", who); return
        if any(m.get("user") == sender for m in newer):
            item["status"], item["outcome"] = "done", {"status": "stood_down", "why": "superseded"}
            save_item(item); log.info("stand down — %s sent more; the later message will be handled", who); return
        item["context"] = self.transcript(channel, thread_ts, CFG["context_messages"])
        save_item(item)
        if self.dispatch != "claude":
            return
        self.handle_with_claude(item)

    def handle_with_claude(self, item: dict) -> None:
        with self.sem, self.chan_locks[item["channel"]]:
            item["status"] = "handling"
            save_item(item)
            tools, result, err = run_claude(item, self.state)
            session = item.get("session")
            item = load_item(item["id"])  # Claude's `act` wrote the decision
            item["session"] = session
            decision = item.get("decision")
            cost = (result or {}).get("total_cost_usd")
            if not decision:
                item["status"], item["outcome"] = "failed", {"status": "failed", "why": err or (result or {}).get("subtype") or "no decision"}
                save_item(item)
                log.error("Claude returned no decision for %s: %s", item["id"], item["outcome"]["why"])
                self.notify(f"⚠️ Couldn't handle a message from *{item['sender_name']}* ({describe_where(item)}): {item['outcome']['why'][:200]}\n{quote(item['text'][:300])}"
                            + (f"\n{item['permalink']}" if item.get("permalink") else ""))
                return
            item["run"] = {"tools": tools, "cost_usd": cost, "turns": (result or {}).get("num_turns"), "subtype": (result or {}).get("subtype")}
            summary = execute_decision(self, item, decision, self.ledger, self.followups, self.share_trusted, observed_tools=tools, state=self.state)
            self._count(item["channel"])
            log.info("%s%s", summary, f" · ${cost:.3f}" if cost else "")

    # ----------------------------------------------------------------- tick

    def tick(self) -> None:
        """Every few minutes: score auto-sends that aged cleanly, expire stale drafts."""
        st = self.state.read()
        now = datetime.now()
        for ts, s in list(st.get("sent", {}).items()):
            if now - datetime.fromisoformat(s["at"]) >= timedelta(hours=CFG["observe_hours"]):
                try:
                    item = load_item(s["item"])
                    if (item.get("outcome") or {}).get("status") in ("sent", "sent_from_draft"):
                        item["outcome"]["status"] = "clean"
                        save_item(item)
                        if item["outcome"].get("approved") is None:   # approved drafts were already scored
                            self.ledger.record(item=item, outcome="clean")
                except SystemExit:
                    pass
                self.state.update(lambda d: d["sent"].pop(ts, None))
        for draft_ts, item_id in list(st.get("pending", {}).items()):
            try:
                item = load_item(item_id)
            except SystemExit:
                self.state.update(lambda d: d["pending"].pop(draft_ts, None)); continue
            at = (item.get("outcome") or {}).get("at")
            if at and now - datetime.fromisoformat(at) >= timedelta(hours=CFG["draft_expire_hours"]):
                item["outcome"]["status"] = "expired"
                save_item(item)
                self.state.update(lambda d: d["pending"].pop(draft_ts, None))
                self.ledger.record(item=item, outcome="expired")
                try:
                    self.update(self.self_dm, draft_ts, f"⌛ Expired (no decision in {CFG['draft_expire_hours']:.0f}h)\n{quote(item['decision'].get('text', ''))}")
                except Exception:
                    pass

    def run(self) -> None:
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse

        app_token = os.getenv("SLACK_APP_TOKEN", "")
        if not app_token.startswith("xapp-"):
            sys.exit("SLACK_APP_TOKEN missing (xapp-…). See README.")
        sm = SocketModeClient(app_token=app_token, web_client=self.client)

        def listener(client, req):
            if req.type != "events_api":
                return
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            if self._dup(req.payload.get("event_id")):
                return
            self._submit(self.on_event, req.payload.get("event") or {})

        sm.socket_mode_request_listeners.append(listener)
        sm.connect()
        log.info("listening as @%s (%s) · MODE=%s · dispatch=%s · triage=%s · inbox=%s",
                 self.me_handle, self.me, CFG["mode"], self.dispatch,
                 "jev" if self.triager.jev_ok else (CFG["triage_fallback_model"] if self.llm else "none (Claude decides)"),
                 config.INBOX_DIR)
        if self.dispatch == "claude":
            cmd, _, cwd = claude_command({"id": "ITEM"})
            log.info("will run: %s  (cwd %s)", shlex.join(cmd), cwd)
        try:
            while True:
                time.sleep(300)
                self._submit(self.tick)
        except KeyboardInterrupt:
            pass
        finally:
            sm.close()


# -------------------------------------------------------------------- CLI


def cmd_doctor(a):
    import doctor
    sys.exit(doctor.run())


def cmd_status(a):
    s = Slack()
    ledger, llm = load_services()
    fus = Followups(str(config.p("followups.json")), lambda f: None, CFG["followup_lead"])
    st = State().read()
    print(f"account: @{s.me_handle} ({s.me_name}, {s.me}) · workspace {s.team}")
    print(f"state dir: {config.STATE_DIR}")
    print(f"mode: {CFG['mode']} · dispatch: {CFG['dispatch']} · triage: {'Jev' if os.getenv('TYPESAFE_API_KEY') else (CFG['triage_fallback_model'] if llm else 'none — Claude decides routes')}")
    print(f"voice: {'VOICE.md ok' if voice.load_notes() else 'VOICE.md empty'} · {len(voice.load_samples())} samples · "
          f"dossiers: {len(list(recall.PEOPLE_DIR.glob('*.md')))} people, {len(list(recall.CHANNELS_DIR.glob('*.md')))} channels · "
          f"sessions: {len(st.get('sessions', {}))} live")
    print(f"queued items: {len(list_items({'queued'}))} · handling: {len(list_items({'handling'}))} · pending drafts: {len(st.get('pending', {}))} · open follow-ups: {len(fus.open())}")
    print(f"context dirs: {', '.join(CFG['context_dirs']) or '(none — set CONTEXT_DIRS)'} · mcp: {', '.join(discover_mcp_names(CFG['claude_cwd'], CFG['mcp_allow'])) or '(none found)'}")
    print()
    print(ledger.scorecard(14))


def cmd_build_voice(a):
    s = Slack()
    samples = voice.build_samples(s.client, s.me, s.me_handle, a.count, not a.no_context)
    if not samples:
        sys.exit("no messages found — does the app have search:read?")
    voice.save_samples(samples)
    paired = sum(1 for x in samples if x.get("prompt"))
    created = voice.ensure_voice_file()
    print(f"saved {len(samples)} samples ({paired} with the message they replied to) → {voice.SAMPLES_FILE}")
    if created:
        print(f"created {voice.VOICE_FILE} from the template — fill it in (or let /slack-autoreply setup draft it)")


def cmd_voice(a):
    samples = voice.load_samples()
    if a.all:
        print(voice.format_samples(samples[-a.limit:]))
        return
    print("--- VOICE.md ---\n" + (voice.load_notes() or "(empty)"))
    print("\n--- samples ---\n" + voice.format_samples(voice.pick_samples(samples, a.channel, a.user, max_samples=a.limit)))


def cmd_listen(a):
    logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    Daemon(a.dispatch or CFG["dispatch"]).run()


def cmd_inbox(a):
    items = list_items({"queued", "handling"} if not a.all else None)
    if not items:
        print("inbox empty")
    for it in items:
        t = it.get("triage") or {}
        print(f"{it['id']}  [{it['status']}]  {it['sender_name']:<18} {describe_where(it):<22} {t.get('route', '?'):<9} {it['text'][:70]}")


def cmd_show(a):
    item = load_item(a.item)
    ledger, _ = load_services()
    share = set(CFG["share_trusted"])
    fus = Followups(str(config.p("followups.json")), lambda f: None, CFG["followup_lead"])
    print(render_item(item, ledger, share, fus))
    if item["status"] == "queued":
        item["status"] = "handling"
        save_item(item)


def cmd_simulate(a):
    config.ensure_dirs()
    ledger, llm = load_services()
    me_name = os.getenv("ME_NAME") or "me"
    try:
        s = Slack(); me_name = s.me_name
    except (SystemExit, Exception):
        pass
    text = a.text
    item = {"id": f"sim-{uuid.uuid4().hex[:8]}", "created": now_iso(), "status": "queued", "simulated": True,
            "channel": "SIM", "channel_type": a.channel_type, "channel_name": a.channel_name,
            "ts": f"{time.time():.6f}", "thread_ts": None, "reply_thread_ts": None,
            "sender": "USIM", "sender_name": a.sender, "sender_handle": a.sender.lower().replace(" ", ""),
            "text": text, "permalink": None, "context": f"[{a.sender}]: {text}"}
    t = make_triager(me_name, llm).run({"owner": me_name, "owner_notes": voice.load_notes(), "owner_presence": "away",
                                         "local_time": datetime.now().strftime("%A %H:%M"), "channel_type": a.channel_type,
                                         "channel_name": a.channel_name, "sender": a.sender, "sender_trusted_for_sharing": False,
                                         "recent_conversation": item["context"], "latest_message": text})
    item["triage"] = None if t.source == "default" else t.__dict__ | {"raw": None}
    save_item(item)
    print(render_item(item, ledger, set()))
    print(f"\nnext: python3 {SCRIPT} act {item['id']} --action … (simulated: nothing is sent)")


def cmd_deadline(a):
    phrase, due = hold_deadline(a.urgency)
    print(f"{phrase}\t{due.isoformat(timespec='minutes')}")


def cmd_act(a):
    item = load_item(a.item)
    text = (a.text or "").replace("\\n", "\n").strip()
    if a.text_file:
        text = Path(a.text_file).read_text(encoding="utf-8").strip()
    if a.action in ("hold", "reply") and not text:
        sys.exit("--text is required for hold/reply")
    decision = {"action": a.action, "text": text, "sources": [s for s in (a.sources or "").split(",") if s],
                "used_context": bool(a.sources), "note": a.note or "", "urgency": a.urgency, "at": now_iso()}
    item["decision"] = decision
    if os.getenv("AUTOREPLY_DISPATCH") == "1":
        item["status"] = "decided"
        save_item(item)
        print(f"decision recorded: {a.action}" + (f" — {text[:80]}" if text else ""))
        return
    ledger, _ = load_services()
    if item.get("simulated"):
        print(execute_decision(None, item, decision, ledger, None, set()))  # type: ignore[arg-type]
        return
    s = Slack()
    fus = Followups(str(config.p("followups.json")), lambda f: None, CFG["followup_lead"])
    print(execute_decision(s, item, decision, ledger, fus, s.resolve_users(CFG["share_trusted"]), state=State()))


def cmd_done(a):
    item = load_item(a.item)
    item["status"] = "done"
    item["outcome"] = {"status": a.status}
    save_item(item)
    print(f"{item['id']} → {a.status}")


def cmd_followups(a):
    fus = Followups(str(config.p("followups.json")), lambda f: None, CFG["followup_lead"])
    items = fus.open()
    if not items:
        print("no open follow-ups")
    for f in items:
        print(f"{f.due}  {f.counterpart_name:<20} {f.phrase:<18} {f.summary[:70]}  [{f.status}]")


def cmd_drafts(a):
    st = State().read()
    if not st.get("pending"):
        print("no pending drafts")
    for draft_ts, item_id in st.get("pending", {}).items():
        try:
            it = load_item(item_id)
        except SystemExit:
            continue
        txt = ((it.get("decision") or {}).get("text") or "").replace("\n", " / ")
        print(f"{item_id}  → {it['sender_name']:<18} {(it.get('decision') or {}).get('action', '?'):<9} {txt[:80]}")


def _find_pending(item_id: str) -> str:
    st = State().read()
    for draft_ts, iid in st.get("pending", {}).items():
        if iid == item_id:
            return draft_ts
    sys.exit(f"{item_id} has no pending draft")


def cmd_approve(a):
    s = Slack(); ledger, _ = load_services()
    fus = Followups(str(config.p("followups.json")), lambda f: None, CFG["followup_lead"])
    print(approve_draft(s, load_item(a.item), ledger, fus, State(), _find_pending(a.item)))


def cmd_drop(a):
    s = Slack(); ledger, _ = load_services()
    print(drop_draft(s, load_item(a.item), ledger, State(), _find_pending(a.item)))


def _slack_and_services():
    s = Slack()
    ledger, _ = load_services()
    fus = Followups(str(config.p("followups.json")), lambda f: None, CFG["followup_lead"])
    return s, ledger, fus


def cmd_history(a):
    s = Slack()
    kind, ident, _ = recall.resolve_target(s, a.target)
    channel = recall.dm_channel(s, ident) if kind == "people" else ident
    print(recall.history(s, channel, a.thread, a.limit, a.before))


def cmd_search(a):
    s = Slack()
    w = recall.resolve_target(s, a.with_)[1] if a.with_ else None
    f = recall.resolve_target(s, a.from_)[1] if a.from_ else None
    c = recall.resolve_target(s, a.in_)[1] if a.in_ else None
    print(recall.search(s, a.query, with_user=w, in_channel=c, from_user=f, limit=a.limit))


def cmd_about(a):
    s, ledger, fus = _slack_and_services()
    kind, ident, name = recall.resolve_target(s, a.target)
    if kind == "people":
        print(recall.about(s, ident, ledger, fus, a.limit))
    else:
        print(f"=== #{name} ({ident}) ===\n--- dossier ---\n{recall.load_dossier('channels', ident) or '(none yet)'}\n")
        print(recall.history(s, ident, None, a.limit))


def _target_offline(target: str):
    """Raw Slack IDs need no API call; @handles and #names do."""
    t = target.strip()
    if looks_like_user_id(t):
        return "people", t, t
    if looks_like_channel_id(t) and not t.startswith("D"):
        return "channels", t, t
    return None


def cmd_note(a):
    if a.target.upper().startswith("USIM") or a.target == "SIM":
        print("(simulated target — note not saved)"); return
    off = _target_offline(a.target)
    if off:
        kind, ident, title = off
        try:
            s = Slack()
            title = f"{s.name_of(ident)} (@{s.handle_of(ident)}, {ident})" if kind == "people" else f"#{s.channel_name(ident)} ({ident})"
        except (SystemExit, Exception):
            pass
    else:
        s = Slack()
        kind, ident, name = recall.resolve_target(s, a.target)
        title = f"{name} (@{s.handle_of(ident)}, {ident})" if kind == "people" else f"#{name} ({ident})"
    p = recall.note(kind, ident, title, a.text)
    print(f"noted → {p}")


def cmd_dossier(a):
    off = _target_offline(a.target)
    kind, ident, name = off if off else recall.resolve_target(Slack(), a.target)
    print(recall.load_dossier(kind, ident) or f"(nothing on file for {name})")


def cmd_review_data(a):
    ledger, _ = load_services()
    print(ledger.review_text(a.days))
    print("\n--- VOICE.md as it stands ---\n" + (voice.load_notes() or "(empty)"))
    print(f"\nVOICE.md path: {voice.VOICE_FILE}")
    files = sorted(recall.PEOPLE_DIR.glob("*.md")) + sorted(recall.CHANNELS_DIR.glob("*.md"))
    if files:
        print(f"\n--- dossiers ({len(files)}) --- consolidate long ones; each should stay a short page")
        for f in files:
            lines = f.read_text(encoding="utf-8").splitlines()
            print(f"  {f}  ({len(lines)} lines) {lines[0] if lines else ''}")


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="preflight every dependency; sends nothing").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("build-voice"); p.add_argument("--count", type=int, default=200); p.add_argument("--no-context", action="store_true"); p.set_defaults(fn=cmd_build_voice)
    p = sub.add_parser("voice"); p.add_argument("--channel"); p.add_argument("--user"); p.add_argument("--all", action="store_true"); p.add_argument("--limit", type=int, default=40); p.set_defaults(fn=cmd_voice)
    p = sub.add_parser("listen"); p.add_argument("--dispatch", choices=["claude", "none"]); p.set_defaults(fn=cmd_listen)
    p = sub.add_parser("inbox"); p.add_argument("--all", action="store_true"); p.set_defaults(fn=cmd_inbox)
    p = sub.add_parser("show"); p.add_argument("item"); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("simulate"); p.add_argument("text"); p.add_argument("--from", dest="sender", default="Sam"); p.add_argument("--channel-type", default="im", choices=["im", "mpim", "channel", "group"]); p.add_argument("--channel-name"); p.set_defaults(fn=cmd_simulate)
    p = sub.add_parser("deadline"); p.add_argument("--urgency", type=float, required=True); p.set_defaults(fn=cmd_deadline)
    p = sub.add_parser("act"); p.add_argument("item"); p.add_argument("--action", required=True, choices=["ignore", "ack", "hold", "reply", "escalate"])
    p.add_argument("--text", help="message text; use \\n for new lines"); p.add_argument("--text-file"); p.add_argument("--sources", help="comma list of where you looked: fs,notion,drive,github,calendar,web …")
    p.add_argument("--note", help="for escalate: why it needs them"); p.add_argument("--urgency", type=float); p.set_defaults(fn=cmd_act)
    p = sub.add_parser("done"); p.add_argument("item"); p.add_argument("--status", default="skipped"); p.set_defaults(fn=cmd_done)
    sub.add_parser("followups").set_defaults(fn=cmd_followups)
    sub.add_parser("drafts").set_defaults(fn=cmd_drafts)
    p = sub.add_parser("approve"); p.add_argument("item"); p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("drop"); p.add_argument("item"); p.set_defaults(fn=cmd_drop)
    p = sub.add_parser("review-data"); p.add_argument("--days", type=int, default=14); p.set_defaults(fn=cmd_review_data)
    p = sub.add_parser("history", help="older messages: @handle (your DM), #channel, or an ID"); p.add_argument("target"); p.add_argument("--limit", type=int, default=100); p.add_argument("--thread"); p.add_argument("--before", help="page back from this ts"); p.set_defaults(fn=cmd_history)
    p = sub.add_parser("search", help="Slack search"); p.add_argument("query"); p.add_argument("--with", dest="with_", help="@handle: only your DM with them"); p.add_argument("--from", dest="from_", help="@handle: only messages by them"); p.add_argument("--in", dest="in_", help="#channel"); p.add_argument("--limit", type=int, default=20); p.set_defaults(fn=cmd_search)
    p = sub.add_parser("about", help="profile, dossier, track record, open promises, recent DM"); p.add_argument("target"); p.add_argument("--limit", type=int, default=20); p.set_defaults(fn=cmd_about)
    p = sub.add_parser("note", help="append one durable fact to a person/channel dossier"); p.add_argument("target"); p.add_argument("text"); p.set_defaults(fn=cmd_note)
    p = sub.add_parser("dossier"); p.add_argument("target"); p.set_defaults(fn=cmd_dossier)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
