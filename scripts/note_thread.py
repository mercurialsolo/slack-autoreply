"""What a note's "🔎 Details" thread reply adds to the note — that, and only that.

Owner, 2026-11-13, on "Box memory high: 53% of 63 GB used, swap 76%" from key-health: "same message
is almost duplicated - little new info". Its thread restated the headline, "Nothing was stopped" and
the lists the note had just said. The thread is for what the note did not say: the full list past
the note's cut, timings, per-item numbers, links, the raw record (a code block, kept whole). When
nothing new is left there is no thread reply, and the note does not say "details in the thread".

Two checks, the first always, the second when a model is at hand:
  - `beyond`: a sentence or list line whose words the note already says (and that brings no new
    link) is dropped. Pure: no model, no quota.
  - `model_beyond`: a model reads the note and the numbered lines left, and names the ones that tell
    the owner something new. It can only drop lines, never add or reword one, and any failure
    leaves the first check's answer.
"""
from __future__ import annotations

import json
import re

import note_rules

LIST_ITEM_RE = re.compile(r"^\s*(?:[-•*◦]|\d+[.)])\s+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z*\"'(])|(?<=;)\s+")  # a clause after ";" is its own fact
# Words that carry no fact: "Nothing was stopped" says what "Nothing stopped" says.
FILLER = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "has", "have", "had", "of", "to", "in",
          "on", "at", "and", "or", "for", "it", "its", "this", "that", "with", "by", "as", "so", "there",
          "details", "detail", "thread", "here", "now",
          "needs", "need", "answer", "you", "your"}  # "Needs an answer on <task>": the 👉 line says that

PROMPT = """The owner already read this note in Slack:
<note>
{note}
</note>

Its thread reply would add these lines:
{lines}

Which lines tell the owner something the note does not already say (a fact, a number, a name, a
link, a list item the note left out)? A line that only says again what the note says, in the same
or other words, is not new.

Reply with JSON only: {{"new": [the numbers of the new lines]}}"""


def words(text: str) -> set[str]:
    """The fact-bearing words of a text, links taken out: does the note already say this?"""
    t = note_rules.TASK_ID_RE.sub(" ", note_rules.take_links(text or "")[0])  # an id the note left out is not news
    t = re.sub(r"[*_~`]", "", t.lower())
    return {w for w in re.findall(r"[a-z0-9]+(?:[.'][a-z0-9]+)*", t) if w not in FILLER}


def urls(text: str) -> set[str]:
    return {u for u, _ in note_rules.take_links(text or "")[1]}


def _units(line: str) -> list[str]:
    """A line as the pieces that can be said or not: a list item whole, prose by its sentences."""
    if LIST_ITEM_RE.match(line):
        return [line]
    return [s for s in SENTENCE_SPLIT.split(line) if s.strip()]


def _fences(lines: list[str]) -> list[bool]:
    """Which lines are inside a ``` block (the raw record, someone's exact words: never pruned)."""
    out, inside = [], False
    for ln in lines:
        fence = ln.count("```") % 2 == 1
        out.append(inside or "```" in ln)
        inside ^= fence
    return out


def beyond(details: str, said: list[str], links: list[str] | tuple = ()) -> str:
    """`details` without what `said` (the note's headline, sentences and question) already says.

    A sentence or list line stays when it has a word the note does not, or a link the note does not
    carry. A header line ("Biggest:") stays only with an item under it. Code blocks stay whole."""
    said_words = set().union(*(words(s) for s in said if s))
    said_urls = set(links).union(*(urls(s) for s in said if s))
    lines = (details or "").splitlines()
    kept: list[str] = []
    for ln, fenced in zip(lines, _fences(lines)):
        if fenced:
            kept.append(ln)
            continue
        if not ln.strip():
            kept.append("")
            continue
        new = [u for u in _units(ln) if (words(u) - said_words) or (urls(u) - said_urls)]
        if new:
            kept.append(" ".join(new) if not LIST_ITEM_RE.match(ln) else ln)
    return _tidy(kept)


def _tidy(lines: list[str]) -> str:
    """No header left without its items, no runs of blank lines."""
    out: list[str] = []
    for i, ln in enumerate(lines):
        if ln.rstrip().endswith(":") and not LIST_ITEM_RE.match(ln):
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            if not LIST_ITEM_RE.match(nxt):
                continue
        if not ln.strip() and (not out or not out[-1].strip()):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _lines(details: str) -> list[str]:
    return [ln for ln in (details or "").splitlines() if ln.strip()]


def model_beyond(details: str, note_text: str, call) -> str:
    """`details` with only the lines a model calls new next to `note_text`. `call(prompt) -> str`.
    Code blocks are not offered: they stay whole. Never raises; on any doubt, `details` as it was."""
    lines = _lines(details)
    if not lines or not call:
        return details
    fenced = _fences(lines)
    offer = [(i, ln) for i, (ln, f) in enumerate(zip(lines, fenced)) if not f]
    if not offer:
        return details
    try:
        listing = "\n".join(f"{n}. {ln.strip()}" for n, (_, ln) in enumerate(offer, 1))
        answer = call(PROMPT.format(note=note_text.strip(), lines=listing))
        m = re.search(r"\{.*\}", answer or "", re.S)
        new = json.loads(m.group(0)).get("new") if m else None
        if not isinstance(new, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in new):
            return details
        keep = {offer[n - 1][0] for n in new if 1 <= n <= len(offer)}
    except Exception:  # noqa: BLE001 - the model is a second look, never load-bearing
        return details
    return _tidy([ln for i, ln in enumerate(lines) if fenced[i] or i in keep])
