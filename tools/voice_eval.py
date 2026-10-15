#!/usr/bin/env python3
"""Does Solo's draft read like the owner's actual reply?

Held-out by construction: pairs come from live Slack history *after* the date the
voice corpus was built from, so the samples in VOICE.md cannot contain them.

    python3 tools/voice_eval.py pairs --since 2026-10-11
    python3 tools/voice_eval.py run --n 6

`run` drafts through the real writer with simulated=True, so nothing reaches Slack.
Scoring has two legs, deliberately: mechanical stylometrics against the owner's
own measured habits, and the three chase-rules that only apply to some messages.
A judge is not asked "does this sound like you" — it cannot see its own style.

Known limitation — the mechanical legs are sound, the substance is not. The writer can call
`slackctl history`, and these inbounds are real messages from real threads, so at draft time it
can read the reply the owner actually sent and everything that came after it. "Held out" is true
of the voice corpus, not of the writer's reach. Trust the style scores; do not read the drafts as
evidence the substance was reached unaided. Fixing it means passing the pre-`ts` thread as
item["context"] and telling the writer not to call `history`.
"""
import argparse, json, os, re, sys, time, uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import slackctl as S  # noqa: E402
import config, voice  # noqa: E402

PAIRS = config.STATE_DIR / "curriculum" / "heldout.jsonl"
RUNS = config.STATE_DIR / "curriculum" / "runs.jsonl"
GREETING = re.compile(r"^\s*(hi|hey|hello|dear|good (morning|afternoon|evening))\b", re.I)
PADDING = re.compile(r"^\s*(thanks for|thank you for|great question|hope you|happy to|i'd be happy)", re.I)
# A date Solo invented rather than one the owner gave it.
INVENTED_TIME = re.compile(r"\b(by|back by|before|at)\s+(\d{1,2}\s*(am|pm|:\d\d)|noon|eod|tomorrow|morrow|monday|tuesday|wednesday|thursday|friday)\b", re.I)


def build_pairs(since: str, limit: int) -> list[dict]:
    """A pair is (what they said) → (what the owner actually replied)."""
    s = S.Slack()
    cutoff = datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp()
    out, targets = [], [("@srivatsan", "im"), ("#adair-homes", "channel"), ("#pop-priorities", "channel")]
    for ref, ctype in targets:
        try:
            ch = (s.resolve_users([ref[1:]]) if ref.startswith("@") else s.resolve_channels([ref[1:]]))
            cid = s.client.conversations_open(users=list(ch)[0])["channel"]["id"] if ref.startswith("@") else list(ch)[0]
            msgs = s.client.conversations_history(channel=cid, limit=limit)["messages"][::-1]
        except Exception as e:  # a channel we are not in, a bad handle
            print(f"  skipped {ref}: {str(e)[:60]}", file=sys.stderr); continue
        for i, m in enumerate(msgs):
            if m.get("user") == s.me or float(m.get("ts", 0)) < cutoff or not m.get("text"):
                continue
            # the owner's reply = his consecutive messages immediately after theirs
            mine = []
            for nxt in msgs[i + 1:]:
                if nxt.get("user") != s.me:
                    break
                if nxt.get("text"):
                    mine.append(nxt["text"])
            if not mine:
                continue
            out.append({"inbound": m["text"], "sender": s.names.get(m.get("user", ""), "Sri"),
                        "channel_type": ctype, "channel_name": None if ctype == "im" else ref,
                        "actual": "\n".join(mine), "ts": m["ts"],
                        "when": datetime.fromtimestamp(float(m["ts"])).strftime("%b %d %H:%M")})
    out.sort(key=lambda p: p["ts"])
    PAIRS.parent.mkdir(parents=True, exist_ok=True)
    PAIRS.write_text("".join(json.dumps(p) + "\n" for p in out))
    return out


def draft_for(pair: dict) -> dict | None:
    """Run the real writer on a simulated item. Nothing can send: simulated=True."""
    item = {"id": f"eval-{uuid.uuid4().hex[:8]}", "created": S.now_iso(), "status": "queued", "simulated": True,
            "channel": "EVAL", "channel_type": pair["channel_type"], "channel_name": pair["channel_name"],
            "ts": f"{time.time():.6f}", "thread_ts": None, "reply_thread_ts": None,
            "sender": "UEVAL", "sender_name": pair["sender"],
            "sender_handle": pair["sender"].lower().replace(" ", ""),
            "text": pair["inbound"], "permalink": None,
            "context": f"[{pair['sender']}]: {pair['inbound']}"}
    S.save_item(item)
    S.run_claude(item, S.State())
    fresh = json.loads(S.item_path(item["id"]).read_text())
    return fresh.get("decision")


def score(draft: str, pair: dict) -> dict:
    """Mechanical legs first — these are measured habits, not taste."""
    d, actual = (draft or "").strip(), pair["actual"].strip()
    words, aw = len(d.split()), max(1, len(actual.split()))
    flags = {}
    flags["no greeting"] = not GREETING.search(d)
    flags["no padding opener"] = not PADDING.search(d)
    flags["no trailing full stop"] = not d.endswith(".")
    flags["not bloated"] = words <= max(12, aw * 3)
    flags["no invented time"] = not INVENTED_TIME.search(d)
    flags["no em-dash prose"] = "—" not in d
    # An empty draft scored "clean" on every regex above — it has no greeting and no full stop
    # because it has nothing. If they asked something, silence or a bare emoji is the failure
    # VOICE.md calls flooding the comms.
    asked = "?" in pair["inbound"] or re.search(r"\b(can you|could you|what|when|why|how|should we|do we)\b", pair["inbound"], re.I)
    flags["said something when asked"] = bool(d) or not asked
    return {"words": words, "actual_words": aw, "flags": flags,
            "clean": all(flags.values()), "failed": [k for k, v in flags.items() if not v]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pairs", help="build the held-out set from live history")
    p.add_argument("--since", default="2026-10-11"); p.add_argument("--limit", type=int, default=60)
    p = sub.add_parser("rescore", help="re-score drafts already produced, without paying to draft again")
    p.add_argument("--bar", type=float, default=0.8)
    p = sub.add_parser("run", help="draft each pair and score it")
    p.add_argument("--n", type=int, default=6); p.add_argument("--bar", type=float, default=0.8)
    a = ap.parse_args()

    if a.cmd == "pairs":
        pairs = build_pairs(a.since, a.limit)
        print(f"{len(pairs)} held-out pairs since {a.since} → {PAIRS}")
        for q in pairs[-8:]:
            print(f"  {q['when']}  {q['sender']}: {q['inbound'][:52]!r} → {q['actual'][:42]!r}")
        return

    if a.cmd == "rescore":
        if not RUNS.exists():
            sys.exit("no saved drafts yet — run: voice_eval.py run")
        rows = [json.loads(l) for l in RUNS.read_text().splitlines() if l.strip()]
        report([(r["pair"], r["route"], r["text"], score(r["text"], r["pair"])) for r in rows], a.bar)
        return

    if not PAIRS.exists():
        sys.exit("no held-out set yet — run: voice_eval.py pairs")
    pairs = [json.loads(l) for l in PAIRS.read_text().splitlines() if l.strip()][-a.n:]
    print(f"voice check · {len(pairs)} held-out pairs · VOICE.md {len(voice.load_notes())} chars\n")
    results = []
    for i, q in enumerate(pairs, 1):
        dec = draft_for(q)
        route = (dec or {}).get("action", "none")
        text = (dec or {}).get("text") or ""
        sc = score(text, q)
        results.append((q, route, text, sc))
        with RUNS.open("a") as fh:
            fh.write(json.dumps({"pair": q, "route": route, "text": text}) + "\n")
        print(f"[{i}] {q['when']} {q['sender']}: {q['inbound'][:66]}")
        print(f"    they wrote: {q['actual'][:100]!r} ({sc['actual_words']}w)")
        print(f"    solo     : {text[:100]!r} ({sc['words']}w, route={route})")
        print(f"    {'clean' if sc['clean'] else 'FAILS: ' + ', '.join(sc['failed'])}\n")

    report(results, a.bar)


def report(results: list, bar: float) -> None:
    for i, (q, route, text, sc) in enumerate(results, 1):
        if not sc["clean"]:
            print(f"[{i}] {q['when']} {q['sender']}: {q['inbound'][:60]}")
            print(f"    they wrote: {q['actual'][:90]!r}")
            print(f"    solo     : {text[:90]!r} (route={route})")
            print(f"    FAILS: {', '.join(sc['failed'])}\n")
    clean = sum(1 for *_, sc in results if sc["clean"])
    rate = clean / max(1, len(results))
    print(f"mechanical: {clean}/{len(results)} clean ({rate:.0%})  bar={bar:.0%}  "
          f"{'PASS' if rate >= bar else 'BELOW BAR'}")
    worst: dict[str, int] = {}
    for *_, sc in results:
        for f in sc["failed"]:
            worst[f] = worst.get(f, 0) + 1
    if worst:
        print("what to fix, most common first: " + ", ".join(f"{k} ({v})" for k, v in sorted(worst.items(), key=lambda x: -x[1])))
    print("\nMechanical only. Read the pairs above for substance — whether the draft answered what was\n"
          "asked is the half no regex scores.")


if __name__ == "__main__":
    main()
