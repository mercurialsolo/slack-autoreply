#!/usr/bin/env python3
"""How a note's mrkdwn reads in Slack (scripts/note_mrkdwn.py, through owner_note). No Slack, no tokens.

Owner, 2026-11-19, on a factory goal-sweep note: a literal `*Done:`, `-&gt` where an arrow was meant,
a doubled ✅ ✅, a bullet holding a second message, a 🔴 on a note that said "no new failures", and
earlier that day a whole report as one raw paragraph in Details. Both inputs are fixtures below.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import note_mrkdwn as M  # noqa: E402
import owner_note as N  # noqa: E402

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}\n{detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


# The screenshot's note, as the factory sent it (no --status).
SWEEP = """✅ *Done: Goal sweep: read CALL-REVIEWs since 16:20Z.
- ✅ No new call failures since 16:20Z (G1-G5 unchanged).
- Known issue: reply time p50 -&gt; 2.1 s on mini, already queued as t_9f3a21bc.
- ✅ ✅ Checked 14 calls; *Done: nothing new to file. Release-watch: v0.812.0 still green, canary 5/5 -&gt; promote at 21:00Z.
- Next sweep at 22:20Z.
"""

# 2026-11-19: a whole report that reached Details as one raw paragraph.
RAW_REPORT = (
    "Release-watch report:\n"
    "v0.812.0 has been on the canary since 14:00Z and every check so far is green. "
    "The p50 reply time went from 1.8 s -> 2.1 s on mini, which is inside the budget but worth watching. "
    "Two calls retried once and then succeeded; neither needs a task. "
    "The promote is planned for 21:00Z unless the canary goes red &amp;&amp; stays red for 10 minutes.\n"
    "\n"
    "**Next**\n"
    "Sweep again at 22:20Z."
)


def all_text(r):
    return r["text"] + "\n" + r["details"]


# --- the screenshot -----------------------------------------------------------------------------
r = N.render(N.from_text(SWEEP, "factory"))
text = all_text(r)
check("screenshot: the icon is the sender's ✅, not a 🔴 from the word 'failures'",
      r["text"].startswith("✅ ") and "🔴" not in r["text"], r["text"])
check("screenshot: no literal *Done: anywhere", "*Done" not in text, text)
check("screenshot: no HTML entity reaches the owner as text", "&gt;" not in text and "&amp;" not in text, text)
check("screenshot: -&gt; reads as an arrow", "p50 → 2.1 s" in text and "5/5 → promote" in text, text)
check("screenshot: no doubled emoji", "✅ ✅" not in text and "✅✅" not in text, text)
check("screenshot: every * pairs up on its line",
      all(line.count("*") % 2 == 0 for line in text.split("\n")), text)
check("screenshot: the bullet that held two messages is two facts in Details",
      "• ✅ Checked 14 calls" in r["details"] and "• Release-watch: v0.812.0 still green" in r["details"],
      r["details"])
check("screenshot: Details are • bullets, not - or raw", not re.search(r"^\s*- ", r["details"], re.M), r["details"])
check("screenshot: the main message has no bullets", not re.search(r"^\s*[-•*] ", r["text"], re.M), r["text"])
check("an explicit --status still wins over the sender's icon",
      N.render(N.from_text(SWEEP, "factory", status="warn"))["text"].startswith("⚠️ "))

# --- the raw paragraph in Details ---------------------------------------------------------------
d = M.details(RAW_REPORT)
check("raw report: 'Release-watch report:' is a *Section* heading", d.startswith("*Release-watch report*\n"), d)
check("raw report: **Next** is a *Section* heading", "\n*Next*\n" in d, d)
check("raw report: the paragraph is one • per sentence",
      sum(1 for line in d.split("\n") if line.startswith("• ")) == 4, d)
check("raw report: no line is a dense paragraph", max(len(line) for line in d.split("\n")) <= 200, d)
check("raw report: -> is an arrow, &amp;&amp; is escaped once", "1.8 s → 2.1 s" in d and "&amp;&amp; stays" in d, d)

# --- icons --------------------------------------------------------------------------------------
want = {"ok": "✅", "warn": "⚠️", "failed": "🔴", "held": "⏸️", "shipping": "🚀", "ask": "❓", "info": "ℹ️"}
check("one icon per status, as the owner listed them", N.ICONS == want, N.ICONS)
check("a negated failure is not a failure", N.status_of("No new call failures since 16:20Z.") != "failed")
check("…nor is 'nothing failed'", N.status_of("Nightly ran, nothing failed.") != "failed")
check("…but a real one still is", N.status_of("Two deploys failed on mini.") == "failed")
check("the sender's leading icon is the status", N.sender_status("🔴 deploy broke") == "failed"
      and N.sender_status("⚠️ disk at 90%") == "warn" and N.sender_status("📨 a clip") == "info")
check("a leading label is the status too", N.sender_status("*Done:* swept") == "ok")
check("a later 🔴 line is not hidden by a first ✅",
      N.from_text("✅ helsinki deployed v1.4\n🔴 hillsboro failed: disk full", "factory").status == "failed")
check("…nor a failure after a Done: label", N.from_text("Done: shipped v2; hillsboro rollback failed", "f").status == "failed")
check("…nor a failed point under a ✅ headline",
      N.from_text("x", "f", headline="✅ Goal sweep finished", points=["hillsboro deploy failed"]).status == "failed")
check("'no idea what failed' is a failure", N.status_of("No idea what failed on helsinki") == "failed")
check("'never seen this error' is a failure", N.status_of("Never seen this error before on helsinki") == "failed")
check("no icon, no label: no sender status", N.sender_status("Disk at 40%.") is None)
check("a command or a link in the note does not set its icon",
      N.from_text("✅ Done: v2.3 is live on hillsboro.\nTo undo: `dev/rollback.sh 2.3`", "f").status == "ok"
      and N.from_text("Merged #2708. CI run: https://github.com/o/r/actions/runs/1/failed-jobs", "f").status == "ok")
check("a zero counter in a list is not a failure",
      N.from_text("Canary green on hillsboro, v2.3 is live.\n- passed: 655\n- failed: 0", "f").status == "ok")
check("…but a non-zero one still is",
      N.from_text("Canary green on hillsboro, v2.3 is live.\n- passed: 653\n- failed: 2", "f").status == "failed")

# --- escaping and arrows ------------------------------------------------------------------------
check("an entity already written is not escaped twice", M.escape("a &gt; b &amp;amp; c") == "a &gt; b &amp; c",
      M.escape("a &gt; b &amp;amp; c"))
check("-> and => and <- are arrows", M.escape("a -> b => c <- d") == "a → b → c ← d", M.escape("a -> b => c <- d"))
check("a Slack link, mention and code span are left as written",
      M.inline("see <https://x.io/a?b=1&c=2|the run> for <@U123>, `a -> b`")
      == "see <https://x.io/a?b=1&c=2|the run> for <@U123>, `a -> b`")
check("a stray < is escaped, a real link is not", M.inline("p99 <2 s, <https://x.io|log>") == "p99 &lt;2 s, <https://x.io|log>")

# --- emoji and markup ---------------------------------------------------------------------------
check("✅ ✅ reads ✅", M.dedup_emoji("✅ ✅ Checked") == "✅ Checked")
check("⚠️⚠️ reads ⚠️", M.dedup_emoji("⚠️⚠️ low") == "⚠️ low")
check("two different icons both stay", M.dedup_emoji("✅ 🚀 go") == "✅ 🚀 go")
check("an unclosed * goes", M.balance("*Done: nothing new") == "Done: nothing new", M.balance("*Done: nothing new"))
check("a closed *bold* stays", M.balance("it is *done* now") == "it is *done* now")
check("markdown **bold** is Slack *bold*", M.balance("**Next** step") == "*Next* step")
check("snake_case is not italics and is not touched", M.balance("set max_retry_count to 3") == "set max_retry_count to 3")
for t in ["config lives in ~/.slack-autoreply/.env", "took ~5 min", "glob *.py files", "2 * 3 * 4 = 24",
          "edit __init__.py now", "user_id and _private"]:
    check(f"plain text is not markup: {t!r}", M.inline(t) == t, M.inline(t))
check("'&region=' is text, not the ® entity", M.inline("x=1&region=eu") == "x=1&amp;region=eu", M.inline("x=1&region=eu"))

# --- bullets ------------------------------------------------------------------------------------
check("'and N more' is not a fact", M.bullets(["Box A is up", "…and 3 more in the thread"]) == ["Box A is up"],
      M.bullets(["Box A is up", "…and 3 more in the thread"]))
check("a trailing 'and 2 more' comes off a fact", M.split_item("Box A is up, and 2 more") == ["Box A is up"],
      M.split_item("Box A is up, and 2 more"))
for t in ["Waiting on 2 others", "helsinki needs 2 more"]:
    check(f"a count that is the fact stays: {t!r}", M.split_item(t) == [t], M.split_item(t))
check("a point that is just a number is still a fact", M.bullets(["42%", "99.9% → 99.2%"]) == ["42%", "99.9% → 99.2%"])
check("cap=0 is no bullets", M.bullets(["a1", "b2"], cap=0) == [])
check("at most 3 bullets", len(M.bullets(["a1", "b2", "c3", "d4", "e5"])) == 3)
check("a bullet holding two messages is two facts",
      M.split_item("Checked 14 calls. ✅ Release-watch green.") == ["Checked 14 calls.", "✅ Release-watch green."],
      M.split_item("Checked 14 calls. ✅ Release-watch green."))
check("a version number does not split a fact", M.split_item("v0.812.0 still green.") == ["v0.812.0 still green."])
check("explicit points that are counts never reach the note",
      "more in the thread" not in all_text(N.render(N.from_text(
          "Swept the boxes.", "factory", headline="Swept the boxes.",
          points=["Box A is fine.", "Box B is fine.", "and 4 more in the thread"]))))

steps = M.details("Steps:\n1. stop the service\n2. run make install\n  - nested one")
check("numbered steps keep their numbers, a nested item its nesting",
      steps == "*Steps*\n1. stop the service\n2. run make install\n    ◦ nested one", steps)

# --- code blocks in Details ---------------------------------------------------------------------
fence = "```\n- a -> b\n**x**\n```"
check("a code block in Details is left exactly as written", fence in M.details("Log:\n" + fence), M.details("Log:\n" + fence))

print(f"\n{passed} checks passed")
