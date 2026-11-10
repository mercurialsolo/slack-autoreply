#!/usr/bin/env python3
"""Golden examples: how a note to the owner reads (scripts/owner_note.py). No Slack, no tokens.

Owner, 2026-11-04: "messages are not well formatted here for readability … use icons, emojis and
language that is more human readable rather than just technical details." Each case is a note an
agent really sends, and the exact message the owner sees. Change a golden only on purpose.

Owner, 2026-11-10, the rule every note now renders to (scripts/note_rules.py): one or two plain
sentences, at most one 👉 question with the choices in words, no ids / shas / counters / commands in
the body, at most two text links, no bullets, never cut off with "…"; the full note in Details.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import note_rules as R  # noqa: E402
import note_shape  # noqa: E402
import owner_note as N  # noqa: E402

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}\n{detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


def golden(name, intent, want, sender="solo-one", **kw):
    r = N.render(N.from_text(intent, sender, **kw))
    check(name, r["text"] == want, f"--- got\n{r['text']}\n--- want\n{want}")
    return r


TODAY = ("Not live now: Helsinki runs dc10473f18, not e150cfdd06 (a rollback, or a deploy by hand). Live: Helsinki "
         "held at v0.551.0: the no-regression gate was red: reply_heard_p50_ms: 1225 vs median 824 of the last 5, +401 "
         "(band 44.5 from 20) (one smoke run: its resample did not run or did not pass), Hillsboro frozen at v0.537.0")

# 1. a rollback
r = golden("a rollback: 🔴 headline with the box and versions in bold, the why and the next step as sentences",
           "Helsinki rolled back v0.552.0 -> stays on v0.551.0: reply_heard_p50_ms: 1225 vs median 824 of the last 5, "
           "+401 (band 44.5 from 20) on one smoke run. Next: the release lane re-runs tonight, see "
           "~/.factory/logs/release-552.log",
           "🔴 *Helsinki* rolled back *v0.552.0* → stays on *v0.551.0*\n"
           "Reply time 1.2 s vs 0.8 s usually, +0.4 s on one test run. Next: the release lane re-runs tonight.\n"
           "_from solo-one · details in the thread_")
check("…the numbers, the metric name and the log path are all still in the Details reply",
      all(x in r["details"] for x in ("1225", "reply_heard_p50_ms", "release-552.log")) and r["details"].startswith("🔎 *Details*"))

# 2. a green release
golden("a green release: ✅, short",
       "Helsinki shipped v0.552.0, reply_heard_p50_ms: 812 vs median 824 of the last 5. Canary done, all checks green",
       "✅ *Helsinki* shipped *v0.552.0*, reply time 0.8 s vs 0.8 s usually.\n"
       "Canary done, all checks green.\n"
       "_from solo-one · details in the thread_")

# 3. a frozen box
golden("a frozen box: ⏸️, the ticket and the setting stay in the thread",
       "Hillsboro frozen at v0.537.0 (freeze=owner, set 2026-11-02, ticket ENG-881) until the owner lifts it; nothing ships there",
       "⏸️ *Hillsboro* frozen at *v0.537.0* until the owner lifts it\n"
       "Nothing ships there.\n"
       "_from solo-one · details in the thread_")

# 4. an ask, with buttons
r = golden("an ask: ❓ the context first, then one 👉 question",
           "v0.553.0 is green on Helsinki. Ship it to Hillsboro now, or wait for the freeze to lift?",
           "❓ *v0.553.0* is green on Helsinki.\n"
           "👉 Ship it to Hillsboro now, or wait for the freeze to lift?\n"
           "_from solo-one_",
           options=["Ship now", "Wait"], link="https://factory.example/ask/t_9")
acts = [b for b in r["blocks"] if b["type"] == "actions"][0]["elements"]
check("…the choices are buttons, never 'reply yes/no'",
      [e["text"]["text"] for e in acts] == ["Ship now", "Wait", "Open the answer page"]
      and [e.get("value") for e in acts[:2]] == ["Ship now", "Wait"] and "reply" not in r["text"].lower())
check("…a Yes is the primary button", N.render(N.from_text("Merge #1739?", "x", options=["Yes", "No"]))["blocks"][1]["elements"][0].get("style") == "primary")
off = N.render(N.from_text("Merge #1739?", "x", options=["Yes", "No"]), buttons=False)
check("…with Interactivity still off: the choices in words, no blocks", off["blocks"] is None
      and "👉 *Yes* or *No* — answer in the thread" in off["text"], off["text"])
off = N.render(N.from_text("Merge #1739?", "x", options=["Yes", "No"], link="https://factory.example/ask/t_9"), buttons=False)
check("…and the answer page as a text link, no id after it",
      off["text"].endswith("👉 *Yes* or *No* — <https://factory.example/ask/t_9|Answer here>\n_from x_"), off["text"])
done = N.answered_blocks(r["blocks"], "Wait", "you", "22:41")
check("…once answered the buttons are gone and the choice is shown",
      not [b for b in done if b["type"] == "actions"] and "✔️ *Wait*" in done[-1]["elements"][0]["text"])

# 5. grouping
notes = [N.from_text("Helsinki shipped v0.552.0, all checks green", "solo-one"),
         N.from_text("Hillsboro frozen at v0.537.0 until you lift it", "solo-one"),
         N.from_text(TODAY, "solo-one")]
g = N.render_group(notes, "solo-one")
check("grouping: one message, the worst icon, one sentence per note, no bullets", g ==
      "🔴 *3 updates* from solo-one. ✅ *Helsinki* shipped *v0.552.0*, all checks green. "
      "⏸️ *Hillsboro* frozen at *v0.537.0* until you lift it. "
      "🔴 *Helsinki* is not on the build it should be (a rollback, or a deploy by hand).\n"
      "_each one's detail is in the thread_", g)
check("…more than three becomes 'And N more'", "And 1 more in the thread." in N.render_group(notes + notes[:1], "x"))

# today's message, the one the owner complained about
golden("today's release note, rewritten", TODAY,
       "🔴 *Helsinki* is not on the build it should be (a rollback, or a deploy by hand).\n"
       "*Helsinki* held at *v0.551.0*: the regression check was red: reply time 1.2 s vs 0.8 s usually, +0.4 s. "
       "*Hillsboro* frozen at *v0.537.0*.\n"
       "_from solo-one · details in the thread_")

# what the rules do with machine parts
CMD = ("Needs on the agentvm VM: colima ssh -p agentvm -- sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0 "
       "(resets on VM reboot).")
c = N.render(N.from_text(CMD, "x"))
check("a command leaves the body and stays whole in Details", CMD in c["details"] and "sysctl" not in c["text"]
      and "The command is in the thread." in c["text"], c["text"])
check("code spans are left exactly as written by humanize", "`git push origin release_v2_ms: 1200`" in
      N.humanize("run `git push origin release_v2_ms: 1200` now"))
check("no shas, metric names or log paths in the body",
      not any(x in N.render(N.from_text(TODAY, "x"))["text"] for x in ("dc10473f18", "e150cfdd06", "_ms", "p50")))
check("a short plain note is just its headline, with no thread", N.render(N.from_text("P1 merged", "x")) ==
      {"text": "✅ P1 merged\n_from x_", "blocks": None, "details": ""})
_eight = N.from_text(". ".join(f"Box{i} shipped v1.{i}" for i in range(8)), "x")
check("a long run: two sentences under the headline, the rest in Details",
      _eight.summary.count(". ") <= 1 and "Box7" in _eight.details and "Box7" not in N.render(_eight)["text"])
check("an agent's explicit shape wins over the rules",
      N.render(N.from_text("raw dump 1225ms", "x", status="held", headline="Lisbon held at v0.9", points=["waiting on the gate"]))["text"]
      == "⏸️ *Lisbon* held at *v0.9*\nWaiting on the gate.\n_from x · details in the thread_")
try:
    N.norm_status("purple")
    check("an unknown status is refused", False)
except ValueError:
    check("an unknown status is refused", True)
check("a count after a ms reading stays a count", N.humanize("reply_heard_p50_ms: 1200 vs 800 median of the last 200 runs, build 1234")
      == "reply time 1.2 s vs 0.8 s median of the last 200 runs, build 1234", N.humanize("reply_heard_p50_ms: 1200 vs 800 median of the last 200 runs, build 1234"))
check("…and 'across 1500 calls' too", N.humanize("first_token_ms: 950 across 1500 calls") == "time to first word 0.9 s across 1500 calls")
check("a bare URL becomes a labelled text link", "<https://ci.example.com/runs/run.log|The log>" in N.render(N.from_text("see https://ci.example.com/runs/run.log", "x"))["text"]
      and "<https://github.com/x/y/releases/tag/v1.2.3|Release page>" in N.render(N.from_text("released https://github.com/x/y/releases/tag/v1.2.3", "x"))["text"])
check("Slack's control characters are escaped", "Latency &lt;2 s &amp; error &gt;1%" in N.render(N.from_text("latency <2 s & error >1%", "x"))["text"]
      and "<@U123>" in N.render(N.from_text("ping <@U123> about it", "x"))["text"])
_many = N.render(N.from_text("Pick a box", "x", options=list("ABCDEFG")))
check("more than five choices are all buttons", [e["text"]["text"] for e in _many["blocks"][1]["elements"]] == list("ABCDEFG"))
_long = N.render(N.from_text("x", "x", headline="Deploy " + "a " * 2000, options=["Yes", "No"]))
check("a section never passes Slack's 3000 characters", len(_long["blocks"][0]["text"]["text"]) <= 3000)
_pts = N.from_text("x", "x", headline="h", points=list("abcde"))
check("explicit points: two sentences under the headline, the rest in Details",
      _pts.summary == "A. B." and all(f"\n{c}" in _pts.details for c in "cde"), repr((_pts.summary, _pts.details)))
check("ms on its own becomes seconds", N.humanize("took 1460 ms") == "took 1.5 s" and N.humanize("took 40 ms") == "took 40 ms")

# The two notes of 2026-11-10 that set the rule, replayed through every way in.
BUDGET = """❓ Needs an answer on t_d1d740fc: Retro: wire review noise tripwire into reviewer briefs (t_d1d740fc)
Operator (budget): the task hit its budget again after one operator grant. Burn so far: 274/1,200 turns, review rounds 5/5.
Grant more or stop it? https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc"""
RELEASE = """✅ v0.771.0 live on Helsinki. Hillsboro v0.768.0 (5 behind v0.773.0, the canary holds it)
What got better:
- Speed: replies start 0.3 s sooner on long threads because the warm cache now…
- Search: finds older messages across archived channels that were previously cut…
- Alerts: the console shows a quieter digest for repeated failures that used to…
- Drafts: keep their formatting when the owner edits on mobile which was broken…
- Setup: one fewer step for a new workspace since the token check now runs…
Changelog: https://github.com/mercurialsolo/slack-autoreply/compare/v0.768.0...v0.771.0
Releases: https://github.com/mercurialsolo/slack-autoreply/releases
Checks: 24 passed, 0 failed, 0 warnings
- e2e gates ran on Helsinki and Hillsboro with the smoke suite and the soak…
Details: https://factory.staffai.workers.dev/#release/v0.771.0"""


def reads_to_rule(name, src, out):
    text = out["text"]
    body = text.rsplit("\n_from ", 1)[0]
    plain = R.SLACK_LINK_RE.sub("", body)
    sentences = len([x for x in re.split(r"(?<=[.?!])\s+|\n", R.ICON_RE.sub("", plain)) if re.search(r"\w{2}", x)])
    check(name, not re.search(r"\bt_[0-9a-f]", plain) and "http" not in plain and not re.search(r"^- ", text, re.M)
          and len(R.SLACK_LINK_RE.findall(text)) <= 2 and "…" not in text and 2 <= sentences <= 3
          and R.violations(text) == [] and src in out["details"], f"{text}\n(sentences: {sentences})")


for label, src in (("the budget ask", BUDGET), ("the v0.771.0 release", RELEASE)):
    reads_to_rule(f"replay, no model: {label} reads to the rule", src, N.render(N.from_text(src, "factory"), buttons=False))

_b = N.render(N.from_text(BUDGET, "factory"), buttons=False)["text"]
check("…the budget ask: one question, the answer page as its link", "👉 Grant more or stop it? — <https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc|Answer here>" in _b, _b)
_r = N.render(N.from_text(RELEASE, "factory"), buttons=False)["text"]
check("…the release: the list folded into a sentence, two links", "speed, search and alerts" in _r.lower()
      and "|What changed>" in _r and "Setup" not in _r, _r)

MODEL = {
    BUDGET: {"status": "ask", "headline": "Retro: wire review noise tripwire into reviewer briefs",
             "summary": "The task hit its budget again after one operator grant.", "ask": "Grant more, or stop it?",
             "links": [{"url": "https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc", "label": "Answer here"}]},
    RELEASE: {"status": "ok", "headline": "v0.771.0 is live on Helsinki; Hillsboro stays on v0.768.0 for the canary.",
              "summary": "Replies start sooner on long threads, search reaches archived channels, and repeated failures alert more quietly.",
              "ask": "", "links": [{"url": "https://github.com/mercurialsolo/slack-autoreply/compare/v0.768.0...v0.771.0", "label": "What changed"},
                                   {"url": "https://github.com/mercurialsolo/slack-autoreply/releases", "label": "Release page"}]},
}
for label, src in (("the budget ask", BUDGET), ("the v0.771.0 release", RELEASE)):
    shaped = note_shape.shape(src, call=lambda prompt, src=src: "Here you go:\n" + json.dumps(MODEL[src]))
    check(f"the shaper's answer parses: {label}", shaped and shaped["status"] == MODEL[src]["status"], repr(shaped))
    reads_to_rule(f"replay, with the model: {label} reads to the rule", src,
                  N.render(N.from_text(src, "factory", shaped=shaped), buttons=False))

# a model that ignores the rule is held to it anyway
BAD = {"status": "ask", "headline": "t_d1d740fc: run `factory grant t_d1d740fc --turns 200`",
       "summary": "- burn 274/1,200 turns\n- review rounds 5/5…", "ask": "Grant more or stop it?",
       "links": [{"url": "https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc", "label": "Answer here"},
                 {"url": "https://evil.example/phish", "label": "Login"}]}
_bad = note_shape.shape(BUDGET, call=lambda prompt: json.dumps(BAD))
check("a link the agent never wrote is dropped", _bad and [u for u, _ in _bad["links"]] == ["https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc"], repr(_bad))
_out = N.render(N.from_text(BUDGET, "factory", shaped=_bad), buttons=False)
check("…and its ids, command, counters and bullets are taken out", R.violations(_out["text"]) == []
      and not re.search(r"\bt_[0-9a-f]|274|5/5|^- ", R.SLACK_LINK_RE.sub("", _out["text"]), re.M), _out["text"])
check("bad JSON, an unknown status, or a model that throws: no shape, the rules read it",
      note_shape.parse("not json", BUDGET) is None and note_shape.parse('{"status": "purple", "headline": "x"}', BUDGET) is None
      and note_shape.shape(BUDGET, call=lambda p: 1 / 0) is None)
os.environ["NOTE_SHAPER"] = "off"
check("NOTE_SHAPER=off: no model call", not note_shape.wanted(RELEASE) and note_shape.shape(RELEASE) is None)
os.environ["NOTE_SHAPER"] = "claude"
check("…a short plain line or an agent's own shape needs no model", not note_shape.wanted("P1 merged")
      and not note_shape.wanted(RELEASE, structured=True) and note_shape.wanted(RELEASE))
check("the rule is in the shaper's prompt", R.RULE in note_shape.PROMPT.format(rule=R.RULE, text="x"))

print(f"\n{passed} checks passed")
