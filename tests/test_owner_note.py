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

# Review round 1.
_o = N.render(N.from_text("Ship it?", "release", options=["Ship it", "Stop and revert"]), buttons=False)["text"]
check("choices in words keep their own words: 'Stop and revert' is one choice", "*Ship it* or *Stop and revert*" in _o, _o)
_n = N.from_text("context", "ops", headline="Deploy blocked; run `kubectl rollout undo deploy/api`", details="logs say the api pod crashlooped")
check("a command taken out of the body is in the thread, even with --details", "kubectl rollout undo" in _n.details
      and "kubectl" not in N.render(_n)["text"], repr(_n))
_n = N.from_text("Canary green: 5/5", "release")
check("a status line that is only a counter keeps its counter", "5/5" in _n.headline, repr(_n))
_n = N.from_text("Budget ask: grant more to t_8d468171? https://h/ask/abc", "factory")
check("a task id that ends a question reads 'the task', not a dangling 'to?'", "grant more to the task?" in N.render(_n)["text"]
      and "t_8d" not in N.render(_n)["text"], N.render(_n)["text"])
_n = N.from_text("Should I ship v1.5 now (t_8d468171)? Reply yes or no", "release")
check("'Reply yes or no' after a question is dropped, the question stays the headline",
      "Reply" not in N.render(_n)["text"] and "ship" in _n.headline, repr(_n))
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["NOTE_SHAPER"] = "auto"
check("auto with no key uses the CLI", note_shape.mode() == "claude")
check("the shaper's statuses are the renderer's", note_shape.STATUSES == N.STATUSES)

# --- Solo 1.4.1: owner, 2026-11-11, on the 1.4.0 factory digests: "same here repeat emojis, repeated
# headlines to sections, half cut off messages". Inputs are real factory text: the ASK task t_76fb256e
# raised (as staff-factory reporter/owner_digest.say_args sends it) and the Done notify for t_ac7d0418
# (staff-factory docs/commands.md notify template). Nothing below is a reply the code writes itself.
os.environ["NOTE_SHAPER"] = "off"
VAULT_LINK = "https://factory.staffai.workers.dev/#factory/ask/t_76fb256e"
VAULT_ASK = ("ASK: [high] Owner: set AGENTVM_VAULT_PER_CUSTOMER=off in ~/.config/agentvm/env on the call box and on "
             "hillsboro (or install customer 0000…0001's login) so #2708 can board? | yes, then I board it via "
             "dev/pr-merge.sh 2708 | keep #2708 parked")
_q, *_opts = VAULT_ASK.split("] ", 1)[1].split(" | ")
_point = f"❓ {_q[:109].rstrip()}… → {VAULT_LINK}"  # owner_digest cut(q, 110): the factory's own cut
VAULT_SPEC = {"status": "ask", "headline": "Factory update: 1 decision for you", "points": [_point],
              "details": _point, "options": _opts, "link": VAULT_LINK}
DONE_3558 = ("Done on mini: Review #3558 at 0b5bdc6 (agentvm). The train drop for #3558 was a CI infrastructure flake "
             "(apt could not fetch `age`), not a code fault; the PR's own CI is green on its head. I labelled it "
             "release-fix, recorded the delta review of the Google-host fix, and boarded it on the ci-release train "
             "again. It now waits for the train to merge it. (t_ac7d0418)")


def model(reply):
    return lambda prompt: json.dumps(reply)


def plain(r):
    return r["text"]


check("a scrub that leaves '=off in  on' is a hole", R.holes("set =off in  on the call box"), "")
check("'via  or no' is a hole", R.holes("board it via  or no"), "")
check("'at (' left by a removed sha is a hole", R.holes("Review #3558 at (agentvm)"), "")
check("a plain sentence has no hole", not R.holes("Turn the vault switch off on the call box?"), "")
check("violations name a hole", "a hole" in R.violations("Set =off in  on the box."), R.violations("Set =off in  on the box."))
check("violations name a raw mrkdwn link left as text", "a raw mrkdwn link" in R.violations("Reply &lt;https://h/x|Answer here&gt;"),
      R.violations("Reply &lt;https://h/x|Answer here&gt;"))
_v = N.option_words("yes, then I board it via dev/pr-merge.sh 2708")
check("a choice keeps its plain words, without the script path or a dangling 'via'", _v == "Yes, then I board it", _v)

for _buttons in (True, False):
    _n = note_shape.build(_q, "owner-digest", VAULT_SPEC, buttons=_buttons)
    _r = N.render(_n, buttons=_buttons)
    _t = plain(_r)
    check(f"vault digest (buttons={_buttons}): one icon, no hole, no raw link, no '…' in the body",
          not R.violations(_t) and "…" not in _t and "&lt;" not in _t and "=off in" not in _t, _t)
    check(f"vault digest (buttons={_buttons}): the choices are words, the script path is not", "Keep #2708 parked" in _t
          and "pr-merge" not in _t and "Yes, then I board it" in _t, _t)
    check(f"vault digest (buttons={_buttons}): the details are in the thread once", _r["details"].count("AGENTVM_VAULT") == 1
          and "AGENTVM_VAULT" not in _t, repr(_r["details"]))
_r = N.render(note_shape.build(_q, "owner-digest", VAULT_SPEC))
_labels = [e["text"]["text"] for e in _r["blocks"][1]["elements"]]
check("vault digest buttons read as words; the button still answers with the agent's own choice",
      _labels[:2] == ["Yes, then I board it", "Keep #2708 parked"]
      and _r["blocks"][1]["elements"][0]["value"] == _opts[0], _labels)

_vm = model({"status": "ask", "headline": "#2708 waits on one setting on the call box and Hillsboro",
             "summary": "The per-customer vault switch has to be off there, or customer 1's login installed, before it can board.",
             "ask": "Turn it off so I can board it, or keep it parked?", "links": [{"url": VAULT_LINK, "label": "Answer here"}]})
_r = N.render(note_shape.build(_q, "owner-digest", VAULT_SPEC, call=_vm))
check("vault digest with the model: its headline, its sentence, the choices as buttons, details once",
      _r["text"] == ("❓ #2708 waits on one setting on the call box and Hillsboro\n"
                     "The per-customer vault switch has to be off there, or customer 1's login installed, before it can board.\n"
                     "👉 Turn it off so I can board it, or keep it parked?\n"
                     "_from owner-digest · details in the thread_")
      and _r["blocks"] and _r["details"].count("AGENTVM_VAULT") == 1, _r["text"])

_r = N.render(note_shape.build(DONE_3558, "factory", {}))
_t = plain(_r)
check("Done #3558 without the model: one icon, no 'Done on mini:' label, no hole left by the sha",
      _t.count("✅") == 1 and "Done on mini" not in _t and "at (" not in _t and "0b5bdc6" not in _t
      and not R.violations(_t), _t)
check("Done #3558: the PR number stays, the task id stays out of the body", "#3558" in _t and "t_ac7d0418" not in _t, _t)
check("Done #3558: the details are in the thread once", _r["details"].count("apt could not fetch") == 1
      and "apt could not fetch" not in _t, _r["details"])
_dm = model({"status": "ok", "headline": "✅ Done: #3558 is back on the release train",
             "summary": "The train dropped it for a CI hiccup, not a code fault; its own CI is green. It now waits for the train to merge it.",
             "ask": "", "links": []})
_r = N.render(note_shape.build(DONE_3558, "factory", {}, call=_dm))
check("Done #3558 with the model: the model's icon and 'Done:' label are not doubled",
      _r["text"] == ("✅ #3558 is back on the release train\n"
                     "The train dropped it for a CI hiccup, not a code fault; its own CI is green. It now waits for the train to merge it.\n"
                     "_from factory · details in the thread_"), _r["text"])

_n = N.from_text("DONE: origin/main merged into <https://github.com/o/r/pull/2767|#2767> with the text brain kept; CI green", "factory")
check("a pulled-out link keeps its label in the sentence", "#2767" in _n.headline, repr(_n))
_n = N.from_text("Merged <https://github.com/o/r/pull/2767|https://github.com/o/r/pull/2767> after review", "factory")
check("a link whose label is its URL reads 'the PR'", "the PR" in N.render(_n)["text"], N.render(_n)["text"])
_r = N.render(N.from_text("Ship v2? <https://h/ask/x|Answer here>", "release"))
check("an Answer-here link is a link, not escaped text", "&lt;" not in _r["text"] and "<https://h/ask/x|" in _r["text"], _r["text"])
_n = N.Note(status="ok", headline="✅ ✅ Done: shipped", summary="It is live.", sender="release")
_g = N.render_group([_n, _n], "release")
check("a digest headline that already has an icon gets exactly one", "✅ ✅" not in _g and "Done:" not in _g, _g)
_e = N.group_thread(N.Note(status="ok", headline="Canary green", summary="All five hosts pass.", details="host 1..5 ok", sender="ops"))
check("a digest thread entry is the summary and details, never the headline again", "Canary green" not in _e
      and "🔎 *Details*" in _e, _e)
_s = N.scrub(N.Note(status="ask", headline="Set =off in  on the box?", sender="ops", reshape=True))
check("scrub replaces a holed headline with a plain one", not R.holes(_s.headline) and _s.headline and not _s.reshape, repr(_s))

_n = N.from_text("Failed: deploy to prod", "ops")
check("a 'Failed:' label still reads failed after the label goes", _n.status == "failed" and "Failed:" not in _n.headline, repr(_n))
check("two choices that read the same keep their own words on the buttons",
      N.choice_words(["Option 1 via scripts/a.sh", "Option 2 via scripts/b.sh"]) != ["Option 1", "Option 1"]
      and len(set(N.choice_words(["run scripts/a.sh", "run scripts/b.sh"]))) == 2)
check("plain English is not a hole", not R.violations("✅ Log in on Helsinki and restart it."), R.violations("✅ Log in on Helsinki and restart it."))
_a = N.Note(status="ask", headline="Board #9?", summary="It waits on you.", sender="ops", options=["Board it", "Park it"])
_e = N.group_thread(_a, named=False)
check("a digest note past the named ones keeps its headline in the thread", "Board #9?" in _e, _e)
check("a digest thread entry keeps the ask", "Board it" in N.group_thread(_a) and "Park it" in N.group_thread(_a), N.group_thread(_a))

print(f"\n{passed} checks passed")
