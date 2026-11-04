"""How Solo tells the owner something: a headline with a status icon, a few plain bullets, and the
technical detail behind a thread reply.

The owner, 2026-11-04: "messages are not well formatted here for readability … use icons, emojis and
language that is more human readable rather than just technical details." A release note arrived as
one paragraph of shas, metric names and millisecond counts. This module is the one place a note to
the owner gets its shape, so every agent's note reads the same way:

    🔴 *Helsinki* is not on the build it should be (a rollback, or a deploy by hand)
    • ⏸️ *Helsinki* held at *v0.551.0*: the no-regression gate was red: reply time 1.2 s vs 0.8 s usually
    • ⏸️ *Hillsboro* frozen at *v0.537.0*
    _from solo-one · details in the thread_

Nothing is lost: the agent's words go, whole and unedited, into a "Details" reply in the message's
thread, which is where the 31 Oct sysctl command would still be. The rendering is deterministic, with
no writer and no model: a note to the owner must not depend on a quota (27, 29 and 31 Oct).

An agent can say exactly what it means (`say --status held --headline … --point …`); a note that
arrives as free text is read for its status, split into clauses and cleaned up as well as rules can.
Pure functions only: Slack calls stay in slackctl.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Most severe first: a message that is several things takes the icon of the worst.
STATUSES = ("ask", "failed", "warn", "held", "shipping", "ok", "info")
ICONS = {"ask": "❓", "failed": "🔴", "warn": "⚠️", "held": "⏸️", "shipping": "🚀", "ok": "✅", "info": "📨"}
ALIASES = {
    "needs-you": "ask", "question": "ask", "decide": "ask",
    "red": "failed", "fail": "failed", "rollback": "failed", "rolled-back": "failed", "error": "failed",
    "warning": "warn", "degraded": "warn",
    "hold": "held", "frozen": "held", "freeze": "held", "paused": "held",
    "ship": "shipping", "deploying": "shipping", "releasing": "shipping",
    "green": "ok", "live": "ok", "pass": "ok", "passed": "ok", "done": "ok",
    "fyi": "info", "note": "info",
}
KEYWORDS = (
    ("ask", re.compile(r"\bneeds? you\b|\byour (call|ok|go-ahead|decision)\b|\bwaiting (on|for) you\b|"
                       r"\bneeds? .{1,60}\bfrom you\b|^\s*needs?\b|\?\s*$", re.I)),
    ("failed", re.compile(r"\broll(ed|ing)?[ -]?back\b|\brollback\b|\bfail(ed|s|ing|ure)?\b|\bred\b|\bnot live\b|"
                          r"\bbroken\b|\bis down\b|\bcrash(ed|ing)?\b|\berrors?\b", re.I)),
    ("warn", re.compile(r"\bwarn(ing)?s?\b|\bdegraded\b|\bslow(er)?\b|\bflaky\b|\bretr(y|ied|ying)\b", re.I)),
    ("held", re.compile(r"\bheld\b|\bon hold\b|\bholding\b|\bfrozen\b|\bfreeze\b|\bpaused\b", re.I)),
    ("shipping", re.compile(r"\bshipping\b|\bdeploying\b|\brolling out\b|\breleasing\b|\bin progress\b", re.I)),
    ("ok", re.compile(r"\blive\b|\bgreen\b|\bpass(ed|es)?\b|\bshipped\b|\bmerged\b|\bdone\b|\bdeployed\b", re.I)),
)

# Metric names an agent uses that a person should not have to decode. Anything not listed is
# turned into words by dropping the unit and percentile suffixes and the underscores.
GLOSSARY = {
    "reply_heard_p50_ms": "reply time",
    "reply_heard_p95_ms": "slowest replies",
    "first_token_ms": "time to first word",
    "error_rate": "error rate",
}
# Release jargon, in the words a person would use.
PHRASES = (("no-regression gate", "regression check"), ("smoke run", "test run"), ("smoke test", "test run"),
           ("resample", "re-run"))
WORDS_STOP = {"The", "It", "This", "That", "CI", "PR", "Solo", "Not", "Live", "Now", "And", "But", "So", "Then", "Gate"}
BOX_VERBS = r"(?:held|frozen|runs|rolled back|stays|is|was|shipped|shipping|deployed|live|green|red|on v|at v|back on)"
BOX_RE = re.compile(r"(?<![*\w])([A-Z][a-z]{2,})(?=\s+" + BOX_VERBS + r"\b)")
VERSION_RE = re.compile(r"(?<![*\w.])(v\d+(?:\.\d+){1,3})(?![\w*])")
SHA = r"(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}"
SHA_RE = re.compile(r"\b" + SHA + r"\b")
RUNS_NOT_RE = re.compile(r"\b(\w+) (?:runs|is on) " + SHA + r",? (?:not|instead of|expected) " + SHA + r"\b")
PATH_RE = re.compile(r"(?<![\w<|])(?:~|/(?:Users|home|var|tmp|private|opt|etc))/[^\s,;)]+|(?<![\w<|/])[\w.\-/]+\.(?:log|jsonl)\b")
METRIC_RE = re.compile(r"(?<![\w.\-/`])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\s*:\s*(?=[-+]?\d)")
MS_RE = re.compile(r"([-+]?)(\d+(?:\.\d+)?)\s?ms\b")
COMMAND_RE = re.compile(r"`|\b(?:sudo|ssh|colima|kubectl|curl|make|git|solo|python3?|npm|brew|launchctl|factory)\s+\S")
LABEL_RE = re.compile(r"^\s*(?:not live(?: now)?|live(?: now)?|status|update|fyi|note|summary|tl;?dr)\s*:\s*", re.I)
CLAUSE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z❓🔴⚠⏸🚀✅])|;\s+|,\s+(?=[A-Z][a-z]{2,}\s+" + BOX_VERBS + r"\b)")
HEADLINE_MAX = 120
BULLETS_MAX = 3
GROUP_WINDOW_S = 300


@dataclass
class Note:
    status: str
    headline: str
    bullets: list[str] = field(default_factory=list)
    details: str = ""          # whole and unedited; goes in the thread
    options: list[str] = field(default_factory=list)
    link: str = ""
    sender: str = ""


def norm_status(s: str | None) -> str | None:
    s = (s or "").strip().lower()
    if not s:
        return None
    s = ALIASES.get(s, s)
    if s not in STATUSES:
        raise ValueError(f"unknown status {s!r} — one of: {', '.join(STATUSES)}")
    return s


VERB_STATUS = (("held", "held"), ("frozen", "held"), ("rolled back", "failed"), ("red", "failed"),
               ("shipping", "shipping"), ("shipped", "ok"), ("deployed", "ok"), ("live", "ok"), ("green", "ok"))


def status_of(text: str) -> str:
    """What the words say happened: the box's own verb first ("Helsinki held at …" is held, whatever
    its reason mentions), then the most severe status anywhere in them, or info when there is none."""
    m = re.match(r"\W*(?:[\w ]{0,20}:\s*)?(?!(?:Not|Live|Now|The|It|This)\b)[A-Z][a-z]{2,}\s+(held|frozen|rolled back|red|shipping|shipped|deployed|live|green)\b", text or "")
    if m:
        return dict(VERB_STATUS)[m.group(1)]
    for status, rx in KEYWORDS:
        if rx.search(text or ""):
            return status
    return "info"


def worst(statuses) -> str:
    got = [s for s in statuses if s]
    return min(got, key=STATUSES.index) if got else "info"


def _seconds(ms: float) -> str:
    s = ms / 1000
    return f"{s:.1f} s" if s < 10 else f"{s:.0f} s"


def _protect(text: str):
    """Code spans and Slack links are the agent's exact words: hide them from every rewrite."""
    kept: list[str] = []

    def keep(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"
    return re.sub(r"```.*?```|`[^`\n]+`|<[^>\s]+>", keep, text, flags=re.S), kept


def _restore(text: str, kept: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


def _metric_words(name: str) -> str:
    if name in GLOSSARY:
        return GLOSSARY[name]
    parts = [p for p in name.split("_") if not re.fullmatch(r"p\d+|ms|s|pct|count|total|n", p)]
    return " ".join(parts) or name.replace("_", " ")


def _drop_parenthetical(m) -> str:
    inner = m.group(1)
    if re.search(r"\d|[=_/\\]|\x00", inner) or len(inner.split()) > 8:
        return ""
    return m.group(0)


def humanize(text: str) -> str:
    """Plain words for a person: seconds not milliseconds, no shas, no metric names, no log paths.

    A clause that carries a command is left exactly as written — the 31 Oct sysctl line is what the
    owner had to run, and `kernel.…_userns=0` rewritten into words is no longer a command.
    """
    if COMMAND_RE.search(text):
        return text.strip()
    t, kept = _protect(text)
    t = RUNS_NOT_RE.sub(r"\1 is not on the build it should be", t)
    t = SHA_RE.sub("", t)
    t = PATH_RE.sub("", t)

    def metric(m):
        name = m.group(1)
        return f"\x01{_metric_words(name)}\x02{'ms' if name.endswith('_ms') else ''}\x03 "
    t = METRIC_RE.sub(metric, t)

    # Numbers that follow a millisecond metric, up to the end of its clause, are milliseconds too.
    def ms_run(m):
        words, unit, rest = m.group(1), m.group(2), m.group(3)
        if unit:
            rest = re.sub(r"([-+]?)(?<![\w.])(\d{3,}(?:\.\d+)?)(?![\w.])",
                          lambda n: f"{n.group(1)}{_seconds(float(n.group(2)))}", rest)
        return words + " " + rest
    t = re.sub(r"\x01([^\x02]*)\x02(ms)?\x03\s*([^()\x01;]*)", ms_run, t)
    t = MS_RE.sub(lambda m: m.group(1) + (_seconds(float(m.group(2))) if float(m.group(2)) >= 100 else f"{m.group(2)} ms"), t)
    t = re.sub(r"\s*\(([^()]*)\)", _drop_parenthetical, t)
    for a, b in PHRASES:
        t = re.sub(re.escape(a), b, t, flags=re.I)
    t = re.sub(r"\bvs\.? median ([\d.]+ s) of the last \d+", r"vs \1 usually", t)
    t = re.sub(r"\s*->\s*", " → ", t)
    # tidy what the removals left behind
    t = re.sub(r"\(\s*\)|\[\s*\]", "", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    t = re.sub(r"([,;:])(?=[,;:.])", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip(" ,;:")
    t = re.sub(r"[,;:]?\s+(?:see|in|at|from|under|logs? at)$", "", t).rstrip(" ,;:.")
    return _restore(t, kept)


def emphasize(text: str) -> str:
    """mrkdwn bold for the box and the version — the two things the eye looks for."""
    t, kept = _protect(text)
    if not COMMAND_RE.search(text):
        t = BOX_RE.sub(lambda m: m.group(0) if m.group(1) in WORDS_STOP else f"*{m.group(1)}*", t)
        t = VERSION_RE.sub(r"*\1*", t)
    return _restore(t, kept)


def _cut(text: str, n: int = HEADLINE_MAX) -> str:
    if len(text) <= n or COMMAND_RE.search(text):
        return text
    cut = text[:n].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return cut + "…"


def clauses(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•*◦ ").strip()
        if not line:
            continue
        if COMMAND_RE.search(line) and "`" in line:
            out.append(line)
            continue
        out.extend(c.strip() for c in CLAUSE_SPLIT.split(line) if c and c.strip(" .,;"))
    return out


def from_text(intent: str, sender: str = "", *, status: str | None = None, headline: str | None = None,
              points: list[str] | None = None, details: str | None = None, options: list[str] | None = None,
              link: str = "") -> Note:
    """Read a note for the owner into a Note. Whatever the agent said explicitly wins over the rules."""
    intent = (intent or "").strip()
    options = [o.strip() for o in (options or []) if o and o.strip()]
    parts = clauses(intent)
    statuses = [status_of(c) for c in parts]
    if headline:
        head = headline.strip()
        bullets = [p.strip() for p in (points or []) if p and p.strip()]
        st = norm_status(status) or ("ask" if options else worst([status_of(head)] + [status_of(b) for b in bullets]))
        whole = details if details is not None else (intent if intent and intent != head else "")
        return Note(st, head, bullets, whole, options, link, sender)
    st = norm_status(status) or ("ask" if options else worst(statuses))
    if not parts:
        return Note(st, "(empty note)", [], "", options, link, sender)
    # The headline is the worst clause; the rest are bullets, in the order the agent gave them.
    lead = next(i for i, s in enumerate(statuses) if s == worst(statuses))
    cleaned = [LABEL_RE.sub("", humanize(c)) or c for c in parts]
    head = cleaned[lead]
    rest = [(statuses[i], c) for i, c in enumerate(cleaned) if i != lead]
    # A long headline keeps the what; its why becomes the first bullet.
    if len(head) > 70 and not COMMAND_RE.search(head) and ": " in head:
        what, why = head.split(": ", 1)
        if len(what) >= 12:
            head = what
            rest.insert(0, ("info", why[0].upper() + why[1:]))
    bullets = [b.strip() for b in (points or []) if b and b.strip()] or [
        (f"{ICONS[s]} {c}" if s != "info" and len(set(statuses)) > 1 else c) for s, c in rest]
    if len(bullets) > BULLETS_MAX:
        extra = len(bullets) - BULLETS_MAX + 1
        bullets = bullets[: BULLETS_MAX - 1] + [f"…and {extra} more in the thread"]
    plain = " ".join([head] + bullets)
    unchanged = len(parts) <= BULLETS_MAX + 1 and all(c == p for c, p in zip(cleaned, parts)) and len(head) <= HEADLINE_MAX
    whole = details if details is not None else ("" if unchanged and len(plain) >= len(intent) * 0.9 else intent)
    return Note(st, head, bullets, whole, options, link, sender)


def headline_line(note: Note) -> str:
    return f"{ICONS[note.status]} {emphasize(_cut(note.headline))}"


def body_lines(note: Note) -> list[str]:
    return [f"• {emphasize(b)}" for b in note.bullets]


def footer(note: Note, has_details: bool) -> str:
    bits = [f"from {note.sender}"] if note.sender else []
    if has_details:
        bits.append("details in the thread")
    line = "_" + " · ".join(bits) + "_" if bits else ""
    if note.link:
        line = (line + " · " if line else "") + f"<{note.link}|{'answer page' if note.options else 'open the log'}>"
    return line


def render(note: Note, buttons: bool = True) -> dict:
    """{text, blocks, details}: text is the message (and the notification); blocks carry buttons.

    `buttons=False` is for a Slack app whose Interactivity is still off: a press there only shows
    the owner a warning icon. The choices are then named in the message, with the answer page."""
    lines = [headline_line(note)] + body_lines(note)
    if note.options and not buttons:
        lines.append("👉 " + " · ".join(f"*{o}*" for o in note.options)
                     + (f" — <{note.link}|answer here>" if note.link else " — answer in the thread"))
    foot = footer(note, bool(note.details))
    text = "\n".join(lines + ([foot] if foot else []))
    blocks = None
    if note.options and buttons:
        elements = [{"type": "button", "action_id": f"solo_answer_{i}",
                     "text": {"type": "plain_text", "text": o[:75], "emoji": True}, "value": o[:2000],
                     **({"style": "primary"} if i == 0 and o.lower() in ("yes", "ok", "go", "approve") else {})}
                    for i, o in enumerate(note.options[:5])]
        if note.link:
            elements.append({"type": "button", "action_id": "solo_answer_link", "url": note.link,
                             "text": {"type": "plain_text", "text": "Open the answer page", "emoji": True}})
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
                  {"type": "actions", "block_id": "solo_ask", "elements": elements}]
        if foot:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": foot}]})
    details = ""
    if note.details:
        details = "🔎 *Details*\n" + note.details
    return {"text": text, "blocks": blocks, "details": details}


def answered_blocks(blocks: list[dict] | None, choice: str, who: str, at: str) -> list[dict]:
    """The same message once answered: the buttons give way to what was chosen, so it is not chosen twice."""
    kept = [b for b in (blocks or []) if b.get("type") != "actions"]
    return kept + [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"✔️ *{choice}* — answered by {who} at {at}"}]}]


def render_group(notes: list[Note], sender: str = "") -> str:
    """Several notes inside the grouping window, as one message: the worst icon, one line per note."""
    if len(notes) == 1:
        return render(notes[0])["text"]
    st = worst(n.status for n in notes)
    head = f"{ICONS[st]} *{len(notes)} updates*" + (f" from {sender}" if sender else "")
    lines = [f"• {headline_line(n)}" for n in notes]
    if len(lines) > BULLETS_MAX:
        lines = lines[: BULLETS_MAX - 1] + [f"• …and {len(lines) - BULLETS_MAX + 1} more in the thread"]
    return "\n".join([head] + lines + ["_each one's detail is in the thread_"])


def to_dict(note: Note) -> dict:
    return {"status": note.status, "headline": note.headline, "bullets": note.bullets, "details": note.details,
            "options": note.options, "link": note.link, "sender": note.sender}


def from_dict(d: dict) -> Note:
    return Note(d.get("status") or "info", d.get("headline") or "", list(d.get("bullets") or []),
                d.get("details") or "", list(d.get("options") or []), d.get("link") or "", d.get("sender") or "")


if __name__ == "__main__":  # quick look: python3 owner_note.py "text"
    import sys
    r = render(from_text(" ".join(sys.argv[1:]), "you"))
    print(r["text"])
    if r["details"]:
        print("--- thread ---\n" + r["details"])
    if r["blocks"]:
        print(json.dumps(r["blocks"], indent=1, ensure_ascii=False))
