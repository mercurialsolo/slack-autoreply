#!/usr/bin/env python3
"""What the routing thresholds would do at other values, replayed on messages already scored.

Every gate in `triage.Policy` carries a default nobody chose against this traffic: execute 0.5,
answerable 0.5, scope 0.5, confidence 0.6. Hamel Husain and Antaripa Saha, "How To Use Decision
Models for Evals" (hamel.dev, 2026-10-20), make the case that the 0.5 you inherit is not the 0.5
you want, and that tuning it costs nothing because the scores are already saved. Ours are: 148 of
176 items on disk carry their full Jev scores, so this replays them and no model is called.

Three things it will not do, because they would be dishonest here:

  * No precision, recall or F1. That method needs labels and we have none. `overtaken` is the
    tempting stand-in and it is wrong — the owner answering an escalation is the escalation
    working, not failing.
  * No recommended value. `max_execute` is a boundary the owner set ("refuse writeable commands").
    This reports what moving it would catch and release; moving it is theirs.
  * No claim about old rows' routes. An item saved before 2026-10-20 records the route *after*
    forcing, so what the model had said is gone. Those rows can only answer "still forced at this
    threshold?". Full before/after replay covers rows carrying `model_route` and says how many.

  python3 tools/threshold_sweep.py                  # the execute gate, bins and sweep
  python3 tools/threshold_sweep.py --gate answerable
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import config  # noqa: E402
import triage as T  # noqa: E402

# gate -> (the Triage field it reads, the Policy field that gates it, the config key that sets it
# on this machine, which way the test runs). The config key, not the dataclass default: the
# default is what shipped, and marking that as "in use" tells you the opposite of the truth on
# any node where the owner has tuned it.
GATES = {
    "execute":    ("asks_to_execute", "max_execute", "triage_max_execute", "at or above forces escalate"),
    "answerable": ("can_answer", "min_answerable", "triage_min_answerable", "below demotes a reply to a hold"),
    "scope":      ("in_scope", "min_in_scope", "triage_min_in_scope", "below forces escalate"),
    "confidence": ("confidence", "min_confidence", "triage_min_confidence", "below demotes reply→hold, ignore→ack"),
}
SHORT = 30          # a message at or under this many characters, where the score drifts to the thread
STEPS = [round(0.30 + 0.05 * i, 2) for i in range(13)]


def load() -> list[dict]:
    """Every scored item on disk, with its ledger outcome where there is one."""
    outcomes = {}
    path = config.p("outcomes.jsonl")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            outcomes[row.get("item")] = row.get("outcome")
    out = []
    for f in sorted(config.INBOX_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        t = d.get("triage") or {}
        if d.get("simulated") or t.get("asks_to_execute") is None:
            continue
        d["_t"], d["_outcome"] = t, outcomes.get(d.get("id"))
        out.append(d)
    return out


def replay(d: dict, gate: str, value: float) -> str:
    """The route this message would have taken with that one gate moved."""
    t = d["_t"]
    kw = {k: t[k] for k in ("route", "confidence", "sensitivity", "urgency", "needs_context",
                            "asks_for_content", "source") if k in t}
    kw |= {k: t[k] for k in ("topic", "can_answer", "asks_to_execute", "in_scope") if k in t}
    # The saved route is what forcing produced. Start from the model's own route where it was
    # recorded, and clear `forced` so the replayed summary is not carrying the old reason.
    kw["route"] = t.get("model_route") or t["route"]
    kw["forced"] = None
    # The execute gate exempts a short message the model chose to stay quiet on, so a replay that
    # does not carry the length models a policy that is not running.
    kw["chars"] = t.get("chars") or len(" ".join((d.get("text") or "").split()))
    return T.Policy(**{GATES[gate][1]: value}).apply(T.Triage(**kw)).route


def bins(rows: list[dict], field: str) -> None:
    """The scores as they actually fall, which is the check to run before tuning anything."""
    print(f"\n=== how {field} actually scores ({len(rows)} messages) ===")
    buckets: dict[int, list] = defaultdict(list)
    for d in rows:
        buckets[min(9, int(float(d["_t"][field]) * 10))].append(d)
    for b in sorted(buckets, reverse=True):
        got = sorted(buckets[b], key=lambda d: -float(d["_t"][field]))
        print(f"  {b / 10:.1f}-{(b + 1) / 10:.1f}  n={len(got):<3}")
        for d in got[:3]:
            text = " ".join((d.get("text") or "").split())[:62]
            print(f"      {float(d['_t'][field]):.2f} {str(d['_t'].get('topic'))[:14]:<14} {text!r}")


def short_message_check(rows: list[dict], field: str) -> None:
    """Does the score describe the message, or the thread it arrived in?

    Decided on this traffic: a message of a couple of words scores half again as high as a long
    one. "ty" came in at 0.86, "i dont think so" at 0.92, "421452" at 0.89. None of them asks for
    anything. No threshold separates those from "please fix login" at 0.97, so a sweep is the
    wrong instrument for them — they need a second signal, not a different number.
    """
    s = [float(d["_t"][field]) for d in rows if len(" ".join((d.get("text") or "").split())) <= SHORT]
    l = [float(d["_t"][field]) for d in rows if len(" ".join((d.get("text") or "").split())) > SHORT]
    if not s or not l:
        return
    print(f"\n=== is {field} describing the message or the thread? ===")
    print(f"  {SHORT} characters or fewer : n={len(s):<4} median {statistics.median(s):.2f}")
    print(f"  longer                  : n={len(l):<4} median {statistics.median(l):.2f}")
    if statistics.median(s) > statistics.median(l) + 0.1:
        print("  → short messages score higher than long ones. The score is reading the thread, not")
        print("    the words, and no threshold tells 'ty' from 'please fix login'. Tune the gate for")
        print("    substantive messages; the short ones need a different signal.")


def sweep(rows: list[dict], gate: str) -> None:
    field, _, cfg_key, direction = GATES[gate]
    live = float(config.CFG[cfg_key])
    forced_now = [d for d in rows if (d["_t"].get("forced") or "")]
    replayable = [d for d in rows if d["_t"].get("model_route")]
    print(f"\n=== moving the {gate} gate ({direction}) ===")
    print(f"  {len(rows)} scored messages · {len(replayable)} carry the model's own route and can be fully replayed")
    if len(replayable) < len(rows):
        print(f"  the other {len(rows) - len(replayable)} were saved before 2026-10-20 and record only the route")
        print("  after forcing, so for those this reports gate membership, not the route they would take")
    prev: set[str] = set()
    for value in STEPS:
        caught = {d["id"] for d in rows if (float(d["_t"][field]) >= value if gate in ("execute",)
                                            else float(d["_t"][field]) < value)}
        routes = defaultdict(int)
        for d in replayable:
            routes[replay(d, gate, value)] += 1
        mark = "  ← in use" if abs(value - live) < 1e-9 else ""
        extra = ("  replayed: " + ", ".join(f"{k} {v}" for k, v in sorted(routes.items()))) if routes else ""
        print(f"  {value:.2f}  catches {len(caught):>3}/{len(rows)}{mark}{extra}")
        if prev and (released := prev - caught):
            for d in sorted((d for d in rows if d["id"] in released), key=lambda d: -float(d["_t"][field])):
                text = " ".join((d.get("text") or "").split())[:60]
                out = f" [{d['_outcome']}]" if d.get("_outcome") else ""
                print(f"        released {float(d['_t'][field]):.2f} {str(d['_t'].get('topic'))[:14]:<14} {text!r}{out}")
        prev = caught
    print("\n  'released' means the gate stops firing on it. Whether that message should still have")
    print("  reached you is the label this has no way to know — read them and say.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gate", choices=sorted(GATES), default="execute")
    ap.add_argument("--bins", action="store_true", help="only the score distribution")
    a = ap.parse_args()
    rows = load()
    if not rows:
        sys.exit(f"no scored items under {config.INBOX_DIR}")
    field = GATES[a.gate][0]
    bins(rows, field)
    short_message_check(rows, field)
    if not a.bins:
        sweep(rows, a.gate)


if __name__ == "__main__":
    main()
