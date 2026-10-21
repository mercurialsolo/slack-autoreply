"""Your voice: hand-written notes (VOICE.md) + your real messages (voice_samples.json).

build_samples() pulls your past messages from Slack search, with the message
each one replied to where it can find it. The daemon keeps the file fresh by
appending every message you send yourself. pick_samples() chooses which ones
go into a given prompt: same conversation first, then same person, then a
random spread, oldest → newest.
"""
from __future__ import annotations

import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

import config

VOICE_FILE = config.p("VOICE.md")
SAMPLES_FILE = config.p("voice_samples.json")

VOICE_TEMPLATE = """# How I write on Slack

<!-- Read into every prompt. The skill's `review` mode proposes edits here from your corrections;
     you can edit it by hand any time. Keep it short and concrete. -->

**Length.** Short. One to three lines. Longer only when someone asked a real question that needs a real answer.

**Case and punctuation.** (lowercase? full stops at the end? exclamation marks ever?)

**Openers I never use.** "Hi X!", "Hope you're well", "Great question", "Thanks for reaching out".

**Words and phrases I actually use.**

**Emoji.** (which ones, how often, as reactions vs inline)

**When I don't know.** "let me check and come back to you" — never guess.

**Things I never commit to without thinking.** Dates, prices, intros, approvals, "yes" on anything with money or people attached. Say I'll come back on it.

**People to be careful with.** (investors, customers, anyone where a wrong reply costs something)

**Things I'm fine replying to on autopilot.** Scheduling, "did you see X", quick factual stuff that's already in the thread.
"""


def load_notes() -> str:
    return VOICE_FILE.read_text(encoding="utf-8").strip() if VOICE_FILE.exists() else ""


def ensure_voice_file() -> bool:
    """Create the template if missing. Returns True if it was created."""
    if VOICE_FILE.exists():
        return False
    VOICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    VOICE_FILE.write_text(VOICE_TEMPLATE, encoding="utf-8")
    return True


def load_samples() -> list[dict]:
    if not SAMPLES_FILE.exists():
        return []
    try:
        data = json.loads(SAMPLES_FILE.read_text(encoding="utf-8"))
        return [s for s in data if isinstance(s, dict) and s.get("text")]
    except json.JSONDecodeError:
        return []


def save_samples(samples: list[dict]) -> None:
    SAMPLES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SAMPLES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(samples, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SAMPLES_FILE)


def append_sample(sample: dict, cap: int) -> None:
    """Used by the daemon: a message you sent yourself is the freshest voice data there is."""
    samples = load_samples()
    if any(s.get("ts") == sample.get("ts") and s.get("channel") == sample.get("channel") for s in samples):
        return
    samples.append(sample)
    samples.sort(key=lambda s: float(s.get("ts", 0)))
    save_samples(samples[-cap:])


def revise_sample(channel: str, ts: str, text: str) -> bool:
    """You edited a message after sending it: the edit is what you meant, so the sample carries it."""
    samples = load_samples()
    for s in samples:
        if s.get("ts") == ts and s.get("channel") == channel:
            s["text"] = text
            save_samples(samples)
            return True
    return False


def pick_samples(samples: list[dict], channel: str | None = None, counterpart: str | None = None,
                 max_samples: int = 40, max_chars: int = 14000) -> list[dict]:
    same_conv, same_person, rest = [], [], []
    for s in samples:
        if channel and s.get("channel") == channel:
            same_conv.append(s)
        elif counterpart and s.get("reply_to_user") == counterpart:
            same_person.append(s)
        else:
            rest.append(s)
    random.shuffle(rest)
    same_conv.sort(key=lambda s: float(s.get("ts", 0)), reverse=True)
    same_person.sort(key=lambda s: float(s.get("ts", 0)), reverse=True)
    ordered = same_conv[: max(1, max_samples // 2)] + same_person[: max(1, max_samples // 4)] + rest
    out, used = [], 0
    for s in ordered:
        if len(out) >= max_samples:
            break
        n = len(s.get("text", "")) + len(s.get("prompt") or "")
        if used + n > max_chars:
            continue
        out.append(s)
        used += n
    out.sort(key=lambda s: float(s.get("ts", 0)))
    return out


def format_samples(samples: list[dict]) -> str:
    blocks = []
    for s in samples:
        attrs = ""
        if s.get("ts"):
            attrs += f' date="{datetime.fromtimestamp(float(s["ts"])).strftime("%Y-%m-%d")}"'
        if s.get("channel_name"):
            attrs += f' where="#{s["channel_name"]}"'
        if s.get("prompt"):
            blocks.append(f"<example{attrs}>\n<them>{s['prompt']}</them>\n<me>{s['text']}</me>\n</example>")
        else:
            blocks.append(f"<example{attrs}>\n<me>{s['text']}</me>\n</example>")
    return "\n".join(blocks) if blocks else "(no samples yet — run: slackctl build-voice)"


# ------------------------------------------------------------- building

SKIP_SUBTYPES = {"channel_join", "channel_leave", "bot_message", "message_changed", "message_deleted"}


def usable(text: str) -> bool:
    t = text.strip()
    if len(t) < 2 or len(t) > 1500:
        return False
    if t.startswith("<http") and " " not in t:
        return False
    if re.fullmatch(r"(:[a-z0-9_+-]+:\s*)+", t):
        return False
    return True


def thread_ts_from(permalink: str | None) -> str | None:
    m = re.search(r"[?&]thread_ts=([0-9.]+)", permalink or "")
    return m.group(1) if m else None


def previous_message(client, me: str, channel: str, ts: str, thread_ts: str | None) -> dict | None:
    from slack_sdk.errors import SlackApiError
    try:
        if thread_ts:
            msgs = client.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
            idx = next((i for i, m in enumerate(msgs) if m.get("ts") == ts), None)
            if idx is None or idx == 0:
                return None
            prev = msgs[idx - 1]
        else:
            msgs = client.conversations_history(channel=channel, latest=ts, inclusive=False, limit=1)["messages"]
            if not msgs:
                return None
            prev = msgs[0]
    except SlackApiError as e:
        print(f"\n  skip {channel}/{ts}: {e.response.get('error')}", file=sys.stderr)
        return None
    if prev.get("subtype") in SKIP_SUBTYPES or prev.get("user") in (None, me) or prev.get("bot_id"):
        return None
    text = (prev.get("text") or "").strip()
    return {"text": text, "user": prev.get("user")} if text else None


def build_samples(client, me: str, handle: str, count: int = 200, with_context: bool = True) -> list[dict]:
    samples: list[dict] = []
    for query in (f"from:<@{me}>", f"from:@{handle}"):
        page = 1
        while len(samples) < count:
            res = client.search_messages(query=query, sort="timestamp", sort_dir="desc", count=100, page=page)
            block = res["messages"]
            matches = block.get("matches", [])
            if not matches:
                break
            for m in matches:
                text = (m.get("text") or "").strip()
                if m.get("user") != me or not usable(text):
                    continue
                ch = m.get("channel") or {}
                samples.append({"text": text, "channel": ch.get("id"), "channel_name": ch.get("name"),
                                "ts": m.get("ts"), "thread_ts": thread_ts_from(m.get("permalink"))})
                if len(samples) >= count:
                    break
            if page >= block.get("paging", {}).get("pages", 1):
                break
            page += 1
        if samples:
            break
    if samples and with_context:
        print(f"fetching what each of {len(samples)} messages replied to (rate-limited, a few minutes)…")
        for i, s in enumerate(samples, 1):
            prev = previous_message(client, me, s["channel"], s["ts"], s.get("thread_ts"))
            if prev:
                s["prompt"] = prev["text"]
                s["reply_to_user"] = prev["user"]
            print(f"\r  {i}/{len(samples)}", end="", flush=True)
        print()
    return samples
