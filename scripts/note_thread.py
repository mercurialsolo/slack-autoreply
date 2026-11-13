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

LIST_ITEM_RE = note_rules.LIST_ITEM_RE
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")  # a count or a size: which item it belongs to is a fact
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z*\"'(])")
CLAUSE_SPLIT = re.compile(r"(?<=;)\s+")  # a clause after ";" is its own fact, unless it is a command's
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


def _seq(text: str) -> list[str]:
    """The fact-bearing words of a text in order, links taken out. A task id counts: the note leaves
    ids out on purpose, so the thread is where the owner finds one (unless the note links to it)."""
    t = re.sub(r"[*_~`]", " ", note_rules.take_links(text or "")[0].lower())
    t = re.sub(r"\(\d+\)(?=:)", " ", t)  # "VMs running (3): a, b, c": the count of the list it heads, no fact of its own
    return [w for w in re.findall(r"[a-z0-9]+(?:[.'][a-z0-9]+)*", t) if w not in FILLER]


def words(text: str) -> set[str]:
    """The fact-bearing words of a text: does the note already say this?"""
    return set(_seq(text))


def _pairs(seq: list[str]) -> set[tuple[str, str]]:
    return set(zip(seq, seq[1:]))


def urls(text: str) -> set[str]:
    return {u for u, _ in note_rules.take_links(text or "")[1]}


def _units(line: str) -> list[str]:
    """A line as the pieces that can be said or not: a list item or a line with inline code whole,
    prose by its sentences and ";" clauses. A sentence with a command stays whole (a command is
    never cut at its ";")."""
    if LIST_ITEM_RE.match(line) or "`" in line:
        return [line]
    out: list[str] = []
    for sent in SENTENCE_SPLIT.split(line):
        out += [sent] if note_rules.COMMAND_RE.search(sent) else CLAUSE_SPLIT.split(sent)
    return [u for u in out if u.strip()]


def _fences(lines: list[str]) -> list[bool]:
    """Which lines are inside a ``` block, its fences too (the raw record, someone's exact words)."""
    out, inside = [], False
    for ln in lines:
        out.append(inside or "```" in ln)
        inside ^= ln.count("```") % 2 == 1
    return out


def _kept(line: str, fenced: bool) -> bool:
    """A line no check may drop: code (a block, a command, inline code). The note never carries a
    command, and when it says "the command is in the thread" the command must be there."""
    return fenced or bool(note_rules.COMMAND_RE.search(line))


def beyond(details: str, said: list[str], links: list[str] | tuple = ()) -> str:
    """`details` without what `said` (the note's headline, sentences and question) already says.

    A sentence or list line stays when it has a word or a link the note does not, or numbers the
    note pairs differently ("hillsboro: 1 passed" against "3 passed, 1 failed on hillsboro"). A
    header line ("Biggest:") stays only with an item under it. Code stays whole."""
    said = [s for s in said if s]
    said_words = set().union(*(words(s) for s in said))
    said_pairs = set().union(*(_pairs(_seq(s)) for s in said))
    said_urls = set(links).union(*(urls(s) for s in said))
    # a task id the note links to (its answer page) is said; one it does not is news
    said_words |= {w for u in said_urls for i in note_rules.TASK_ID_RE.findall(u) for w in _seq(i)}

    def new(unit: str) -> bool:
        seq = _seq(unit)
        if set(seq) - said_words or urls(unit) - said_urls:
            return True
        return any(NUMBER_RE.fullmatch(a) or NUMBER_RE.fullmatch(b) for a, b in _pairs(seq) - said_pairs)

    lines = (details or "").splitlines()
    kept: list[str] = []
    for ln, fenced in zip(lines, _fences(lines)):
        if fenced or not ln.strip():
            kept.append(ln)
            continue
        fresh = [u for u in _units(ln) if note_rules.COMMAND_RE.search(u) or new(u)]
        if fresh:
            kept.append(ln if LIST_ITEM_RE.match(ln) or len(fresh) == len(_units(ln)) else " ".join(fresh))
    return _tidy(kept)


def _tidy(lines: list[str]) -> str:
    """No header left without its items (or the code block it introduces), no runs of blank lines
    outside code. A code block is never touched."""
    fenced = _fences(lines)
    out: list[str] = []
    for i, ln in enumerate(lines):
        if fenced[i]:
            out.append(ln)
            continue
        if ln.rstrip().endswith(":") and not LIST_ITEM_RE.match(ln):
            nxt = next((x for x in lines[i + 1:] if x.strip()), "")
            if not (LIST_ITEM_RE.match(nxt) or nxt.lstrip().startswith("```")):
                continue
        if not ln.strip() and (not out or not out[-1].strip()):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _intro(lines: list[str], i: int) -> bool:
    """A "Raw record:" line that heads the code block under it: part of the block."""
    nxt = next((x for x in lines[i + 1:] if x.strip()), "")
    return lines[i].rstrip().endswith(":") and nxt.lstrip().startswith("```")


def model_beyond(details: str, note_text: str, call) -> str:
    """`details` with only the lines a model calls new next to `note_text`. `call(prompt) -> str`.
    Code is not offered (it stays whole), blank lines stay. Never raises; on any doubt, `details`
    as it was."""
    lines = (details or "").splitlines()
    if not call or not any(ln.strip() for ln in lines):
        return details
    fixed = [_kept(ln, f) or not ln.strip() or _intro(lines, i) for i, (ln, f) in enumerate(zip(lines, _fences(lines)))]
    offer = [i for i, f in enumerate(fixed) if not f]
    if not offer:
        return details
    try:
        listing = "\n".join(f"{n}. {lines[i].strip()}" for n, i in enumerate(offer, 1))
        answer = call(PROMPT.format(note=note_text.strip(), lines=listing))
        m = re.search(r"\{.*\}", answer or "", re.S)
        new = json.loads(m.group(0)).get("new") if m else None
        if not isinstance(new, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in new):
            return details
        keep = {offer[n - 1] for n in new if 1 <= n <= len(offer)}
    except Exception:  # noqa: BLE001 - the model is a second look, never load-bearing
        return details
    return _tidy([ln for i, ln in enumerate(lines) if fixed[i] or i in keep])
