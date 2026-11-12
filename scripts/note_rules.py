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
MARK_TAIL_RE = re.compile(r"[*_~\u201d\u2019]+$")


def url_tail(url: str, before: str = "") -> tuple[str, str]:
    """A URL and what it swallowed of the sentence. A closing quote, `*` or `~` is the sentence's.
    A closing `_` is too, but only while italics are open before the URL ("_see https://x_");
    otherwise it is the URL's own: https://example.com/name_ keeps its underscore."""
    m = MARK_TAIL_RE.search(url)
    if not m:
        return url, ""
    cut = len(url)
    for i in range(len(url) - 1, m.start() - 1, -1):
        c = url[i]
        if c != "_" or before.count("_") % 2 == 1:
            cut = i
        else:
            break
    return url[:cut], url[cut:]
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
# Any leading pictograph (the factory starts its headlines with its own ✅ / 🔴 / ❓), once or twice:
# the renderer adds exactly one icon of its own.
EMOJI = "[\u2190-\u21ff\u2300-\u27bf\u2b00-\u2bff\U0001F000-\U0001FAFF]"
ICON_RE = re.compile(r"^\s*(?:" + EMOJI + r"[\ufe0f\u200d]*\s*)+")
# A status word the factory writes before its headline ("Done: …", "Needs your answer: …"): the icon
# already says it. Taken off once, at the start only.
STATUS_LABEL_RE = re.compile(r"^\s*(?:done|failed|failure|blocked|held|on hold|shipped|shipping|live|warning|"
                             r"needs (?:your|an) (?:answer|call|decision|ok)|needs you|question|ask|fyi|info|cancelled|canceled|fix held)"
                             r"(?: on [\w.-]+)?\s*[:\-–—]\s+", re.I)  # "Done on mini: …" (the factory's notify)
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


# A link label that is only a call to open it ("Answer here", "Details"): at the end of a sentence it
# goes with its link; any other label is part of the sentence ("merge main into #2767 and land").
CTA_LABEL_RE = re.compile(r"^(?:answer here|answer|here|link|details?|open(?: it)?|what changed|release page|"
                          r"the (?:pr|issue|ci run|log)|dashboard|view|see|click here)$", re.I)
PR_NUM_RE = re.compile(r"^#\d+$")
SHA_LABEL_RE = re.compile(r"^[0-9a-f]{7,40}$")
WORDS_FOR = {"Answer here": "the answer page", "Open it": "the link"}


def _in_sentence(label: str, url: str, before: str, after: str) -> str:
    """What a link leaves in its sentence: its label when the label carries meaning, "the PR" for a
    bare URL in mid-sentence, nothing when it ends the sentence or is a sha. Never a hole."""
    tail = not re.search(r"[A-Za-z0-9#]", re.split(r"(?<=[.!?])\s|\n", after, 1)[0])
    if label and not URL_RE.fullmatch(label):
        if SHA_LABEL_RE.match(label):
            return "\x07"  # a sha: gone, with the "at" before it (see the caller)
        if PR_NUM_RE.match(label):
            return label if ("/pull/" not in url or re.search(r"\b(?:PRs?|pull request)\s*$", before, re.I)) else f"PR {label}"
        if tail and CTA_LABEL_RE.match(label):
            return ""
        return label
    if tail:
        return ""
    lb = label_for(url, before)
    return WORDS_FOR.get(lb, lb[:1].lower() + lb[1:])


def take_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Pull every link out of a sentence — `<url|label>` and bare URLs alike — with a label for each.

    The sentence keeps the link's words: a label in mid-sentence stays as text ("#2767" reads "PR
    #2767"), a bare URL in mid-sentence reads "the PR" / "the issue"; only a link that ends its
    sentence with nothing but "Answer here" is taken out whole. A sentence never keeps a hole."""
    links: list[tuple[str, str]] = []

    def slack(m):
        label = (m.group(2) or "").strip()
        links.append((m.group(1), label if label and not URL_RE.fullmatch(label) and not SHA_LABEL_RE.match(label)
                      and not PR_NUM_RE.match(label) else label_for(m.group(1), text[:m.start()])))
        return " " + _in_sentence(label, m.group(1), m.string[:m.start()], m.string[m.end():]) + " "

    def bare(m):
        url, tail = url_tail(m.group(0), m.string[:m.start()])
        links.append((url, label_for(url, m.string[:m.start()])))
        return " " + _in_sentence("", url, m.string[:m.start()], tail + m.string[m.end():]) + " " + tail
    text = SLACK_LINK_RE.sub(slack, text)
    text = URL_RE.sub(bare, text)
    text = re.sub(r"(?:\s+(?:at|on|in|from)\b)?\s*\x07", " ", text)  # "Review #3558 at <sha>" reads "Review #3558"
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


# What a scrub leaves when it takes a word out of the middle of a sentence: "set =off in  on the call
# box", "board it via  or no", "Reply yes to yes". A sentence with one is handed to the model whole.
HOLE_RE = re.compile(
    r"\S[ \t]{2,}\S|(?:^|\s)=\S|\S=(?:\s|$)"
    r"|\b(?:via|into|at|from|with|by|of)\s+(?:and|or|in|on|at|via|into|from|to|with|by|of)\b"
    r"|\b(?:in|on)\s+(?:in|on|at|via|into)\b|\bto\s+(?:and|or|at|via|into|from|by|of)\b"
    r"|\b(?:via|at|with|by|into)\s*[,;.!]|\b(?:yes|no)\s+to\s+(?:yes|no)\b"
    r"|\b(?:via|at|with|into)\s*$|\b(?:via|at|into|by)\s*\(", re.I)
# The holes a finished note can be judged by alone: ordinary English never has these ("log in on",
# "with or without" do occur, so the wider HOLE_RE is only read as a change a scrub made, in clean()).
CUT_RE = re.compile(r"\S[ \t]{2,}\S|(?:^|\s)=\S|\S=(?:\s|$)|\b(?:via|into)\s+(?:and|or|in|on|at|via|into|from|to|by|of)\b"
                    r"|\b(?:via|into)\s*(?:[,;.!]|$)|\b(?:at|with|from)\s*[.;!,](?!\w)|\b(?:at|from|by)\s+(?:and|or)\b"
                    r"|\b(?:yes|no)\s+to\s+(?:yes|no)\b|\b(?:via|at|into|by)\s*\(", re.I)
RAW_LINK_RE = re.compile(r"&lt;https?://|<https?://[^>|]*\|[^>]*$", re.M)


def holes(text: str) -> bool:
    """Does a sentence read as if words were cut out of it?"""
    return bool(HOLE_RE.search(SLACK_LINK_RE.sub("link", text or "")))


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
    if RAW_LINK_RE.search(text):
        bad.append("a raw mrkdwn link")
    if any(CUT_RE.search(SLACK_LINK_RE.sub("link", re.sub(r"^_.*_$", "", ln))) for ln in text.splitlines()):
        bad.append("a hole")
    if re.search(r"^\s*(?:" + EMOJI + r"[\ufe0f\u200d]*\s*){2,}", text):
        bad.append("two icons")
    return bad
