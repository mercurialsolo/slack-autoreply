"""Memory beyond the thread window.

Dossiers   ~/.slack-autoreply/people/<UID>.md and channels/<CID>.md — who someone is, what you're
           working on together, how you talk to them, what "the deck" means. `show` prints them,
           Jev reads them, `note` appends to them, the skill's review mode consolidates them.
Recall     history / search / about — pulled on demand from Slack with your user token, so Claude
           looks back only when a message needs it instead of every prompt carrying everything.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import config

PEOPLE_DIR = config.p("people")
CHANNELS_DIR = config.p("channels")


# ------------------------------------------------------------- dossiers


def dossier_path(kind: str, ident: str) -> Path:
    return (PEOPLE_DIR if kind == "people" else CHANNELS_DIR) / f"{ident}.md"


def load_dossier(kind: str, ident: str) -> str:
    p = dossier_path(kind, ident)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def note(kind: str, ident: str, title: str, line: str) -> Path:
    """Append a dated line; create the file with a header on first use."""
    p = dossier_path(kind, ident)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(f"# {title}\n\n", encoding="utf-8")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"- {datetime.now():%Y-%m-%d}: {line.strip()}\n")
    return p


def trim(text: str, n: int = 1500) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# -------------------------------------------------------------- targets


def resolve_target(slack, s: str) -> tuple[str, str, str]:
    """'@priya' / 'U…' → ('people', uid, name); '#general' / 'C…' / 'G…' → ('channels', cid, name); 'D…' → the person in that DM."""
    s = s.strip()
    if s.startswith("@") or re.fullmatch(r"[UW][A-Z0-9]{6,}", s):
        uid = s if not s.startswith("@") else next(iter(slack.resolve_users([s])), None)
        if not uid:
            raise SystemExit(f"no Slack user matches {s}")
        return "people", uid, slack.name_of(uid)
    if s.startswith("D") and re.fullmatch(r"D[A-Z0-9]{6,}", s):
        try:
            uid = slack.client.conversations_info(channel=s)["channel"].get("user")
        except Exception:
            uid = None
        if uid:
            return "people", uid, slack.name_of(uid)
        return "channels", s, s
    if s.startswith("#") or re.fullmatch(r"[CG][A-Z0-9]{6,}", s):
        cid = s if not s.startswith("#") else next(iter(slack.resolve_channels([s])), None)
        if not cid:
            raise SystemExit(f"no channel matches {s}")
        return "channels", cid, slack.channel_name(cid)
    raise SystemExit(f"can't resolve {s!r}: use @handle, #channel, or a Slack ID")


def dm_channel(slack, uid: str) -> str:
    return slack.client.conversations_open(users=uid)["channel"]["id"]


# --------------------------------------------------------------- recall


def _fmt(slack, m: dict) -> str:
    from slackctl import clean_text  # late import to avoid a cycle
    uid = m.get("user") or m.get("bot_id") or "?"
    who = f"{slack.me_name} (me)" if uid == slack.me else slack.name_of(uid)
    when = datetime.fromtimestamp(float(m.get("ts", 0))).strftime("%Y-%m-%d %H:%M")
    text = clean_text(m.get("text", ""), slack.name_of) or ("[shared a file]" if m.get("files") else "")
    tail = f"  (thread, {m['reply_count']} replies)" if m.get("reply_count") else ""
    return f"[{when}] {who}: {text}{tail}"


def history(slack, channel: str, thread_ts: str | None = None, limit: int = 100, before: str | None = None) -> str:
    """Oldest → newest, dated. `before` is a ts to page further back from."""
    limit = max(1, min(limit, 500))
    msgs: list[dict] = []
    cursor = None
    while len(msgs) < limit:
        kw = {"channel": channel, "limit": min(200, limit - len(msgs)), "cursor": cursor}
        if before:
            kw["latest"] = before
            kw["inclusive"] = False
        res = slack.client.conversations_replies(ts=thread_ts, **kw) if thread_ts else slack.client.conversations_history(**kw)
        batch = res.get("messages", [])
        if not batch:
            break
        msgs.extend(batch)
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    if not thread_ts:
        msgs = msgs[::-1]
    msgs = [m for m in msgs if m.get("subtype") in (None, "file_share", "thread_broadcast")]
    lines = [_fmt(slack, m) for m in msgs]
    oldest = msgs[0]["ts"] if msgs else None
    head = f"--- {len(lines)} messages in {slack.channel_name(channel) if channel[0] in 'CG' else channel}" + (f" (thread {thread_ts})" if thread_ts else "") + " ---"
    foot = f"--- older: slackctl history {channel} --before {oldest} ---" if oldest and len(msgs) >= limit else ""
    return "\n".join([head, *lines, foot]).strip()


def search(slack, query: str, with_user: str | None = None, in_channel: str | None = None,
           from_user: str | None = None, limit: int = 20) -> str:
    q = query
    if with_user:
        q += f" in:@{slack.handle_of(with_user)}"
    if in_channel:
        q += f" in:#{slack.channel_name(in_channel)}"
    if from_user:
        q += f" from:@{slack.handle_of(from_user)}"
    res = slack.client.search_messages(query=q, sort="timestamp", sort_dir="desc", count=max(1, min(limit, 100)))
    matches = (res.get("messages") or {}).get("matches", [])
    if not matches:
        return f"no results for: {q}"
    from slackctl import clean_text
    lines = [f"--- {len(matches)} results for: {q} ---"]
    for m in reversed(matches):
        ch = m.get("channel") or {}
        where = f"#{ch['name']}" if ch.get("name") and not ch.get("is_im") else ("DM" if ch.get("is_im") else ch.get("name") or ch.get("id", "?"))
        when = datetime.fromtimestamp(float(m.get("ts", 0))).strftime("%Y-%m-%d")
        who = slack.me_name + " (me)" if m.get("user") == slack.me else (m.get("username") or slack.name_of(m.get("user", "?")))
        lines.append(f"[{when} {where}] {who}: {clean_text(m.get('text', ''), slack.name_of)[:300]}")
        if m.get("permalink"):
            lines.append(f"    {m['permalink']}")
    return "\n".join(lines)


def about(slack, uid: str, ledger=None, followups=None, exchanges: int = 20) -> str:
    try:
        u = slack.client.users_info(user=uid)["user"]
    except Exception:
        u = {}
    prof = u.get("profile") or {}
    name = prof.get("display_name") or u.get("real_name") or u.get("name") or uid
    out = [f"=== {name} (@{u.get('name', '?')}, {uid}) ===",
           " · ".join(x for x in [prof.get("title"), u.get("tz_label"), ("admin" if u.get("is_admin") else None),
                                 (f"status: {prof['status_text']}" if prof.get("status_text") else None)] if x) or "(no profile details)"]
    d = load_dossier("people", uid)
    out += ["", "--- dossier ---", d or "(none yet — add with: slackctl note @handle \"…\")"]
    if ledger is not None:
        s = ledger.stats(sender=uid)
        mode, why = ledger.effective_mode("reply", uid, None, config.CFG["mode"])
        out += ["", f"--- track record --- {s} → replies to them currently go {mode.upper()} ({why})"]
        cs = [c for c in ledger.corrections(50) if c.get("sender") == uid][-3:]
        for c in cs:
            out.append(f"  [{c['outcome']}] bot: {(c.get('draft') or '')[:120]} | you: {(c.get('actual') or '')[:120]}")
    if followups is not None:
        opens = [f for f in followups.open() if f.counterpart == uid]
        if opens:
            out += ["", "--- open promises ---"] + [f"  '{f.phrase}' (due {f.due}) re {f.summary[:80]}" for f in opens]
    try:
        ch = dm_channel(slack, uid)
        out += ["", history(slack, ch, None, exchanges)]
    except Exception as e:
        out += ["", f"(couldn't read DM history: {e})"]
    return "\n".join(out)
