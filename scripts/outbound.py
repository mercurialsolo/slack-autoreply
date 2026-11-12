"""What Solo posts to the owner's console: said once, outcome first, in plain text.

The owner, 2026-11-11: "Human readable messaging including good formatting, no duplication,
summarization." Notes from agents already go through owner_note; this is the same promise for
everything else Solo itself posts to the console — failures, warnings, follow-up reminders, SR
hand-offs — and the one place that decides whether a message was already said.

Two pieces, both pure apart from one small JSON file:

* `compose(text)` -> (head, rest). Bare URLs become text links, task ids leave the prose, a line
  repeated word for word is said once, and a long notice opens with its first two sentences while
  the rest goes into the thread. Quoted lines (`>`, a draft or someone's own words) and code are
  never touched: what the owner approves or copies is exactly what was written.
* `Once` — a persisted idempotency ledger. A key (sender, place, words) claimed inside its window
  returns what was posted the first time instead of posting again. It lives on disk so a restart
  or an agent's retry after a timeout cannot post the same thing twice.

Illustrative, not literal rules: the model never sees this file.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import re
import time
from pathlib import Path

import note_rules

LONG_CHARS = 900       # past this, a notice opens with its summary and the rest goes in the thread
LONG_LINES = 12
HEAD_MAX = 400         # prose in the head; past it, two sentences stay and the rest moves down
MORE = "_More in the thread._"
KEEP = 2000            # keys kept in the ledger, newest first

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_CODE_SPAN = re.compile(r"`[^`\n]+`")
_SLACK_LINK = re.compile(r"<[^<>\n]+>")
_TASK_PAREN = re.compile(r"\s*\(\s*" + note_rules.TASK_ID + r"\s*\)")
# "Budget for t_ab12cd34." -> "Budget." The id goes with the preposition that pointed at it; an id
# inside a path (/tmp/t_ab12cd34/log) is a path, and stays.
_TASK_ID = re.compile(r"[ \t]*(?:\b(?:for|from|in|on|by)[ \t]+)?(?:\btask[ \t]+)?(?<![/\w])" + note_rules.TASK_ID
                      + r"(?![\w/-])", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z*_\"“])")


# ------------------------------------------------------------------ compose

def _protect(text: str) -> tuple[str, list[str]]:
    """Code and existing Slack links out of reach of the rewrites, as placeholders."""
    kept: list[str] = []

    def hide(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"
    for rx in (_CODE_FENCE, _CODE_SPAN, _SLACK_LINK):
        text = rx.sub(hide, text)
    return text, kept


def _restore(text: str, kept: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


def plain(line: str) -> str:
    """One prose line to the owner's rule: links as text, no task ids. Code and links already in
    Slack's `<url|label>` form are left exactly as they are."""
    t, kept = _protect(line)
    t = note_rules.URL_RE.sub(lambda m: f"<{m.group(0)}|{note_rules.label_for(m.group(0), t[:m.start()])}>", t)
    t, kept2 = _protect(_restore(t, kept))   # the new links are now out of reach too
    t = _TASK_PAREN.sub("", t)
    t = _TASK_ID.sub("", t)
    if not line[:1].isspace():
        t = t.lstrip()
    t = re.sub(r"(?<=\S)[ \t]{2,}", " ", t)               # indentation is left as it was
    t = re.sub(r"(?<=\S)[ \t]+([,.;:?!])(?=\s|$)", r"\1", t).rstrip()
    return _restore(t, kept2)


def _norm(line: str) -> str:
    return " ".join(line.lower().split())


def compose(text: str, *, fold: bool = True) -> tuple[str, str]:
    """(head, rest) for a console notice. `rest` is empty unless the notice was long enough to fold.

    `fold=False` keeps everything in one message — a draft has to: ✅ on that message is what sends it.
    """
    out: list[str] = []
    seen: set[str] = set()
    in_fence = False
    for line in (text or "").splitlines():
        fence = line.count("```") % 2 == 1
        if in_fence or line.lstrip().startswith(">") or ("```" in line and (fence or line.strip().startswith("```"))):
            out.append(line)             # code and quotes are someone's exact words
        else:
            p = plain(line)
            n = _norm(p)
            if n and n in seen:
                continue                 # the same line twice is noise, not emphasis
            if n:
                seen.add(n)
            out.append(p)
        in_fence ^= fence
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if not fold or (len(body) <= LONG_CHARS and body.count("\n") < LONG_LINES):
        return body, ""
    paras = body.split("\n\n")
    head, rest = paras[0], paras[1:]
    if len(head) > HEAD_MAX and "\n" not in head and "```" not in head and not head.lstrip().startswith(">"):
        sentences = _SENTENCE.split(head)
        first, after = " ".join(sentences[:2]), " ".join(sentences[2:])
        if len(sentences) > 2 and _balanced(first) and _balanced(after):
            head, rest = first, [after] + rest
    if not rest:
        return body, ""
    return f"{head}\n{MORE}", "\n\n".join(rest).strip()


def _balanced(t: str) -> bool:
    """No bold, italic, code span or link left open — a cut through `*a. B*` would break both halves."""
    t = _SLACK_LINK.sub("", t)
    return all(t.count(c) % 2 == 0 for c in "*_`") and "<" not in t


# ------------------------------------------------------------------ once

def key(*parts) -> str:
    """An idempotency key: the same words to the same place from the same sender are the same message,
    whatever whitespace or case a retry used."""
    raw = "\x1f".join(_norm(str(p or "")) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Once:
    """Keys already posted, with what they posted as, on disk and file-locked.

    `claim(k, window_s)` returns None when the caller should post (and records the claim), or the
    earlier record when the same key was claimed inside the window. `record(k, **info)` adds what the
    post produced (a ts, an item id) so a later duplicate can point at it."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _update(self, fn):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "a+", encoding="utf-8") as lockf:
            fcntl.flock(lockf, fcntl.LOCK_EX)
            try:
                data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            except (json.JSONDecodeError, OSError):
                data = {}
            out = fn(data)
            if len(data) > KEEP:
                for k in sorted(data, key=lambda k: data[k].get("at", 0))[:-KEEP]:
                    data.pop(k, None)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            return out

    def claim(self, k: str, window_s: float, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now

        def go(d):
            prev = d.get(k)
            if prev and now - float(prev.get("at") or 0) < window_s:
                return dict(prev)
            d[k] = {"at": now}
            return None
        return self._update(go)

    def record(self, k: str, **info) -> None:
        self._update(lambda d: d.setdefault(k, {"at": time.time()}).update(info))

    def release(self, k: str) -> None:
        """The post failed: the next try must not be taken for a duplicate."""
        self._update(lambda d: d.pop(k, None))
