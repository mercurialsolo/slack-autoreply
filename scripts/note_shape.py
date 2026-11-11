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
import owner_note

STATUSES = owner_note.STATUSES
TIMEOUT_S = 20  # it runs inside delivery: a slow model costs the owner a late note, not a lost one
SHORT = 140  # a one-line note this short with no link is already shaped

PROMPT = """You turn an agent's status note into a short note its owner reads on a phone in Slack.

The owner's rule: {rule}

Reply with JSON only, no prose around it:
{{"status": one of "ask" (the owner must decide), "failed" (rolled back, broken), "warn" (live with a problem), "held" (frozen, paused), "shipping" (in progress), "ok" (done, live, green), "info",
 "headline": one short plain sentence, the news (under 100 characters),
 "summary": one or two more short plain sentences: what it means, the two or three things that matter. "" if the headline says it all,
 "ask": only when the owner must decide: one question with the choices in words ("Give it one more round, or stop it?"); otherwise "",
 "links": at most two of the URLs in the note, the ones the owner would open: [{{"url": exact URL as written, "label": two or three plain words such as "Answer here", "What changed", "Release page"}}]. An answer page always comes first when there is one.}}

Keep version numbers and box or machine names (v0.771.0, Helsinki). Leave out task ids, shas, field names, counters and commands entirely: they stay in the full note, which the owner can open.

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
    return {"status": status, "headline": headline, "summary": str(d.get("summary") or "").strip(),
            "ask": str(d.get("ask") or "").strip(), "links": links}


def shape(text: str, call=None, force: bool = False) -> dict | None:
    """{status, headline, summary, ask, links} for a note, or None when the rules alone should read it.

    `call(prompt) -> str` is the model; tests pass their own. `force` asks even for a short or
    structured note — the rules already failed it. Never raises."""
    try:
        if call is None:
            if mode() == "off" or not (force or wanted(text)):
                return None
            call = _call_api if mode() == "api" else _call_cli
        return parse(call(PROMPT.format(rule=note_rules.RULE, text=text.strip())), text)
    except Exception:  # noqa: BLE001 - the shaper is a courtesy; the fallback always reads the note
        return None


def build(body: str, sender: str, spec: dict | None = None, call=None, buttons: bool = True) -> owner_note.Note:
    """The note for an agent's message, held to the rule: the rules first (with a model's reading
    when the text wants judgment), then — when the rules could only leave a hole, a raw link, a
    cut headline or two icons — a model words it from the agent's text, and the rendered note is
    checked again. Whatever is still against the rule is dropped (`owner_note.scrub`); the agent's
    words stay whole in Details. A note that breaks the rule is never posted (owner, 2026-11-11)."""
    spec = spec or {}
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
    if not _broken(note, buttons):
        return note
    again = ask(context, force=True)
    if again:
        redo = owner_note.from_text(body, sender, shaped=again, **kw)
        if not _broken(redo, buttons):
            return redo
        note = redo
    return owner_note.scrub(note, buttons)


def _broken(note: owner_note.Note, buttons: bool) -> bool:
    if note.reshape or not note.headline:
        return True
    return bool(note_rules.violations(owner_note.render(note, buttons=buttons)["text"]))
