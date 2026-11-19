"""Slack mrkdwn for a note to the owner: what Slack shows is what the agent meant.

The owner, 2026-11-19, on a factory goal-sweep note: a literal `*Done:` (bold never closed), `-&gt`
where an arrow was meant, a doubled ✅ ✅, a bullet holding a whole second message, "…and 3 more in
the thread" as a bullet, and earlier that day a whole report as one raw paragraph in Details.

Pure functions, no Slack calls. `inline` is for a line of the message (owner_note.emphasize runs it
before it adds its own bold); `details` is for the thread reply: `*Section*` headings, one `•` per
fact, short lines, never one dense paragraph. Code spans and Slack links are left exactly as written.
"""
from __future__ import annotations

import html
import re

import note_rules

EMOJI = note_rules.EMOJI
CODE_RE = note_rules.CODE_RE
# Slack's own <…>: a link, a mention, a channel, a special mention. Anything else in <> is text.
SLACK_TOKEN_RE = re.compile(r"<(?:https?://|mailto:|[@#!])[^<>\s|]*(?:\|[^<>\n]*)?>")
BARE_URL_RE = re.compile(r"\bhttps?://[^\s<>|]+[^\s<>|.,;:!?)]")
ARROW_RE = re.compile(r"[ \t]*(?:-{1,2}>|=>|⟶|➔)[ \t]*")
BACK_ARROW_RE = re.compile(r"[ \t]*(?<![<!-])<-{1,2}(?![->\d])[ \t]*")
EMOJI_RUN_RE = re.compile(r"(" + EMOJI + r"[️‍]*)(?:[ \t]*\1)+")
MD_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+(.+?)\s*#*|\*{1,2}([^*\n]{2,60}?):?\*{1,2}:?|([A-Z][^.!?:\n]{1,40}):)\s*$")
# "…and 3 more in the thread", "+2 more": a count of what is not said is not a fact.
MORE_RE = re.compile(r"(?:^|[\s,;(]*(?:\.\.\.|…)?\s*)\b(?:and|plus|\+)?\s*\d+\s+(?:more|others?)\b(?:\s+(?:in|on|are in)\b[^.;)]*)?[.)]?\s*$", re.I)
# Where one bullet holds a second message: an icon or a status label after a sentence or a ";".
SUB_SPLIT_RE = re.compile(r"(?<=[.;!?])\s+(?=[*_]*(?:" + EMOJI + r"|(?:[A-Z][\w-]*(?:\s[\w-]+){0,2}):\s))")
PARA_MAX = 200  # a Details line longer than this, with several sentences, is a list
BULLETS_MAX = 3
PROTECT = re.compile(CODE_RE.pattern + "|" + SLACK_TOKEN_RE.pattern + "|" + BARE_URL_RE.pattern, re.S)


def _protect(text: str, rx=PROTECT, tag: str = "\x00"):
    """Spans of `rx` out of the way as tag-N-tag; each nesting level uses its own tag."""
    kept: list[str] = []

    def keep(m):
        kept.append(m.group(0))
        return f"{tag}{len(kept) - 1}{tag}"
    return rx.sub(keep, text), kept


def _restore(text: str, kept: list[str], tag: str = "\x00") -> str:
    return re.sub(f"{tag}(\\d+){tag}", lambda m: kept[int(m.group(1))], text)


def unescape(t: str) -> str:
    """Entities as the characters they stand for, however many times they were escaped (`&amp;gt;`)."""
    for _ in range(3):
        u = html.unescape(t)
        if u == t:
            break
        t = u
    return t


def escape(t: str) -> str:
    """Plain text (no code or links in it) as Slack mrkdwn: entities read once, "->" an arrow, then
    Slack's three control characters escaped exactly once."""
    t = unescape(t)
    t = ARROW_RE.sub(" → ", t)
    t = BACK_ARROW_RE.sub(" ← ", t)
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def dedup_emoji(t: str) -> str:
    """✅ ✅ reads ✅."""
    return EMOJI_RUN_RE.sub(r"\1", t)


def _balance(line: str, ch: str) -> str:
    c = re.escape(ch)
    pairs, kept = _protect(line, re.compile(rf"(?<![\w{c}]){c}(?=[^\s{c}])[^\n]*?(?<=[^\s{c}]){c}(?![\w{c}])"), "\x01")
    # what is left of the marker is stray: one that opens or closes a word, never one inside a word
    pairs = re.sub(rf"(?<!\w){c}+|{c}+(?!\w)", "", pairs) if ch != "_" else re.sub(rf"(?<![\w{c}]){c}+(?=\w)|(?<=\w){c}+(?![\w{c}])", "", pairs)
    return _restore(pairs, kept, "\x01")


def balance(t: str) -> str:
    """No stray `*`, `_` or `~`: a marker without its pair, which Slack shows as a literal, goes."""
    t = MD_BOLD_RE.sub(lambda m: f"*{m.group(1)}*" if m.group(1) else f"_{m.group(2)}_", t)
    out = []
    for line in t.split("\n"):
        for ch in "*_~":
            line = _balance(line, ch)
        out.append(line)
    return "\n".join(out)


def plain(t: str) -> str:
    """Text whose code and links the caller already hid: escaped, arrows, one icon, balanced markup."""
    return balance(dedup_emoji(escape(t)))


def inline(text: str) -> str:
    """One line or two of the message, made safe for mrkdwn; code and links left as written."""
    t, kept = _protect(text or "")
    return _restore(plain(t), kept)


def split_item(item: str) -> list[str]:
    """One bullet that holds several messages, as one fact each; "and N more" is not one."""
    out = []
    for part in SUB_SPLIT_RE.split(item.strip()):
        part = MORE_RE.sub("", part).strip(" ;,")
        if re.search(r"[A-Za-z]", part):
            out.append(part)
    return out


def bullets(items: list[str], cap: int | None = BULLETS_MAX) -> list[str]:
    """Items as complete facts: split where one held several, counts of the rest dropped, at most `cap`."""
    facts = [f for it in items for f in split_item(it or "")]
    return facts[:cap] if cap else facts


def _sentences(line: str) -> list[str]:
    t, kept = _protect(line)
    return [_restore(s, kept).strip() for s in note_rules.SENTENCE_SPLIT.split(t) if s.strip()]


def details(text: str) -> str:
    """The thread reply: `*Section*` headings, one `•` per fact, a long paragraph split into its
    sentences, entities and markup fixed. Code blocks are left exactly as written."""
    out: list[str] = []
    t, fences = _protect(text or "", re.compile(r"```.*?```", re.S), "\x02")
    for raw in t.split("\n"):
        line = raw.strip()
        if not line:
            if out and out[-1]:
                out.append("")
            continue
        if re.fullmatch(r"\x02\d+\x02", line):
            out.append(line)
            continue
        h = HEADING_RE.match(line)
        if h and not note_rules.LIST_ITEM_RE.match(line):
            out.append("*" + inline(next(g for g in h.groups() if g)).strip("*_ ") + "*")
            continue
        if note_rules.LIST_ITEM_RE.match(line):
            out.extend("• " + inline(f) for f in split_item(note_rules.LIST_ITEM_RE.sub("", line)))
            continue
        sents = _sentences(line)
        if len(line) > PARA_MAX and len(sents) > 1:
            out.extend("• " + inline(f) for s in sents for f in split_item(s))
            continue
        out.append(inline(line))
    return _restore("\n".join(out).strip("\n"), fences, "\x02")
