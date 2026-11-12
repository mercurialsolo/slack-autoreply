#!/usr/bin/env python3
"""What Solo itself posts to the console (scripts/outbound.py): said once, outcome first, plain text.
No Slack, no tokens.

Owner, 2026-11-11: "Human readable messaging including good formatting, no duplication,
summarization." The three cases below are the worst real posts of the last two days, replayed
through the one composer every console notice now goes through.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import note_rules as R  # noqa: E402
import outbound as O  # noqa: E402

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}\n{detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


def prose(text):
    """The lines a reader reads as prose: not quotes, not code."""
    return "\n".join(ln for ln in R.CODE_RE.sub("", text).splitlines() if not ln.lstrip().startswith(">"))


# 1. The budget dump of 2026-11-10: task ids in the prose, a bare ask URL.
BUDGET = """❓ Needs an answer on t_d1d740fc: Retro: wire review noise tripwire into reviewer briefs (t_d1d740fc)
Operator (budget): the task hit its budget again after one operator grant.
Grant more or stop it? https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc"""
h, rest = O.compose(BUDGET)
check("budget: no task id left in the prose", not re.search(R.TASK_ID, R.SLACK_LINK_RE.sub("", h)), h)
check("budget: the ask URL is a text link, the id inside it kept", "<https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc|Answer here>" in h, h)
check("budget: no bare URL", not R.URL_RE.search(R.SLACK_LINK_RE.sub("", h)), h)
check("budget: short, so one message and no thread", rest == "", rest)
check("budget: the words around the id read on", "Needs an answer on the task: Retro: wire review noise tripwire into reviewer briefs" in h, h)

# 2. The release note of 2026-11-10, as an SR hand-off posted it twice in one go: bare URLs,
#    a repeated line, long enough to fold.
RELEASE = """v0.771.0 is live on Helsinki. Hillsboro stays on v0.768.0 because the canary holds it.

What got better: replies start 0.3 s sooner on long threads. Search finds older messages across archived channels.
What got better: replies start 0.3 s sooner on long threads. Search finds older messages across archived channels.
Alerts: the console shows a quieter digest for repeated failures.
Drafts keep their formatting when the owner edits on mobile.
Setup needs one fewer step for a new workspace since the token check now runs first.

Changelog: https://github.com/mercurialsolo/slack-autoreply/compare/v0.768.0...v0.771.0
Releases: https://github.com/mercurialsolo/slack-autoreply/releases
e2e gates ran on Helsinki and Hillsboro with the smoke suite and the soak, and every one of them passed.
""" + "\n".join(f"Box {i} took the build without a restart and reported healthy within a minute." for i in range(8))
h, rest = O.compose(RELEASE)
check("release: long, so it folds", rest != "" and h.endswith(O.MORE), h)
check("release: the head is the outcome, in two lines", h == "v0.771.0 is live on Helsinki. Hillsboro stays on v0.768.0 because the canary holds it.\n" + O.MORE, h)
check("release: the repeated line is said once", rest.count("What got better") == 1, rest)
check("release: links are text links", "<https://github.com/mercurialsolo/slack-autoreply/compare/v0.768.0...v0.771.0|What changed>" in rest
      and "<https://github.com/mercurialsolo/slack-autoreply/releases|Release page>" in rest, rest)
check("release: no bare URL anywhere", not R.URL_RE.search(R.SLACK_LINK_RE.sub("", h + rest)), rest)
check("release: nothing lost but the duplicate", all(f"Box {i} " in rest for i in range(8)) and "Setup needs" in rest)

# 3. The compose-failure notice: Solo could not draft, so the agent's request is quoted whole —
#    it used to be cut at 1,200 characters mid-word. Quoted lines are someone's words: untouched.
INTENT = "\n".join(f"> step {i}: post the digest for t_abc12345 to #ops, see https://example.com/run/{i}" for i in range(30))
NOTICE = ("⚠️ Couldn't draft the message scout asked for in #ops: the writer timed out twice. "
          "Nothing was sent. Reply here with the words, or ask scout to try again.\n\n" + INTENT)
h, rest = O.compose(NOTICE)
check("failure notice: folds, and the head says what happened and what to do",
      h.startswith("⚠️ Couldn't draft the message scout asked for") and h.endswith(O.MORE) and ">" not in h, h)
check("failure notice: the quote is whole, every line, word for word", rest == INTENT, rest[:300])

# The rules, one at a time.
check("plain: code spans and existing links are left exactly", O.plain("run `git push t_abc12345` or <https://x.io/a|see>")
      == "run `git push t_abc12345` or <https://x.io/a|see>")
check("plain: 'for task t_…' leaves with its words", O.plain("Done for task t_abc12345, see the log.") == "Done for the task, see the log.")
check("plain: an id in brackets goes", O.plain("Merged the fix (t_abc12345).") == "Merged the fix.")
check("compose: code fences are kept as written, repeats and all",
      O.compose("x\n```\nsame\nsame t_abc12345\n```")[0] == "x\n```\nsame\nsame t_abc12345\n```")
check("compose: fold=False keeps a long draft in one message", O.compose("a. " * 600, fold=False)[1] == "")
check("compose: a short notice is unchanged apart from the rules", O.compose("All good.") == ("All good.", ""))

# The ledger: said once, across restarts, and a failed post may try again.
with tempfile.TemporaryDirectory() as tmp:
    p = Path(tmp) / "outbound.json"
    k = O.key("say", "scout", "#ops", "The deploy  finished")
    check("key: whitespace and case do not make a new message", k == O.key("say", "scout", "#ops", "the deploy finished"))
    check("key: another place is another message", k != O.key("say", "scout", "#eng", "the deploy finished"))
    a = O.Once(p)
    check("once: the first claim posts", a.claim(k, 60, now=1000) is None)
    a.record(k, ts="1.1")
    prev = O.Once(p).claim(k, 60, now=1030)
    check("once: a second claim inside the window, from a new instance, gets the first post back",
          prev is not None and prev.get("ts") == "1.1", prev)
    check("once: after the window it may post again", O.Once(p).claim(k, 60, now=1100) is None)
    O.Once(p).release(k)
    check("once: a released key posts again at once", O.Once(p).claim(k, 60, now=1101) is None)
    p.write_text("not json")
    check("once: a broken ledger is a fresh one, not a crash", O.Once(p).claim("z", 60) is None)

# Review round 1: the rewrites must not touch what is not prose.
check("plain: an id inside a path stays", O.plain("log is at /tmp/t_ab12cd34/run.log") == "log is at /tmp/t_ab12cd34/run.log")
check("plain: indentation is left as it was", O.plain("    step  two") == "    step two")
check("plain: a ratio's colon stays where it was", O.plain("ratio a :b") == "ratio a :b")
check("plain: no leading space where a task id began the line", O.plain("task t_ab12cd34 finished") == "task finished")
_lnk = ("Release is out. See <https://x.example/r|the release page> for notes. " + "More detail follows here. " * 25)
_h, _r = O.compose(_lnk + "\n\n" + "tail " * 150)
check("fold: a head with a link is still cut to two sentences", _h.startswith("Release is out. See <https://x.example/r|the release page> for notes.\n"), _h[:160])
_bold = "*Release went out. Then we waited. And checked.* " + "More detail follows here. " * 25
_h2, _ = O.compose(_bold + "\n\n" + "tail " * 150)
check("fold: a cut that would split bold in two is not made", _h2.startswith(_bold.strip()), _h2[:80])
_multi = "\n".join(f"Line {i} of a status. It ends." for i in range(15))
_h3, _ = O.compose(_multi + "\n\n" + "tail " * 150)
check("fold: a multi-line head keeps its lines", _h3.count("\n") >= 14, _h3[:80])

# Review round 1 (max): what the rewrites must keep.
check("plain: a < b … c > d is prose, and the code inside it is not leaked as a placeholder",
      O.plain("if a < b and `c` > d") == "if a < b and `c` > d")
check("plain: 'Factory task t_… for' keeps every word but the id",
      O.plain("Factory task t_abc12345 for Priyas bug") == "Factory task for Priyas bug")
check("plain: an id that begins a sentence becomes 'the task'", O.plain("t_abc12345 finished") == "the task finished")
check("plain: a file named for a task stays", O.plain("Logs: t_abc12345.log") == "Logs: t_abc12345.log")
check("plain: mentions and specials are kept", O.plain("<@U123> and <!here> look") == "<@U123> and <!here> look")
check("plain: bold after a URL stays outside the link", O.plain("See https://x.io/a*").endswith("|Open it>*"),
      O.plain("See https://x.io/a*"))
check("unlinked: a ping outside Slack reads the label, or the bare URL",
      O.unlinked("see <https://x.io/a|the page> and <https://y.io>") == "see the page and https://y.io")
with tempfile.TemporaryDirectory() as tmp:
    o = O.Once(Path(tmp) / "outbound.json")
    check("once: a claim records what it was given", o.claim("k", 60, now=1000, pid=7) is None)
    check("once: retry=False keeps the earlier claim", o.claim("k", 60, now=1001, retry=lambda p: False) == {"at": 1000, "pid": 7})
    check("once: retry=True takes it over in the same step", o.claim("k", 60, now=1002, retry=lambda p: p["pid"] == 7, pid=8) is None)
    check("once: the take-over is recorded, so a third try is blocked", o.claim("k", 60, now=1003)["pid"] == 8)
check("said: dropped said nothing, sent did",
      not O.said({"outcome": {"status": "dropped"}}) and O.said({"outcome": {"status": "sent"}}))
check("said: a failed item said nothing", not O.said({"status": "failed", "outcome": {"status": "sent"}}))

print(f"\n{passed} checks passed")
