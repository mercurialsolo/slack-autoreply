"""The judgment behind a note to the owner: which two or three things matter, in plain sentences.

Rules can strip an id or turn a URL into a link (note_rules); they cannot tell that "the review
budget ran out twice" is the news in a paragraph of counters. A small model reads the agent's
words and answers with the note's parts as JSON. Whatever it says is then held to the rule again
(note_rules, applied in owner_note.from_text), and its links must be ones the agent actually wrote.

Never load-bearing: no key, no CLI, a timeout, bad JSON — `shape` returns None and the note falls
back to the deterministic reading. A note to the owner must not depend on a quota (27, 29, 31 Oct).

NOTE_SHAPER: auto (default: the API when ANTHROPIC_API_KEY is set and DISPATCH_USE_API_KEY on, else the claude
CLI) | api |
claude | off. NOTE_SHAPER_MODEL picks the model (default: claude-haiku-5-5).
"""
from __future__ import annotations

import json
import os
import re
import subprocess

import config
import note_rules
import note_thread
import owner_note

STATUSES = owner_note.STATUSES
TIMEOUT_S = 20  # it runs inside delivery: a slow model costs the owner a late note, not a lost one
SHORT = 140  # a one-line note this short with no link is already shaped

PROMPT = """You turn an agent's status note into a short note its owner reads on a phone in Slack.

The owner's rule: {rule}

Reply with JSON only, no prose around it:
{{"status": what happened, not the words used for it ("no new failures" is "ok"): one of "ask" (the owner must decide), "failed" (rolled back, broken), "warn" (live with a problem), "held" (frozen, paused), "shipping" (in progress), "ok" (done, live, green), "info",
 "headline": one short plain sentence, what happened (under 100 characters): "Goal sweep: no new call failures", never what was read or checked ("Read the reviews since 16:20Z"),
 "points": at most three short plain sentences, each one whole fact the owner cares about; [] if the headline says it all. Never "and 3 more",
 "ask": only when the owner must decide: one question with the choices in words ("Give it one more round, or stop it?"); otherwise "",
 "links": at most two of the URLs in the note, the ones the owner would open: [{{"url": exact URL as written, "label": two or three plain words such as "Answer here", "What changed", "Release page"}}]. An answer page always comes first when there is one,
 "sections": the whole note for the thread, every fact in it: [{{"title": two or three words, "facts": ["one fact per item, ids, links and numbers kept exactly as written, except a count from two to twelve in words"]}}]. A last section titled "Needs you" only when the owner must do something.}}

Each fact in the sections leads with plain words: which behaviour broke or what changed, in a short sentence a person reads without the code ("The replay where a Slack ask also gets the line in the DM is red on two train runs, not due yet"). Then, after the words, each identifier the agent wrote (a file::test, a path, a task id, a sha, a field name, a command) exactly as written inside Slack inline code: `host/replay_test.py::test_ask_in_dm`. Never a bare identifier, never one as the fact's first words. Version numbers and box names are words, not code. A count from two to twelve in words, right before the same word the agent wrote after it: "two train runs", never "2 train run(s)"; other numbers as written.

Keep version numbers and box or machine names (v0.771.0, Helsinki) in the headline and points. Leave out of them task ids, shas, field names, counters, commands and the agents' own names and abbreviations (CALL-REVIEW, p50, G1-G5): say what they mean in plain words. They stay in the sections.

The agent's note:
<note>
{text}
</note>"""


def wanted(text: str, structured: bool = False) -> bool:
    """A model call only for a note that needs judgment: not one the agent shaped itself, not one
    that is already a short plain line."""
    t = (text or "").strip()
    if structured or not t or mode() == "off":
        return False
    return len(t) > SHORT or "\n" in t or bool(note_rules.URL_RE.search(t)) or bool(note_rules.violations(t))


def mode() -> str:
    m = (config.env("NOTE_SHAPER") or "auto").strip().lower()
    if m == "auto":
        # The API only where the operator opted into billing it (DISPATCH_USE_API_KEY), like dispatch.
        return "api" if os.environ.get("ANTHROPIC_API_KEY") and config.CFG.get("dispatch_use_api_key") else "claude"
    return m if m in ("api", "claude", "off") else "off"


def _model() -> str:
    return config.env("NOTE_SHAPER_MODEL") or config.CFG.get("triage_fallback_model") or "claude-haiku-5-5"


def _call_api(prompt: str) -> str:
    import anthropic  # noqa: PLC0415 - optional; only when this path is chosen
    client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=1)
    msg = client.messages.create(model=_model(), max_tokens=600, messages=[{"role": "user", "content": prompt}])
    return "".join(getattr(b, "text", "") for b in msg.content)


def _call_cli(prompt: str) -> str:
    env = dict(os.environ)
    if not config.CFG.get("dispatch_use_api_key"):
        env.pop("ANTHROPIC_API_KEY", None)
    p = subprocess.run([config.CFG["claude_bin"], "-p", prompt, "--output-format", "json", "--max-turns", "1",
                        "--model", _model()], capture_output=True, text=True, timeout=TIMEOUT_S, env=env,
                       cwd=str(config.STATE_DIR))
    out = p.stdout.strip()
    if p.returncode != 0 or not out:
        raise RuntimeError(f"claude exit {p.returncode}")
    res = json.loads(out.splitlines()[-1])
    if res.get("is_error"):
        raise RuntimeError("claude answered with an error")
    return res.get("result") or ""


def parse(answer: str, source: str) -> dict | None:
    """The model's answer, checked: a known status, a headline, and links the agent really wrote."""
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    status = str(d.get("status") or "").strip().lower()
    headline = str(d.get("headline") or "").strip()
    if status not in STATUSES or not headline:
        return None
    links = []
    for x in d.get("links") or []:
        if isinstance(x, dict) and str(x.get("url") or "") and str(x["url"]) in source:
            links.append((str(x["url"]), str(x.get("label") or "").strip()))
    raw_points = d.get("points") if isinstance(d.get("points"), list) else []  # a string is no list of facts
    points = [p.strip() for p in raw_points if isinstance(p, str) and p.strip()]
    return {"status": status, "headline": headline, "summary": str(d.get("summary") or "").strip(),
            "points": points[:owner_note.POINTS_MAX], "ask": str(d.get("ask") or "").strip(), "links": links,
            "details": sections(d.get("sections"), source)}


NEEDS_YOU = "needs you"
FENCED_RE = re.compile(r"```.*?```", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
FACT_TOKEN_RE = re.compile(r"https?://[^\s<>|]+|[^\s<>|]*\d[^\s<>|]*")


def sections(raw, source: str) -> str:
    """The model's sections as the thread's text (`*Title*`, one `•` per fact, "Needs you" last),
    or "" — the agent's own words then — when they lost an id, a number or a link the agent wrote."""
    if not isinstance(raw, list):
        return ""
    parts = []
    for s in raw:
        if not isinstance(s, dict):
            return ""
        title = str(s.get("title") or "").strip().strip("*_: ")
        raw_facts = s.get("facts")
        if not isinstance(raw_facts, list):  # a string would be read a character at a time
            return ""
        facts = [re.sub(r"^(?:[•◦]|[-*](?=\s))\s*", "", str(f).strip()) for f in raw_facts]
        facts = [f for f in facts if f]
        if title and facts:
            parts.append((title, facts))
    if not parts:
        return ""
    chunks = [f"*{t}*\n" + "\n".join("• " + f for f in fs) for t, fs in parts if t.lower() != NEEDS_YOU]
    # a raw record (a fenced block) is no one-line fact: it stays whole, in a section of its own
    blocks = [b for b in FENCED_RE.findall(source) if b not in "\n".join(chunks)]
    chunks += ["*Raw record*\n" + "\n".join(blocks)] if blocks else []
    chunks += [f"*{t}*\n" + "\n".join("• " + f for f in fs) for t, fs in parts if t.lower() == NEEDS_YOU]  # last
    text = "\n\n".join(chunks)
    kept = set(_tokens(text))
    if not all(_kept(t, kept, text, source) for t in _tokens(source)):
        return ""
    # a command the agent wrote is its exact words: a section that lost one loses the note's point
    return text if all(m.group(0) in text for m in INLINE_CODE_RE.finditer(FENCED_RE.sub("", source))) else ""


# A small count the owner reads in words (owner, 2026-11-20: "red on two train runs", not "2 train run(s)").
# Not one: "one" is a pronoun as often as a count, so it would keep a lost "1".
COUNT_WORDS = {"2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight",
               "9": "nine", "10": "ten", "11": "eleven", "12": "twelve"}


def _kept(token: str, kept: set, text: str, source: str = "") -> bool:
    """`token` (a number, an id, a URL of the agent's) is in `text` as itself, not inside a longer one:
    "5" is not kept by "15", "1.5" or "5,000" (a changed count is a lost fact). A count from two to
    twelve is also kept by its word before the word the agent wrote after it ("2 train run(s)" by
    "two train runs"); "two failed" does not keep "2 passed"."""
    if token in kept or re.search(r"(?<![\w.])" + re.escape(token) + r"(?!\w|[.,]\d)", text):
        return True
    word = COUNT_WORDS.get(token)
    if not word:
        return False
    nouns = re.findall(r"(?<![\w.])" + re.escape(token) + r"\s+([A-Za-z]+)", source)
    return any(re.search(rf"\b{word}\s+{re.escape(n[:-1] if n.endswith('s') and len(n) > 3 else n)}", text, re.I)
               for n in nouns)


def _tokens(text: str) -> list[str]:
    return [m.group(0).strip(".,;:!?()[]*_`'\"") for m in FACT_TOKEN_RE.finditer(text or "")]


def _resolve_call():
    """The model to call by NOTE_SHAPER, or None when it is off."""
    m = mode()
    return None if m == "off" else _call_api if m == "api" else _call_cli


def shape(text: str, call=None, force: bool = False) -> dict | None:
    """{status, headline, summary, points, ask, links, details} for a note, or None when the rules alone should read it.

    `call(prompt) -> str` is the model; tests pass their own. `force` asks even for a short or
    structured note — the rules already failed it. Never raises."""
    try:
        call = call or (_resolve_call() if force or wanted(text) else None)
        if call is None:
            return None
        return parse(call(PROMPT.format(rule=note_rules.RULE, text=text.strip())), text)
    except Exception:  # noqa: BLE001 - the shaper is a courtesy; the fallback always reads the note
        return None


def build(body: str, sender: str, spec: dict | None = None, call=None, buttons: bool = True) -> owner_note.Note:
    """The note for an agent's message, held to the rule: the rules first (with a model's reading
    when the text wants judgment), then — when the rules could only leave a hole, a raw link, a
    cut headline or two icons — a model words it from the agent's text, and the rendered note is
    checked again. Whatever is still against the rule is dropped (`owner_note.scrub`); the agent's
    words stay whole in Details. A note that breaks the rule is never posted (owner, 2026-11-11)."""
    sl = note_rules.slack_links  # the sender's links as Slack links once, before any part is read, scrubbed or kept
    body, spec = sl(body or ""), {**(spec or {}), "headline": sl((spec or {}).get("headline")),
                                  "points": [sl(p) for p in (spec or {}).get("points") or []],
                                  "details": sl((spec or {}).get("details"))}
    kw = dict(status=spec.get("status"), details=spec.get("details"), options=spec.get("options"),
              link=spec.get("link") or "")
    context = "\n".join([spec.get("headline") or ""] + list(spec.get("points") or []) + [body]).strip()
    asked = {}  # what a model already said to a prompt: the same prompt again costs time, not a better note

    def ask(text, force=False):
        key = (text or "").strip()
        if not asked.get(key):
            asked[key] = shape(text, call=call, force=force)
        return asked[key]

    if spec.get("headline"):
        # The sender's own parts. A digest's ("Factory update: 1 decision for you", points of links
        # and paths) still wants judgment: a model words it when one can; else the parts, by the rules.
        shaped = ask(context) if wanted(context) or call else None
        note = owner_note.from_text(body, sender, shaped=shaped, **kw) if shaped else None
        if note is None or _broken(note, buttons):
            note = owner_note.from_text(body, sender, headline=spec["headline"], points=spec.get("points"), **kw)
    else:
        note = owner_note.from_text(body, sender, shaped=ask(body), **kw)
    # the thread's second look: only where a model already reads this note (no extra call for a short line)
    look = call or (_resolve_call() if wanted(context) or wanted(body) else None)
    if not call and any(v is None for v in asked.values()):
        look = None  # the model already failed (or timed out) on this note: no second wait for it
    if not _broken(note, buttons):
        return _thread(note, buttons, look)
    again = ask(context, force=True)
    if again:
        redo = owner_note.from_text(body, sender, shaped=again, **kw)
        if not _broken(redo, buttons):
            return _thread(redo, buttons, look)
        note = redo
    return _thread(owner_note.scrub(note, buttons, body), buttons, look)


def _thread(note: owner_note.Note, buttons: bool, call=None) -> owner_note.Note:
    """A model's second look at the Details thread next to the note as posted: only the lines that
    add to it stay; none left, no thread (owner, 2026-11-13). The rules already dropped the plain
    repeats (note_thread.beyond); a model also catches one in other words. Never load-bearing.
    `call` None: no model for this note (a short plain line costs no model call)."""
    if note.details and call:
        note.details = note_thread.model_beyond(note.details, owner_note.render(note, buttons)["text"], call)
    return note


def _broken(note: owner_note.Note, buttons: bool) -> bool:
    if note.reshape or not note.headline:
        return True
    return bool(note_rules.violations(owner_note.render(note, buttons=buttons)["text"]))
