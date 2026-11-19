#!/usr/bin/env python3
"""How a model shapes a note to the owner (scripts/note_shape.py, through owner_note). No Slack, no
tokens, no model: each test's model is a function that answers with what a model answered.

Owner, 2026-11-19: the headline says what happened in plain words, its icon from the status; at most
three bullets, each a whole fact, never "and N more"; Details in `*Section*`s, one • per fact, a last
*Needs you* only when something does; ids, shas, p50, CALL-REVIEWs and G1-G5 never in the top message.
The notes below are real factory and watch notes (the sweep from that day's screenshot).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import note_rules as R  # noqa: E402
import note_shape as S  # noqa: E402
import owner_note as N  # noqa: E402

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}\n{detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


def model(answer):
    """A model that answers the shaping prompt with `answer` and has nothing to add to the thread."""
    def call(prompt):
        return json.dumps(answer) if prompt.startswith(S.PROMPT[:40]) else "{}"
    return call


IDS = re.compile(r"\bt_[0-9a-f]{6,}\b|\b[0-9a-f]{7,40}\b")


def top_ok(text):
    """The top message by the owner's rule: no ids (a link's URL is not read), no jargon, at most three bullets, no 'and N more'."""
    return (not R.violations(text) and not IDS.search(R.SLACK_LINK_RE.sub("", text)) and len(re.findall(r"^• ", text, re.M)) <= 3
            and not re.search(r"\b\d+ more\b", text))


# The screenshot's note, as the factory sent it.
SWEEP = """✅ *Done: Goal sweep: read CALL-REVIEWs since 16:20Z.
- ✅ No new call failures since 16:20Z (G1-G5 unchanged).
- Known issue: reply time p50 -&gt; 2.1 s on mini, already queued as t_9f3a21bc.
- ✅ ✅ Checked 14 calls; *Done: nothing new to file. Release-watch: v0.812.0 still green, canary 5/5 -&gt; promote at 21:00Z.
- Next sweep at 22:20Z.
"""
SWEEP_ANSWER = {
    "status": "ok", "headline": "Goal sweep: no new call failures",
    "points": ["Replies on mini are a little slower; a task already covers it.",
               "v0.812.0 is still green on the canary and goes out at 21:00Z.",
               "The next sweep is at 22:20Z.", "Nothing new to file.", "…and 2 more in the thread"],
    "ask": "", "links": [],
    "sections": [
        {"title": "Calls", "facts": ["No new call failures since 16:20Z (G1-G5 unchanged).",
                                     "Checked 14 calls; nothing new to file."]},
        {"title": "Known issue", "facts": ["Reply time p50 -> 2.1 s on mini, already queued as t_9f3a21bc."]},
        {"title": "Release", "facts": ["v0.812.0 still green, canary 5/5 -> promote at 21:00Z."]},
        {"title": "Next", "facts": ["Next sweep at 22:20Z."]}]}

_n = S.build(SWEEP, "factory", call=model(SWEEP_ANSWER))
_r = N.render(_n)
_lines = _r["text"].split("\n")
check("sweep: the headline says what happened, its icon from the status",
      _lines[0] == "✅ Goal sweep: no new call failures", _r["text"])
check("sweep: three bullets, each a whole fact, never 'and N more'",
      [ln for ln in _lines if ln.startswith("• ")] == [
          "• Replies on mini are a little slower; a task already covers it.",
          "• *v0.812.0* is still green on the canary and goes out at 21:00Z.",
          "• The next sweep is at 22:20Z."], _r["text"])
check("sweep: no ids, no jargon in the top message", top_ok(_r["text"]), _r["text"])
check("sweep: Details are sectioned, one • per fact",
      all(f"*{t}*" in _r["details"] for t in ("Calls", "Known issue", "Release"))
      and "\n• Checked 14 calls; nothing new to file." in _r["details"], _r["details"])
check("sweep: the ids and numbers the note left out are in Details",
      all(t in _r["details"] for t in ("t_9f3a21bc", "p50", "2.1 s", "5/5", "G1-G5")), _r["details"])
check("sweep: Details read as Slack text (an arrow, not '-&gt;')", "→" in _r["details"] and "-&gt;" not in _r["details"],
      _r["details"])

# The model said what the note means but lost a fact from the thread: the agent's own words stay.
_lossy = {**SWEEP_ANSWER, "sections": [{"title": "Calls", "facts": ["No new call failures."]}]}
_n = S.build(SWEEP, "factory", call=model(_lossy))
_r = N.render(_n)
check("a section that lost an id or a number: Details fall back to the agent's words, whole",
      "t_9f3a21bc" in _r["details"] and "5/5" in _r["details"] and "*Calls*" not in _r["details"], _r["details"])
check("…and the top message is still the model's plain one", _r["text"].startswith("✅ Goal sweep: no new call failures")
      and top_ok(_r["text"]), _r["text"])

# A model that words the headline as what was read, in the agent's jargon: the note never carries it.
_jargon = {**SWEEP_ANSWER, "headline": "Read CALL-REVIEWs since 16:20Z", "points": ["p50 is 2.1 s (G1-G5 unchanged)."]}
_r = N.render(S.build(SWEEP, "factory", call=model(_jargon)))
check("a jargon headline and point from the model never reach the top message",
      _r["text"].split("\n")[0].strip() and top_ok(_r["text"]) and "CALL-REVIEW" not in _r["text"], _r["text"])

# A real ask: the budget note. Its last section is what the owner must do.
BUDGET = """❓ Needs an answer on t_d1d740fc: Retro: wire review noise tripwire into reviewer briefs (t_d1d740fc)
Operator (budget): the task hit its budget again after one operator grant. Burn so far: 274/1,200 turns, review rounds 5/5.
Grant more or stop it? https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc"""
_ask_url = "https://factory.staffai.workers.dev/#factory/ask/t_d1d740fc"
BUDGET_ANSWER = {
    "status": "ask", "headline": "The review-noise retro ran out of budget again",
    "points": ["It already had one extra grant.", "All five review rounds are used."],
    "ask": "Give it more budget, or stop it?", "links": [{"url": _ask_url, "label": "Answer here"}],
    "sections": [
        {"title": "Needs you", "facts": [f"Grant more or stop it? {_ask_url}"]},
        {"title": "Budget", "facts": ["Task t_d1d740fc: Retro: wire review noise tripwire into reviewer briefs.",
                                      "Hit its budget again after one operator grant.",
                                      "Burn so far: 274/1,200 turns, review rounds 5/5."]}]}
_n = S.build(BUDGET, "factory", call=model(BUDGET_ANSWER))
_r = N.render(_n)
check("budget ask: a plain headline with the ask's icon, two bullets, the question",
      _r["text"].startswith("❓ The review-noise retro ran out of budget again\n• It already had one extra grant.\n"
                            "• All five review rounds are used.\n") and "Give it more budget, or stop it?" in _r["text"],
      _r["text"])
check("budget ask: no ids in the top message", top_ok(_r["text"]), _r["text"])
_d = _r["details"]
check("budget ask: *Needs you* is the last section of Details, wherever the model put it",
      "*Needs you*" in _d and "*Budget*" in _d and _d.index("*Needs you*") > _d.index("*Budget*"), _d)

# A done note with no decision: no *Needs you* section.
DONE_3558 = ("Done on mini: Review #3558 at 0b5bdc6 (agentvm). The train drop for #3558 was a CI infrastructure flake "
             "(apt could not fetch `age`), not a code fault; the PR's own CI is green on its head. I labelled it "
             "release-fix, recorded the delta review of the Google-host fix, and boarded it on the ci-release train "
             "again. It now waits for the train to merge it. (t_ac7d0418)")
DONE_ANSWER = {
    "status": "ok", "headline": "The Google-host fix is back on the release train",
    "points": ["Its train drop was a CI hiccup, not the code.", "Its own CI is green.", "The train merges it next."],
    "ask": "", "links": [],
    "sections": [
        {"title": "What happened", "facts": ["Review #3558 at 0b5bdc6 (agentvm), on mini.",
                                             "The train drop for #3558 was a CI infrastructure flake (apt could not "
                                             "fetch `age`), not a code fault.", "The PR's own CI is green on its head."]},
        {"title": "Done", "facts": ["Labelled release-fix; recorded the delta review; boarded on the ci-release train.",
                                    "Waits for the train to merge it. (t_ac7d0418)"]}]}
_r = N.render(S.build(DONE_3558, "factory", call=model(DONE_ANSWER)))
check("done note: plain headline, three bullets, no PR number, sha or task id on top",
      _r["text"].startswith("✅ The Google-host fix is back on the release train\n") and top_ok(_r["text"])
      and "3558" not in _r["text"], _r["text"])
check("done note: Details keep the sha, the PR and the task id, and no *Needs you*",
      all(t in _r["details"] for t in ("0b5bdc6", "#3558", "t_ac7d0418")) and "Needs you" not in _r["details"], _r["details"])

# A watch note: the box's memory, with a list the owner may want.
KEY_HEALTH = ("Box memory high: 53% of 63 GB used, swap 76%. Nothing was stopped.\n"
              "- Biggest: qemu-system-x86 18.2 GB, qemu-system-x86 9.6 GB, python3 2.1 GB, node 1.4 GB, claude 0.9 GB\n"
              "- VMs running (3): agentvm, hillsboro, helsinki")
KH_ANSWER = {
    "status": "warn", "headline": "The box is short on memory",
    "points": ["Half of its 63 GB is used and swap is at 76%.", "The two VMs use most of it.", "Nothing was stopped."],
    "ask": "", "links": [],
    "sections": [{"title": "Memory", "facts": ["53% of 63 GB used, swap 76%. Nothing was stopped."]},
                 {"title": "Biggest", "facts": ["qemu-system-x86 18.2 GB", "qemu-system-x86 9.6 GB", "python3 2.1 GB",
                                                "node 1.4 GB", "claude 0.9 GB"]},
                 {"title": "VMs running", "facts": ["3: agentvm, hillsboro, helsinki"]}]}
_r = N.render(S.build(KEY_HEALTH, "key-health", call=model(KH_ANSWER)))
check("watch note: a warn headline and three whole bullets", _r["text"].startswith(
    "⚠️ The box is short on memory\n• Half of its 63 GB is used and swap is at 76%.\n• The two VMs use most of it.\n"
    "• *Nothing* was stopped.\n") and top_ok(_r["text"]), _r["text"])
check("watch note: Details in sections, one • per process", "*Biggest*" in _r["details"]
      and "\n• qemu-system-x86 9.6 GB\n" in _r["details"], _r["details"])

# parse: what a model may not say.
check("parse: an unknown status is no answer", S.parse(json.dumps({**SWEEP_ANSWER, "status": "great"}), SWEEP) is None)
check("parse: no headline is no answer", S.parse(json.dumps({**SWEEP_ANSWER, "headline": ""}), SWEEP) is None)
check("parse: a link the agent never wrote is dropped",
      S.parse(json.dumps({**BUDGET_ANSWER, "links": [{"url": "https://evil.example/x", "label": "x"}]}), BUDGET)["links"] == [])
check("parse: points that are not text are dropped, at most three kept",
      S.parse(json.dumps({**SWEEP_ANSWER, "points": [1, None, "a", "b", "c", "d"]}), SWEEP)["points"] == ["a", "b", "c"])
check("parse: sections that are not a list are no Details", S.parse(json.dumps({**SWEEP_ANSWER, "sections": "x"}),
                                                                    SWEEP)["details"] == "")
check("parse: an answer from before points and sections still reads",
      (lambda d: d and d["points"] == [] and d["details"] == "" and d["summary"] == "s")(
          S.parse(json.dumps({"status": "ok", "headline": "h", "summary": "s"}), "x")))
check("the prompt keeps the owner's words: plain headline, three facts, no jargon, Needs you last",
      all(w in S.PROMPT for w in ('"points"', '"sections"', "Needs you", "CALL-REVIEW", "never what was read")))

print(f"\n{passed} checks passed")
