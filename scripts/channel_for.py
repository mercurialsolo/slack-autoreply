"""A channel dossier's **For:** line: the owner's own, or a draft Solo writes from the traffic.

The router places a report by what each channel is *for*. When the owner has not said and Slack
has no purpose or topic, the line was left blank and routine reports never placed (release-watch
at 0.49). A draft fills that gap from the channel's recent human traffic, and is marked so it is
never mistaken for the owner's word:

    **For (draft):** <text> <!-- solo-draft:<sig> -->

- `**For:** ...` is the owner's line. Solo never rewrites it.
- A draft whose text still matches its signature is Solo's, and the next learn refreshes it.
- A draft whose text the owner changed is theirs now: kept verbatim, read as an owner line.

The router reads either; `slackctl where` counts drafts separately, as lines to review.
"""
from __future__ import annotations

import hashlib
import re

OWNER = "**For:**"
DRAFT = "**For (draft):**"
_MARK = re.compile(r"\s*<!--\s*solo-draft:([0-9a-f]{8})\s*-->\s*$")


def _sig(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def read(lines: list[str]) -> tuple[str, str]:
    """('owner' | 'draft' | '', text) for a dossier's lines. '' is a blank or missing line."""
    for l in lines:
        if l.startswith(OWNER):
            text = l[len(OWNER):].strip()
            return ("owner", text) if text else ("", "")
        if l.startswith(DRAFT):
            rest = l[len(DRAFT):]
            m = _MARK.search(rest)
            text = _MARK.sub("", rest).strip()
            if not text:
                return "", ""
            # Edited text (or a removed marker) means the owner touched it, so it is theirs.
            return ("draft" if m and m.group(1) == _sig(text) else "owner"), text
    return "", ""


def _plain(text: str, n: int) -> str:
    text = " ".join(text.replace("<!--", "").replace("-->", "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def draft(terms: list[str], recent: list[str]) -> str:
    """A For: draft from recurring terms and a few recent lines; '' when there is nothing to go on."""
    parts = []
    if terms:
        parts.append("recent traffic about " + ", ".join(terms[:6]))
    eg = [f"“{_plain(t, 60)}”" for t in recent if t.strip()][:2]
    if eg:
        parts.append("e.g. " + "; ".join(eg))
    return _plain(" — ".join(parts), 220)


def draft_line(text: str) -> str:
    text = _plain(text, 220)
    return f"{DRAFT} {text} <!-- solo-draft:{_sig(text)} -->"


def replace(lines: list[str], new: str) -> list[str]:
    """`lines` with the For: line (owner or draft) swapped for `new`; inserted after the title if
    there is none."""
    for i, l in enumerate(lines):
        if l.startswith((OWNER, DRAFT)):
            return lines[:i] + [new] + lines[i + 1:]
    at = 2 if len(lines) > 1 and not lines[1].strip() else 1
    return lines[:at] + [new] + lines[at:]
