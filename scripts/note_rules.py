"""The owner's readability rule, as a pass every note to the owner goes through before it renders.

The owner, 2026-11-10, on two notes that arrived as a budget dump and a release note of cut-off
bullets and bare URLs:

    Start with one or two short plain sentences, then at most one line on what is needed, as a
    question with its choices in words. No task ids, shas, metric names, counters or CLI commands
    in the body. Links are text links, one or two at most, never a bare URL and never an id after
    a link. No bulleted lists that get cut off: the two or three things that matter go in a
    sentence, the full list goes in the Details reply.

A model decides what matters (note_shape); this module makes sure whatever it, an agent's explicit
`--headline/--point`, or the no-model fallback wrote still obeys the rule. It only ever removes from
the body — the agent's words stay whole in Details. Pure functions, no Slack, no model.
"""
from __future__ import annotations

import re

MAX_LINKS = 2
RULE = (
    "Start with one or two short plain sentences: what happened and what it means. Then, only if "
    "the owner has to decide something, one line asking it as a question with the choices in words "
    "(\"one more round or stop it?\"). No task ids (t_…), shas, metric or field names, counters "
    "(5/5, 274/1,200), file paths or CLI commands in the body — they stay in the Details thread. "
    "Links are text links with a plain label (Answer here, What changed, Release page), one or two "
    "at most; never a bare URL and never an id after a link. No bulleted lists: the two or three "
    "things that matter go in a sentence and the full list stays in Details. Never cut a sentence "
    "off with an ellipsis.")

URL = r"https?://[^\s<>|]+[^\s<>|.,;:!?)\]'\"]"
URL_RE = re.compile(URL)
SLACK_LINK_RE = re.compile(r"<(" + URL + r")(?:\|([^>]*))?>")
TASK_ID = r"\bt_[0-9a-f]{6,}\b"
TASK_ID_RE = re.compile(TASK_ID)
# A command is a command word with arguments that only a shell would want: a flag, a path, an
# `x=y`, or a "...". "make sure" and "the factory queue" are words, not commands.
CMD_WORD = r"\b(?:sudo|ssh|colima|kubectl|curl|git|python3?|npm|pip3?|brew|launchctl|solo|slackctl|factory|claudectl|make|docker|systemctl|sysctl|gh)\s"
CMD_ARGS = r"(?=[^.?!()\n]*?(?:\s--?[a-z]|\S=\S|\s[~.]?/\w|\.\.\.|…))"
COMMAND_SPAN_RE = re.compile(CMD_WORD + CMD_ARGS + r"[^()\n]*?(?=\s*\(|[?!]|\.\s|\.$|$)", re.I)
CODE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.S)
COMMAND_RE = re.compile(r"`|" + CMD_WORD + CMD_ARGS, re.I)  # does this text carry a command at all
COUNTER_RE = re.compile(r"(?:\b[a-z][a-z-]*\s+)?\b\d[\d,]*\s*/\s*\d[\d,]*\b")
CHECKS_RE = re.compile(r"\b(?:checks?\s*:\s*)?(\d+)\s+passed,?\s+0\s+failed(?:,?\s+(?:and\s+)?0\s+warnings?)?", re.I)
TRUNCATED_QUOTE_RE = re.compile(r"[\"“][^\"”\n]*(?:\.\.\.|…)[\"”]")
ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.|…)")
ICON_RE = re.compile(r"^\s*(?:✅|⚠️|⚠|🔴|⏸️|⏸|🚀|❓|📨)️?\s*")
# Labels a machine puts before a value ("Changelog: <url>", "Operator (budget): …"): the value is
# what the owner reads.
LABEL_RE = re.compile(r"^\s*(?:needs an answer(?: on [\w-]+)?|operator(?:\s*\([^)]*\))?|budget|burn(?: so far)?|"
                      r"changelog|releases?|details|checks|links?|not live(?: now)?|live(?: now)?|status|update|"
                      r"fyi|note|summary|tl;?dr)\s*:\s*", re.I)


def label_for(url: str, before: str = "") -> str:
    """A plain label for a link, from the words just before it and the shape of the URL."""
    ctx, u = (before or "")[-40:].lower(), url.lower()
    if "/ask/" in u or re.search(r"\banswer|\bdecide|\bgrant\b|\?\s*$", ctx):
        return "Answer here"
    if "changelog" in ctx or "/compare/" in u or "what changed" in ctx:
        return "What changed"
    if "release" in ctx or "/releases" in u:
        return "Release page"
    if "/pull/" in u:
        return "The PR"
    if "/issues/" in u:
        return "The issue"
    if "detail" in ctx:
        return "Details"
    if "/actions/runs" in u or re.search(r"\bci\b|\bchecks?\b", ctx):
        return "The CI run"
    if "dashboard" in ctx or "dashboard" in u:
        return "Dashboard"
    if u.endswith(".log") or re.search(r"\blogs?\b", ctx):
        return "The log"
    return "Open it"


def take_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Pull every link out of a sentence — `<url|label>` and bare URLs alike — with a label for each."""
    links: list[tuple[str, str]] = []

    def slack(m):
        links.append((m.group(1), (m.group(2) or "").strip() or label_for(m.group(1), text[:m.start()])))
        return " "

    def bare(m):
        links.append((m.group(0), label_for(m.group(0), text[:m.start()])))
        return " "
    text = SLACK_LINK_RE.sub(slack, text)
    text = URL_RE.sub(bare, text)
    return text, links


def merge_links(*groups) -> list[tuple[str, str]]:
    """In order, each URL once, at most two; an answer link always makes the cut."""
    seen, out = set(), []
    for url, label in (x for g in groups for x in (g or [])):
        if url and url not in seen:
            seen.add(url)
            out.append((url, label or label_for(url)))
    out.sort(key=lambda x: x[1] != "Answer here")
    return out[:MAX_LINKS]


def _drop_paren(m) -> str:
    inner = m.group(1)
    return "" if (TASK_ID_RE.search(inner) or COMMAND_RE.search(inner) or "/" in inner) else m.group(0)


def strip_machine(text: str) -> tuple[str, bool]:
    """Ids, commands, counters and cut-off quotes out of a sentence. Says whether a command went.

    A task id in the subject's place reads as "the task"; anywhere else (in brackets, after a link)
    it is simply gone. A command becomes "one command" and the note says it is in the thread."""
    had_cmd = False
    t = TRUNCATED_QUOTE_RE.sub("", text)
    t = re.sub(r"\s*\(([^()]*)\)", _drop_paren, t)
    t = re.sub(r"\s*\(\s*" + TASK_ID + r"\s*\)", "", t)
    t = re.sub(r"(?:\b(?:the\s+)?task\s+)?" + TASK_ID + r"(?=\s+[a-z])", "the task", t, flags=re.I)
    # after a preposition it is still the object of the sentence ("grant more to t_…?"): "the task"
    t = re.sub(r"\b(to|for|on|of|about|with|from|in)\s+(?:the\s+)?(?:task\s+)?" + TASK_ID, r"\1 the task", t, flags=re.I)
    t = TASK_ID_RE.sub("", t)

    def cmd(_m):
        nonlocal had_cmd
        had_cmd = True
        return "one command"
    t = CODE_RE.sub(cmd, t)
    t = COMMAND_SPAN_RE.sub(cmd, t)
    t = CHECKS_RE.sub(lambda m: f"all {m.group(1)} checks passed", t)
    t = COUNTER_RE.sub("", t)
    t = ELLIPSIS_RE.sub("", t)
    return t, had_cmd


def tidy(text: str) -> str:
    """What the removals left behind: empty brackets, doubled or dangling punctuation and spaces."""
    t = re.sub(r"\(\s*[,;:]?\s*\)|\[\s*\]", "", text)
    t = re.sub(r"\s+([,.;:?!])", r"\1", t)
    t = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", t)
    t = re.sub(r"([,;:])(?=[.?!])", "", t)
    t = re.sub(r"([,;:])\s*\)", ")", t)
    t = re.sub(r"[ \t]{2,}", " ", t).strip(" ,;:")
    t = re.sub(r"[,;:]?\s+(?:see|in|at|from|under|via|here|logs? at)$", "", t).rstrip(" ,;:")
    t = LABEL_RE.sub("", t).strip(" ,;:")
    return t[:1].upper() + t[1:] if t and t[0].islower() and not re.match(r"v\d|[a-z]+\d", t) else t


def sentence(text: str) -> str:
    """One finished sentence: it ends with its own punctuation."""
    t = text.strip()
    return t if not t or t[-1] in ".?!" or t.endswith("*") and t[-2:-1] in ".?!" else t + "."


def flat(text: str) -> str:
    """A list written as lines, as one run of prose: no list markers, no line breaks."""
    lines = [re.sub(r"^\s*(?:[-•*◦]|\d+[.)])\s+", "", ln).strip() for ln in (text or "").splitlines()]
    return " ".join(sentence(ln) if i < len(lines) - 1 else ln for i, ln in enumerate(lines) if ln)


def violations(text: str) -> list[str]:
    """What in a rendered note breaks the rule — empty when it reads the way the owner asked."""
    body, _links = take_links(text)
    bad = []
    if TASK_ID_RE.search(SLACK_LINK_RE.sub("", text)):  # an answer URL may carry one
        bad.append("a task id")
    if URL_RE.search(SLACK_LINK_RE.sub("", text)):
        bad.append("a bare URL")
    if re.search(r"^\s*[-•*]\s", text, re.M):
        bad.append("a bulleted line")
    if "…" in text or "..." in body:
        bad.append("an ellipsis")
    if len(SLACK_LINK_RE.findall(text)) > MAX_LINKS:
        bad.append("more than two links")
    if COMMAND_RE.search(body):
        bad.append("a command")
    return bad
