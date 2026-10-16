"""The learning loop: count what went right and wrong, earn the right to act alone.

Signals (recorded by the daemon as they happen):
  good   approved      you ✅'d a draft unchanged
         clean         auto-sent and nothing bad happened for OBSERVE_HOURS
  bad    dropped       you ❌'d a draft
         replaced      a draft with real content was pending and you wrote something else
         corrected     you edited a message it had sent as you
         deleted       you deleted a message it had sent as you
         missed        it chose "ignore" and you replied to that message yourself within 2h
  neutral overtaken    a bare holding draft was pending and you simply answered first
          expired      a draft sat for DRAFT_EXPIRE_HOURS
          followed_up  you sent another message in that conversation soon after an auto-reply

Every bad signal that has a before/after is also stored as a correction pair,
which is what the skill's `review` mode reads to update VOICE.md.

Trust (MODE=learn): a (route, sender) pair starts in draft and graduates to auto
once it has TRUST_PROMOTE_N outcomes with a smoothed success rate ≥ TRUST_PROMOTE_SCORE
and fewer than TRUST_DEMOTE_BAD_IN_LAST5 bad outcomes among the last five. A route
can also graduate for everyone once it has TRUST_PROMOTE_N_GLOBAL outcomes at
≥ TRUST_PROMOTE_SCORE_GLOBAL. Escalations never auto-send. Acks auto-send too, except in MODE=draft, where a
reaction waits like everything else.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

GOOD = {"approved", "clean"}
BAD = {"dropped", "replaced", "corrected", "deleted", "missed"}

# VOICE.md's autopilot acks plus the bare agreements. A draft that is *only* one of these is a
# holding line: it says "I am looking at this" and commits to nothing. If the owner then answers
# the person themselves, the hold was not wrong — they were simply there first, which is the
# normal case for a present owner. Scoring that as a failure punishes Solo for the owner being
# responsive, and means no route can ever graduate while the owner is active. So it is neutral.
#
# "Checking - will update by 11am" deliberately does NOT match: the whole string must be the
# hold. A draft that added a time, or any substance, and was then replaced really was replaced.
BARE_HOLD = re.compile(
    r"^\W*(?:"
    r"(?:yeah|yes|ok|okay|sure|noted)"                      # a bare agreement, nothing more
    r"|(?:(?:yeah|yes|ok)\W+)?(?:(?:am|i'?m)\s+)?"          # optional "Yeah " / "Am " lead-in
    r"(?:checking|looking|on it|will check|will do|will revert|working on it)"
    r")\W*$",
    re.I,
)


def is_bare_hold(text: str | None) -> bool:
    """Was this draft nothing but a holding line?"""
    return bool(BARE_HOLD.match((text or "").strip()))


@dataclass
class Stats:
    n: int = 0
    good: int = 0
    bad: int = 0
    recent_bad: int = 0   # among the last 5 scored outcomes

    @property
    def score(self) -> float:
        return (self.good + 1) / (self.n + 2)   # Laplace-smoothed

    def __str__(self) -> str:
        return f"{self.good}/{self.n} good (score {self.score:.2f}, {self.recent_bad} bad in last 5)"


class Ledger:
    def __init__(self, outcomes_path: Path, corrections_path: Path, cfg: dict):
        self.outcomes_path = outcomes_path
        self.corrections_path = corrections_path
        self.cfg = cfg
        self.rows: list[dict] = []
        self._load()

    def _load(self) -> None:
        self.rows = []
        if self.outcomes_path.exists():
            for line in self.outcomes_path.read_text(encoding="utf-8").splitlines():
                try:
                    self.rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # --------------------------------------------------------- record

    def record(self, *, item: dict, outcome: str, correction: dict | None = None, note: str = "") -> dict:
        good = True if outcome in GOOD else False if outcome in BAD else None
        dec = item.get("decision") or {}
        row = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "item": item.get("id"),
            "route": dec.get("action") or (item.get("triage") or {}).get("route"),
            "sender": item.get("sender"),
            "sender_name": item.get("sender_name"),
            "channel": item.get("channel"),
            "channel_type": item.get("channel_type"),
            "effective_mode": (item.get("policy") or {}).get("effective"),
            "used_context": bool(dec.get("used_context")),
            "outcome": outcome,
            "good": good,
            "note": note,
        }
        self.outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.outcomes_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.rows.append(row)
        if correction:
            with open(self.corrections_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({**row, "trigger": item.get("text"), **correction}, ensure_ascii=False) + "\n")
        return row

    # ---------------------------------------------------------- stats

    def stats(self, route: str | None = None, sender: str | None = None, channel: str | None = None,
              used_context: bool | None = None) -> Stats:
        s = Stats()
        scored = []
        for r in self.rows:
            if r.get("good") is None:
                continue
            if route and r.get("route") != route:
                continue
            if sender and r.get("sender") != sender:
                continue
            if channel and r.get("channel") != channel:
                continue
            if used_context is not None and bool(r.get("used_context")) != used_context:
                continue
            scored.append(r)
        s.n = len(scored)
        s.good = sum(1 for r in scored if r["good"])
        s.bad = s.n - s.good
        s.recent_bad = sum(1 for r in scored[-5:] if not r["good"])
        return s

    def effective_mode(self, route: str, sender: str | None, channel: str | None, base_mode: str) -> tuple[str, str]:
        """(auto|draft, reason). base_mode is MODE from .env: learn | draft | auto."""
        if route == "escalate":
            return "draft", "escalations never auto-send"
        if route == "ignore":
            return "auto", "nothing is sent"
        if route == "ack":
            # A reaction is visible in the other person's Slack, so it is not free. In MODE=draft
            # it waits like everything else — "approve everything" has to mean everything, or the
            # one unapproved thing is the one that goes wrong.
            return ("draft", "MODE=draft — even a reaction waits") if base_mode == "draft" else ("auto", "harmless")
        if base_mode in ("auto", "draft"):
            return base_mode, f"MODE={base_mode}"
        c = self.cfg
        s = self.stats(route=route, sender=sender)
        if s.n >= c["promote_n"] and s.score >= c["promote_score"] and s.recent_bad < c["demote_bad_in_last5"]:
            return "auto", f"earned with this sender: {s}"
        g = self.stats(route=route)
        if g.n >= c["promote_n_global"] and g.score >= c["promote_score_global"] and g.recent_bad < c["demote_bad_in_last5"]:
            return "auto", f"earned overall for {route}: {g}"
        return "draft", f"still learning {route} with this sender: {s}"

    def share_ok(self, sender: str | None) -> bool:
        """Learned permission to send context-backed replies to this person without review."""
        s = self.stats(sender=sender, used_context=True)
        return s.n >= self.cfg["promote_n"] and s.score >= 0.9 and s.recent_bad == 0

    # ------------------------------------------------------- reporting

    def scorecard(self, days: int = 14) -> str:
        since = datetime.now() - timedelta(days=days)
        rows = [r for r in self.rows if datetime.fromisoformat(r["at"]) >= since]
        if not rows:
            return f"no outcomes in the last {days} days"
        out = [f"Last {days} days: {len(rows)} outcomes"]
        by_route = Counter(r.get("route") for r in rows)
        for route, n in by_route.most_common():
            scored = [r for r in rows if r.get("route") == route and r.get("good") is not None]
            good = sum(1 for r in scored if r["good"])
            oc = Counter(r["outcome"] for r in rows if r.get("route") == route)
            detail = ", ".join(f"{k} {v}" for k, v in oc.most_common())
            all_time = self.stats(route=route)
            out.append(f"  {route:<9} {n:>3}  {good}/{len(scored)} good  [{detail}]  all-time {all_time}")
        auto = [r for r in rows if r.get("effective_mode") == "auto"]
        drafted = [r for r in rows if r.get("effective_mode") == "draft"]
        out.append(f"  sent on its own: {len(auto)}   went through you: {len(drafted)}")
        senders = Counter((r.get("sender"), r.get("sender_name")) for r in rows if r.get("route") in ("reply", "hold"))
        if senders:
            out.append("By sender (reply/hold):")
            for (uid, name), n in senders.most_common(10):
                s = self.stats(sender=uid)
                mode, why = self.effective_mode("reply", uid, None, self.cfg["mode"])
                out.append(f"  {name or uid:<22} {s}  → {mode}")
        return "\n".join(out)

    def corrections(self, limit: int = 15) -> list[dict]:
        rows = []
        if self.corrections_path.exists():
            for line in self.corrections_path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-limit:]

    def review_text(self, days: int = 14) -> str:
        out = [self.scorecard(days), "", "Recent corrections (what it wrote → what you actually did):"]
        cs = self.corrections()
        if not cs:
            out.append("  none yet")
        for c in cs:
            out.append(f"- [{c['outcome']}] to {c.get('sender_name') or c.get('sender')} · route {c.get('route')}")
            if c.get("trigger"):
                out.append(f"    them:  {c['trigger'][:200]}")
            if c.get("draft"):
                out.append(f"    bot:   {c['draft'][:300]}")
            if c.get("actual"):
                out.append(f"    you:   {c['actual'][:300]}")
        return "\n".join(out)
