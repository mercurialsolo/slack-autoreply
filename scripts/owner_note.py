"""How Solo tells the owner something: an icon headline, one or two plain sentences, at most one
line asking what is needed, and the agent's words whole in a Details reply in the thread.

The owner, 2026-11-10, after a budget note full of task ids and counters and a release note of
cut-off bullets and bare URLs, set the rule (note_rules.RULE). This is the one renderer every note
to the owner goes through, so they all read it:

    ❓ Retro: wire review noise tripwire into reviewer briefs.
    The task hit its budget again after one operator grant.
    👉 Grant more or stop it? — <https://…/ask/…|Answer here>
    _from factory · details in the thread_

Three ways in, one way out: an agent's explicit `--headline/--point/--link`, a model's reading of
free text (note_shape), or — with no model, no quota, no key — the rules below. Whichever it was, the
parts are cleaned to the rule (note_rules) before they render. Nothing is lost: what the body leaves
out (ids, commands, the full list) is in Details. Pure functions only: Slack calls stay in slackctl.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import note_rules
from note_rules import COMMAND_RE

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
CLAUSE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z❓🔴⚠⏸🚀✅])|;\s+|,\s+(?=[A-Z][a-z]{2,}\s+" + BOX_VERBS + r"\b)")
HEADLINE_MAX = 100  # longer, and the headline keeps its what; the why opens the summary
SUMMARY_MAX = 2  # sentences under the headline
SECTION_MAX = 2900  # Slack's limit on a section block's text is 3000
GROUP_WINDOW_S = 300


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


_status_of = status_of  # from_text reads a piece's status before its "Failed:" label goes


def worst(statuses) -> str:
    got = [s for s in statuses if s]
    return min(got, key=STATUSES.index) if got else "info"


def _seconds(ms: float) -> str:
    s = ms / 1000
    return f"{s:.1f} s" if s < 10 else f"{s:.0f} s"


def _protect(text: str):
    """Code spans, Slack links and bare URLs are the agent's exact words: hide them from every rewrite."""
    kept: list[str] = []

    def keep(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"
    return re.sub(r"```.*?```|`[^`\n]+`|<[^>\s|]+(?:\|[^>\n]*)?>|\bhttps?://[^\s<>|]+[^\s<>|.,;:!?)]", keep, text, flags=re.S), kept


def _restore(text: str, kept: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


def _metric_words(name: str) -> str:
    if name in GLOSSARY:
        return GLOSSARY[name]
    parts = [p for p in name.split("_") if not re.fullmatch(r"p\d+|ms|s|pct|count|total|n", p)]
    return " ".join(parts) or name.replace("_", " ")


def _drop_parenthetical(m) -> str:
    """A bracket keeps only its plain words: "(5 behind v0.773.0, the canary holds it)" reads
    "(the canary holds it)"; one that is all numbers, names or paths goes."""
    inner = m.group(1)
    if len(inner.split()) > 8:
        return ""
    keep = [p for p in re.split(r",\s+", inner) if p.strip() and not re.search(r"\d|[=_/\\]|\x00", p)]
    if len(keep) == len(re.split(r",\s+", inner)):
        return m.group(0)
    return f" ({', '.join(keep)})" if keep else ""


def humanize(text: str) -> str:
    """Plain words for a person: seconds not milliseconds, no shas, no metric names, no log paths.
    Code spans and links are left exactly as written (a command is note_rules' to take out whole)."""
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
            # Only the reading and what it is set against (`vs 824`, `median 824`, `-> 900`) are
            # milliseconds; "the last 200 runs" and "build 1234" are counts, left as they are.
            rest = re.sub(r"(^\s*|\bvs\.?\s+(?:median\s+)?|\bmedian\s+|->\s*|→\s*|\bto\s+|\bfrom\s+|,\s*)"
                          r"([-+]?)(?<![\w.])(\d{3,}(?:\.\d+)?)(?![\w.])(?!\s+(?:runs?|calls?|samples?|times|items?|builds?)\b)",
                          lambda n: f"{n.group(1)}{n.group(2)}{_seconds(float(n.group(3)))}", rest)
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
    t = re.sub(r"[,;:]?\s+(?:see|in|at|from|under|logs? at)$", "", t).rstrip(" ,;:")
    return _restore(t, kept)


def _escape(t: str) -> str:
    """Slack's three control characters, as entities (an entity already written stays as it is)."""
    return re.sub(r"&(?!(?:amp|lt|gt);)", "&amp;", t).replace("<", "&lt;").replace(">", "&gt;")


def emphasize(text: str) -> str:
    """mrkdwn bold for the box and the version — the two things the eye looks for."""
    t, kept = _protect(text)
    t = _escape(t)
    if not COMMAND_RE.search(text):
        t = BOX_RE.sub(lambda m: m.group(0) if m.group(1) in WORDS_STOP else f"*{m.group(1)}*", t)
        t = VERSION_RE.sub(r"*\1*", t)
    return _restore(t, kept)




@dataclass
class Note:
    status: str
    headline: str
    summary: str = ""          # one or two plain sentences under the headline
    ask: str = ""              # the one question, when the owner has to decide
    links: list = field(default_factory=list)  # [(url, label)], at most two
    details: str = ""          # whole and unedited; goes in the thread
    options: list[str] = field(default_factory=list)
    sender: str = ""
    reshape: bool = False      # a rule could not clean a part without leaving a hole: a model should word it

    @property
    def link(self) -> str:
        """The answer page: the link labelled so, or with choices to make, the first link."""
        for url, label in self.links:
            if label == "Answer here":
                return url
        return self.links[0][0] if self.options and self.links else ""


LIST_ITEM_RE = re.compile(r"^\s*(?:[-•*◦]|\d+[.)])\s+")
THEME_RE = re.compile(r"^\s*\**([^:*]{2,40}?)\**\s*:\s+\S")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z*❓🔴⚠⏸🚀✅\"'])")
CMD_NOTE = "The command is in the thread."
REPLY_LINE_RE = re.compile(r"^\W*(?:please\s+)?(?:reply|respond|answer)\b[^?]{0,30}\b(?:yes|no|ok|approve|go)\b[^?]*$", re.I)


def lead_off(text: str) -> str:
    """A headline without the icon and status word the sender put first ("✅ Done: …"): the
    renderer adds the one icon, which already says it. Each is taken off once, at the start."""
    return note_rules.STATUS_LABEL_RE.sub("", note_rules.ICON_RE.sub("", text or ""), count=1)


def clean(piece: str) -> tuple[str, list, bool, bool]:
    """One piece of a note, to the rule: (text, links taken out of it, whether a command went,
    whether the scrub left a hole). A piece the scrub would leave with a hole ("set =off in  on the
    call box") comes back empty: it is a model's to word (note_shape), the agent's words are in Details."""
    t, links = note_rules.take_links(lead_off(piece))
    t = note_rules.LABEL_RE.sub("", t.strip())
    was = note_rules.tidy(t)
    t, had_cmd = note_rules.strip_machine(t)
    t = note_rules.tidy(humanize(t))
    if note_rules.COUNTER_RE.search(piece or "") and len(re.findall(r"[A-Za-z]+", t)) <= 4:
        t = ""  # a line of counters with the numbers gone says nothing
    if t and note_rules.holes(t) and not note_rules.holes(was):
        return "", links, had_cmd, True
    return (t if re.search(r"[A-Za-z]", t) else ""), links, had_cmd, False


DANGLING_RE = re.compile(r"[\s,;:]+(?:via|in|on|at|with|into|to|from|by|of|using|and|or)$", re.I)


def option_words(option: str) -> str:
    """A choice as the words for the ask line: its plain meaning without the command or path
    ("yes, then I board it via dev/pr-merge.sh 2708" reads "yes, then I board it"). The button keeps
    the choice exactly as the agent wrote it."""
    t = note_rules.tidy(humanize(note_rules.strip_machine(note_rules.take_links(option)[0])[0]))
    t = note_rules.tidy(PATH_RE.sub("", re.sub(r"\S*/\S+", "", t)))
    while DANGLING_RE.search(t) or re.search(r"\s\d+$", t):  # what the command left: "via", its argument
        t = re.sub(r"\s+\d+$", "", DANGLING_RE.sub("", t)).strip()
    t = t.strip(" ,;:")
    if note_rules.holes(t):
        t = re.split(r",\s*", t, 1)[0]
    return t or option.split(",")[0].strip()


def choice_words(options: list[str]) -> list[str]:
    """The choices as words for the ask line and the buttons. If the plain words would make two
    choices read the same ("Option 1", "Option 2" both "Option"), each keeps the agent's words."""
    words = [option_words(o) for o in options]
    return words if len({w.lower() for w in words}) == len(words) else list(options)


def _and(words: list[str], conj: str = "and") -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + f" {conj} " + words[-1]


def _fold(header: str, items: list[str]) -> str:
    """A list, as the sentence the owner asked for: "What got better: speed, search and alerts (the
    full list is in the thread)". Items name their theme ("Search: …"); a list that does not is left
    to Details."""
    themes = [m.group(1).strip() for m in (THEME_RE.match(i) for i in items) if m]
    if not header or not themes:
        return ""
    themes = [t if re.match(r"[A-Z]{2}|[A-Z][a-z]*[A-Z]", t) else t[:1].lower() + t[1:] for t in themes[:3]]
    return f"{header.rstrip(' :')}: {_and(themes)} (the full list is in the thread)"


def _pieces(intent: str) -> list[str]:
    """Free text as the clauses that could be said: icons off, lists folded, link lines kept as links."""
    out, lines, i = [], [note_rules.ICON_RE.sub("", ln).rstrip() for ln in (intent or "").splitlines()], 0
    while i < len(lines):
        ln = lines[i].strip()
        if not ln:
            i += 1
            continue
        if LIST_ITEM_RE.match(ln) or ln.endswith(":") and i + 1 < len(lines) and LIST_ITEM_RE.match(lines[i + 1]):
            header = ln if ln.endswith(":") and not LIST_ITEM_RE.match(ln) else ""
            j, items = i + (1 if header else 0), []
            while j < len(lines) and LIST_ITEM_RE.match(lines[j]):
                items.append(LIST_ITEM_RE.sub("", lines[j]).strip())
                j += 1
            folded = _fold(header, items)
            out.append(folded) if folded else out.extend("\x00" + x for x in items)  # \x00: only if nothing else
            i = j
            continue
        out.extend(c.strip() for c in CLAUSE_SPLIT.split(ln) if c and c.strip(" .,;"))
        i += 1
    plain = [p for p in out if not p.startswith("\x00")]
    return plain if plain else [p[1:] for p in out]




def _split_long(head: str) -> tuple[str, str]:
    """A long headline keeps its what; the why opens the summary. Never cut with an ellipsis."""
    if len(head) <= HEADLINE_MAX:
        return head, ""
    if ": " in head and len(head.split(": ", 1)[0]) >= 12:
        what, why = head.split(": ", 1)
        return what, note_rules.sentence(why[:1].upper() + why[1:])
    parts = SENTENCE_SPLIT.split(head, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (head, "")


def _summary(parts: list[str]) -> str:
    text = " ".join(note_rules.sentence(p) for p in parts if p)
    return " ".join(SENTENCE_SPLIT.split(text)[:SUMMARY_MAX]) if text else ""


def _norm(t: str) -> list[str]:
    """The words of a text, in any order: does the body already say all of it?"""
    return sorted(re.findall(r"[a-z0-9]+", (t or "").lower()))


def from_text(intent: str, sender: str = "", *, status: str | None = None, headline: str | None = None,
              points: list[str] | None = None, details: str | None = None, options: list[str] | None = None,
              link: str = "", shaped: dict | None = None) -> Note:
    """Read a note for the owner into a Note, to the rule. What the agent said explicitly wins over
    a model's reading (`shaped`, from note_shape), which wins over the rules' own reading."""
    intent = (intent or "").strip()
    options = [o.strip() for o in (options or []) if o and o.strip()]
    given = [(link, note_rules.label_for(link, "answer" if options else ""))] if link else []
    had_cmd, taken = False, []

    holed, said = False, {}

    def c(piece):
        nonlocal had_cmd, holed
        t, ls, h, hole = clean(piece)
        label = (piece or "")[:len(piece or "") - len(lead_off(piece))]  # "Failed: …" says failed, after "Failed:" goes
        said[t] = worst([said.get(t), _status_of(label) if label.strip() else None])
        taken.extend(ls)
        had_cmd |= h and "one command" in t
        holed |= hole
        return t

    def read_status(x):  # what the piece's label said before it went, or what is left says
        return worst([said.get(x), _status_of(x)])

    asks = ""
    if headline:
        points = [p.strip() for p in (points or []) if p and p.strip()]
        head, more = _split_long(c(headline))
        body = [more] + [c(p) for p in points[:SUMMARY_MAX]]
        if not head:  # the headline could not be cleaned: the first plain point stands in for it
            head, body = next(((b, body[:i] + body[i + 1:]) for i, b in enumerate(body) if b), ("", body))
        st = norm_status(status) or ("ask" if options else worst([read_status(head)] + [read_status(b) for b in body if b]))
        extra = points[SUMMARY_MAX:]
        whole = details if details is not None else _beyond(intent, [headline] + points)
        if extra and not all(x in whole for x in extra):
            whole = "\n".join(x for x in [whole] + extra if x)  # the points the body had no room for
        if had_cmd:  # the body says the command is in the thread: make sure it is
            missing = [p for p in [headline] + points if p and COMMAND_RE.search(p) and p not in whole]
            whole = "\n".join(x for x in [whole] + missing if x)
    elif shaped:
        head, more = _split_long(c(shaped.get("headline") or ""))
        body = [more, c(note_rules.flat(shaped.get("summary") or ""))]
        asks = c(shaped.get("ask") or "")  # the model's question; the choices are the options'
        taken[:0] = [(u, lb) for u, lb in shaped.get("links") or []]
        st = norm_status(status) or ("ask" if options else norm_status(shaped.get("status")) or "info")
        if not head:
            return from_text(intent, sender, status=status, details=details, options=options, link=link)
        whole = details if details is not None else intent
    else:
        cleaned = [x for x in (c(p) for p in _pieces(intent)) if x]
        # "Reply yes or no" is the old way to ask; the question before it is the ask, never the headline
        if options or any(x.endswith("?") for x in cleaned):
            cleaned = [x for x in cleaned if not REPLY_LINE_RE.match(x)] or ([] if options else cleaned)
        if not cleaned and holed:  # all of it was a hole: the note is a model's to word
            note = Note(norm_status(status) or ("ask" if options else "info"), "", links=note_rules.merge_links(given, taken),
                        details=details if details is not None else intent, options=options, sender=sender, reshape=True)
            return note
        if not cleaned:
            st = norm_status(status) or ("ask" if options else "info")
            # Only counters, no words around them ("Canary green: 5/5"): the counter is the news.
            raw = note_rules.tidy(note_rules.TASK_ID_RE.sub("", note_rules.take_links(intent)[0]))
            head = raw if re.search(r"[A-Za-z]", raw) and len(raw) <= 120 else "(empty note)"
            return Note(st, head, links=note_rules.merge_links(given, taken), details=intent,
                        options=options, sender=sender)
        ask_i = max((i for i, x in enumerate(cleaned) if x.endswith("?")), default=None)
        asks = cleaned[ask_i] if ask_i is not None else ""
        rest = [x for i, x in enumerate(cleaned) if i != ask_i]
        statuses = [read_status(x) for x in cleaned]
        if rest:
            rs = [read_status(x) for x in rest]
            lead = rs.index(worst(rs))
            head, others = rest[lead], rest[:lead] + rest[lead + 1:]
        else:
            head, others, asks = asks, [], ""
        head, more = _split_long(head)
        body = [more] + others
        st = norm_status(status) or ("ask" if options else worst(statuses))
        whole = details if details is not None else intent
    summary = _summary([b for b in body if b])
    if had_cmd and CMD_NOTE not in summary:
        summary = (summary + " " + CMD_NOTE).strip()
    note = Note(st, head, summary, note_rules.sentence(asks) if asks else "", note_rules.merge_links(given, taken),
                whole, options, sender, reshape=holed or not head or len(head) > HEADLINE_MAX)
    if details is None and set(_norm(note.details)) <= set(_norm(" ".join([head, summary, asks]))):
        note.details = ""  # the body already says all of it: no thread
    return note


def _beyond(intent: str, said: list[str]) -> str:
    """What the agent's text says that its headline and points do not: that, and only that, is
    Details. The same words twice — the body, then the body again in the thread — is noise."""
    said_words = set(_norm(" ".join(said)))
    lines = [ln for ln in (intent or "").splitlines() if ln.strip()]
    rest = [ln for ln in lines if not set(_norm(note_rules.take_links(ln)[0])) <= said_words]
    return "\n".join(rest) if rest else ""


def _label(label: str) -> str:
    return re.sub(r"[<>|*_~`]", "", label or "").strip() or "Open it"


def ask_line(note: Note, buttons: bool = True) -> str:
    q, answer = emphasize(note.ask), note.link
    if note.options and buttons and not q and not note.headline.endswith("?"):
        # no question in plain words: the choices, as words, are the question; the buttons answer it
        q = _escape(note_rules.sentence(_and(choice_words(note.options), "or")).rstrip(".") + "?")
    if note.options and not buttons:
        choices = _and([f"*{_escape(w)}*" for w in choice_words(note.options)], "or")
        q = f"{q} {choices}".strip()
        return f"👉 {q} — " + (f"<{answer}|Answer here>" if answer else "answer in the thread")
    if q and answer and not (note.options and buttons):
        return f"👉 {q} — <{answer}|Answer here>"
    return f"👉 {q}" if q else ""


def footer(note: Note, has_details: bool) -> str:
    bits = [f"from {note.sender}"] if note.sender else []
    if has_details:
        bits.append("details in the thread")
    return "_" + " · ".join(bits) + "_" if bits else ""


def body_lines(note: Note, buttons: bool = True) -> list[str]:
    lines = [f"{ICONS[note.status]} {emphasize(lead_off(note.headline))}"]
    if note.summary:
        lines.append(emphasize(note.summary))
    q = ask_line(note, buttons)
    if q:
        lines.append(q)
    shown = {note.link} if q and note.link and "|Answer here>" in q or note.options and buttons else set()
    rest = [(u, lb) for u, lb in note.links if u not in shown]
    if rest:
        lines.append(" · ".join(f"<{u}|{_label(lb)}>" for u, lb in rest))
    return lines


def _fit(section: str, n: int = SECTION_MAX) -> str:
    """Past Slack's 3000 characters the whole message is refused: end at a line or a sentence, never "…"."""
    if len(section) <= n:
        return section
    head = section[:n]
    cut = max(head.rfind("\n"), max((m.end() for m in re.finditer(r"[.!?](?=\s)", head)), default=-1))
    return head[:cut].rstrip() if cut > 0 else head.rsplit(" ", 1)[0]


def render(note: Note, buttons: bool = True) -> dict:
    """{text, blocks, details}: text is the message (and the notification); blocks carry buttons.

    `buttons=False` is for a Slack app whose Interactivity is still off: a press there only shows
    the owner a warning icon. The choices are then named in the message, with the answer page."""
    lines = body_lines(note, buttons)
    foot = footer(note, bool(note.details))
    text = _fit("\n".join(lines + ([foot] if foot else [])), 39000)
    blocks = None
    if note.options and buttons:
        elements = [{"type": "button", "action_id": f"solo_answer_{i}",
                     "text": {"type": "plain_text", "text": w[:75], "emoji": True}, "value": o[:2000],
                     **({"style": "primary"} if i == 0 and o.lower() in ("yes", "ok", "go", "approve") else {})}
                    for i, (o, w) in enumerate(zip(note.options[:24], choice_words(note.options[:24])))]
        if note.link:
            elements.append({"type": "button", "action_id": "solo_answer_link", "url": note.link,
                             "text": {"type": "plain_text", "text": "Open the answer page", "emoji": True}})
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": _fit("\n".join(lines))}},
                  {"type": "actions", "block_id": "solo_ask", "elements": elements}]
        if foot:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": foot}]})
    details = "🔎 *Details*\n" + note.details if note.details else ""
    return {"text": text, "blocks": blocks, "details": details}


def answered_blocks(blocks: list[dict] | None, choice: str, who: str, at: str) -> list[dict]:
    """The same message once answered: the buttons give way to what was chosen, so it is not chosen twice."""
    kept = [b for b in (blocks or []) if b.get("type") != "actions"]
    return kept + [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"✔️ *{choice}* — answered by {who} at {at}"}]}]


def group_thread(note: Note, details: bool = True, named: bool = True) -> str:
    """A grouped note's entry in the thread: what the one-line entry in the message does not say —
    its sentences, its question, its links and its details — and its headline only when the message
    did not name it (owner, 2026-11-11: "repeated headlines to sections"). `details=False` when its
    details are already in the thread."""
    parts = body_lines(note, buttons=False)[0 if not named else 1:]
    if details and note.details:
        parts.append("🔎 *Details*\n" + note.details)
    return "\n".join(parts)


def scrub(note: Note) -> Note:
    """The last resort when neither the rules nor a model gave a note that keeps the rule: each
    part that breaks it is dropped (the agent's words are whole in Details), and a headline that
    breaks it gives way to the first part that does not."""
    def ok(t):
        return bool(t) and not note_rules.violations(t) and len(t) <= HEADLINE_MAX * 2
    keep = [x for x in (note.headline, note.summary, note.ask) if ok(x)]
    head = note.headline if ok(note.headline) else (keep.pop(0) if keep else _FALLBACK.get(note.status, "An update."))
    note.summary = note.summary if ok(note.summary) and note.summary != head else ""
    note.ask = note.ask if ok(note.ask) and note.ask != head else ""
    note.headline, note.reshape = head, False
    if note_rules.violations(render(note)["text"]):  # the ask line or a link still breaks it: the headline alone
        note.summary, note.ask = "", ""
    return note


_FALLBACK = {"ask": "A decision is waiting for you.", "failed": "Something failed.", "warn": "Something needs a look.",
             "held": "Something is on hold.", "shipping": "Something is on its way.", "ok": "Done.", "info": "An update."}

GROUP_NAMED = 3


def render_group(notes: list[Note], sender: str = "") -> str:
    """Several notes inside the grouping window, as one message of prose: the worst icon, then each
    headline as a sentence with its own icon, three at most and the rest counted."""
    if len(notes) == 1:
        return render(notes[0])["text"]
    st = worst(n.status for n in notes)
    head = f"{ICONS[st]} *{len(notes)} updates*" + (f" from {sender}" if sender else "") + "."
    said = [f"{ICONS[n.status]} {emphasize(note_rules.sentence(lead_off(n.headline)))}" for n in notes[:GROUP_NAMED]]
    if len(notes) > GROUP_NAMED:
        said.append(f"And {len(notes) - GROUP_NAMED} more in the thread.")
    return " ".join([head] + said) + "\n_each one's detail is in the thread_"


def to_dict(note: Note) -> dict:
    return {"status": note.status, "headline": note.headline, "summary": note.summary, "ask": note.ask,
            "links": [list(x) for x in note.links], "details": note.details, "options": note.options,
            "link": note.link, "sender": note.sender}


def from_dict(d: dict) -> Note:
    """A stored note, including one written before the 2026-11-10 rule (bullets and a single link)."""
    summary = d.get("summary") or " ".join(note_rules.sentence(b) for b in d.get("bullets") or [])
    links = [tuple(x) for x in d.get("links") or []] or ([(d["link"], "Answer here")] if d.get("link") else [])
    return Note(d.get("status") or "info", d.get("headline") or "", summary, d.get("ask") or "", links,
                d.get("details") or "", list(d.get("options") or []), d.get("sender") or "")


if __name__ == "__main__":  # quick look: python3 owner_note.py "text"
    import sys
    r = render(from_text(" ".join(sys.argv[1:]), "you"))
    print(r["text"])
    if r["details"]:
        print("--- thread ---\n" + r["details"])
    if r["blocks"]:
        print(json.dumps(r["blocks"], indent=1, ensure_ascii=False))
