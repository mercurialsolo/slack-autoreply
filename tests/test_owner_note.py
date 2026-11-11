#!/usr/bin/env python3
"""Golden examples: how a note to the owner reads (scripts/owner_note.py). No Slack, no tokens.

Owner, 2026-11-04: "messages are not well formatted here for readability … use icons, emojis and
language that is more human readable rather than just technical details." Each case is a note an
agent really sends, and the exact message the owner sees. Change a golden only on purpose.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
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
r = golden("a rollback: 🔴 headline with the box and versions in bold, the why and the next step as bullets",
           "Helsinki rolled back v0.552.0 -> stays on v0.551.0: reply_heard_p50_ms: 1225 vs median 824 of the last 5, "
           "+401 (band 44.5 from 20) on one smoke run. Next: the release lane re-runs tonight, see "
           "~/.factory/logs/release-552.log",
           "🔴 *Helsinki* rolled back *v0.552.0* → stays on *v0.551.0*\n"
           "• Reply time 1.2 s vs 0.8 s usually, +0.4 s on one test run\n"
           "• Next: the release lane re-runs tonight\n"
           "_from solo-one · details in the thread_")
check("…the numbers, the metric name and the log path are all still in the Details reply",
      all(x in r["details"] for x in ("1225", "reply_heard_p50_ms", "release-552.log")) and r["details"].startswith("🔎 *Details*"))

# 2. a green release
golden("a green release: ✅, short",
       "Helsinki shipped v0.552.0, reply_heard_p50_ms: 812 vs median 824 of the last 5. Canary done, all checks green",
       "✅ *Helsinki* shipped *v0.552.0*, reply time 0.8 s vs 0.8 s usually\n"
       "• Canary done, all checks green\n"
       "_from solo-one · details in the thread_")

# 3. a frozen box
golden("a frozen box: ⏸️, the ticket and the setting stay in the thread",
       "Hillsboro frozen at v0.537.0 (freeze=owner, set 2026-11-02, ticket ENG-881) until the owner lifts it; nothing ships there",
       "⏸️ *Hillsboro* frozen at *v0.537.0* until the owner lifts it\n"
       "• nothing ships there\n"
       "_from solo-one · details in the thread_")

# 4. an ask, with buttons
r = golden("an ask: ❓ first, the context as a bullet, a link to the answer page",
           "v0.553.0 is green on Helsinki. Ship it to Hillsboro now, or wait for the freeze to lift?",
           "❓ Ship it to Hillsboro now, or wait for the freeze to lift?\n"
           "• ✅ *v0.553.0* is green on Helsinki\n"
           "_from solo-one · details in the thread_ · <https://factory.example/ask/t_9|answer page>",
           options=["Ship now", "Wait"], link="https://factory.example/ask/t_9")
acts = [b for b in r["blocks"] if b["type"] == "actions"][0]["elements"]
check("…the choices are buttons, never 'reply yes/no'",
      [e["text"]["text"] for e in acts] == ["Ship now", "Wait", "Open the answer page"]
      and [e.get("value") for e in acts[:2]] == ["Ship now", "Wait"] and "reply" not in r["text"].lower())
check("…a Yes is the primary button", N.render(N.from_text("Merge #1739?", "x", options=["Yes", "No"]))["blocks"][1]["elements"][0].get("style") == "primary")
off = N.render(N.from_text("Merge #1739?", "x", options=["Yes", "No"]), buttons=False)
check("…with Interactivity still off: the choices in words, no blocks", off["blocks"] is None
      and "👉 *Yes* · *No* — answer in the thread" in off["text"], off["text"])
done = N.answered_blocks(r["blocks"], "Wait", "you", "22:41")
check("…once answered the buttons are gone and the choice is shown",
      not [b for b in done if b["type"] == "actions"] and "✔️ *Wait*" in done[-1]["elements"][0]["text"])

# 5. grouping
notes = [N.from_text("Helsinki shipped v0.552.0, all checks green", "solo-one"),
         N.from_text("Hillsboro frozen at v0.537.0 until you lift it", "solo-one"),
         N.from_text(TODAY, "solo-one")]
g = N.render_group(notes, "solo-one")
check("grouping: one message, the worst icon, one line per note", g ==
      "🔴 *3 updates* from solo-one\n"
      "• ✅ *Helsinki* shipped *v0.552.0*, all checks green\n"
      "• ⏸️ *Hillsboro* frozen at *v0.537.0* until you lift it\n"
      "• 🔴 *Helsinki* is not on the build it should be (a rollback, or a deploy by hand)\n"
      "_each one's detail is in the thread_", g)
check("…more than three becomes 'and N more'", "…and 2 more in the thread" in N.render_group(notes + notes[:1], "x"))

# today's message, the one the owner complained about
golden("today's release note, rewritten", TODAY,
       "🔴 *Helsinki* is not on the build it should be (a rollback, or a deploy by hand)\n"
       "• ⏸️ *Helsinki* held at *v0.551.0*: the regression check was red: reply time 1.2 s vs 0.8 s usually, +0.4 s\n"
       "• ⏸️ *Hillsboro* frozen at *v0.537.0*\n"
       "_from solo-one · details in the thread_")

# what the rules must never do
CMD = ("Needs on the agentvm VM: colima ssh -p agentvm -- sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0 "
       "(resets on VM reboot).")
check("a command the owner has to run is never rewritten", CMD in N.render(N.from_text(CMD, "x"))["text"])
check("code spans are left exactly as written", "`git push origin release_v2_ms: 1200`" in
      N.humanize("run `git push origin release_v2_ms: 1200` now"))
check("no shas, metric names or log paths in the body",
      not any(x in N.render(N.from_text(TODAY, "x"))["text"] for x in ("dc10473f18", "e150cfdd06", "_ms", "p50")))
check("a short plain note is just its headline, with no thread", N.render(N.from_text("P1 merged", "x")) ==
      {"text": "✅ P1 merged\n_from x_", "blocks": None, "details": ""})
check("at most three bullets", len(N.from_text(". ".join(f"Box{i} shipped v1.{i}" for i in range(8)), "x").bullets) <= 3)
check("an agent's explicit shape wins over the rules",
      N.render(N.from_text("raw dump 1225ms", "x", status="held", headline="Lisbon held at v0.9", points=["waiting on the gate"]))["text"]
      == "⏸️ *Lisbon* held at *v0.9*\n• waiting on the gate\n_from x · details in the thread_")
try:
    N.norm_status("purple")
    check("an unknown status is refused", False)
except ValueError:
    check("an unknown status is refused", True)
check("a count after a ms reading stays a count", N.humanize("reply_heard_p50_ms: 1200 vs 800 median of the last 200 runs, build 1234")
      == "reply time 1.2 s vs 0.8 s median of the last 200 runs, build 1234", N.humanize("reply_heard_p50_ms: 1200 vs 800 median of the last 200 runs, build 1234"))
check("…and 'across 1500 calls' too", N.humanize("first_token_ms: 950 across 1500 calls") == "time to first word 0.9 s across 1500 calls")
check("a bare URL is left whole", "https://ci.example.com/runs/run.log" in N.render(N.from_text("see https://ci.example.com/runs/run.log", "x"))["text"]
      and "https://github.com/x/y/releases/tag/v1.2.3" in N.render(N.from_text("released https://github.com/x/y/releases/tag/v1.2.3", "x"))["text"])
check("Slack's control characters are escaped", "latency &lt;2 s &amp; error &gt;1%" in N.render(N.from_text("latency <2 s & error >1%", "x"))["text"]
      and "<@U123>" in N.render(N.from_text("ping <@U123> about it", "x"))["text"])
_many = N.render(N.from_text("Pick a box", "x", options=list("ABCDEFG")))
check("more than five choices are all buttons", [e["text"]["text"] for e in _many["blocks"][1]["elements"]] == list("ABCDEFG"))
_long = N.render(N.from_text("x", "x", headline="Run `make " + "a" * 4000 + "`", options=["Yes", "No"]))
check("a section never passes Slack's 3000 characters", len(_long["blocks"][0]["text"]["text"]) <= 3000)
_pts = N.from_text("x", "x", headline="h", points=list("abcde"))
check("explicit points obey the three-bullet rule, the rest in Details", len(_pts.bullets) == 3 and all(f"• {c}" in _pts.details for c in "cde"))
check("ms on its own becomes seconds", N.humanize("took 1460 ms") == "took 1.5 s" and N.humanize("took 40 ms") == "took 40 ms")

print(f"\n{passed} checks passed")
