"""Inbound messages the writer could not handle for want of a Claude account, kept and retried.

On 19 Nov at 19:39Z Matthew's DM ("call running over, testing in 10 minutes") came back 429 on
every account the pool would lend, and Solo posted "Couldn't handle a message from Matthew" with
the raw API error. The message was then dropped: a failed item is only retried if Slack redelivers
it, and Slack does not redeliver an event it was acknowledged for.

The owner's rule: a message that cannot be handled is kept and retried with backoff, across
restarts, and the person hears a plain holding reply only if it takes long. The owner sees one
alert that the accounts are out (the writer's edge-triggered "Solo cannot draft"), not one per
message.

This module is the policy only: which failures are kept, when the next try is due, when a holding
reply is owed and what it says. The item on disk (status `retry_wait`, `item["retry"]`) is the
queue, so a restart loses nothing; the daemon runs the retries and does the sending.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

STATUS = "retry_wait"

# Failure classes that never reached an account with room: nothing was decided or sent, so the
# same message can be run again as if new. Only a rate limit for now: a login or plugin fault
# needs a person, and its alert already says so.
RETRYABLE = frozenset({"rate_limited"})

HOLDING_TEXT = "Seen this, will get back to you shortly."


def _env_min(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name) or default))
    except ValueError:
        return default


def first_delay_s() -> float:
    return _env_min("INBOUND_RETRY_FIRST_MIN", 1) * 60


def max_delay_s() -> float:
    return _env_min("INBOUND_RETRY_MAX_MIN", 30) * 60


def give_up_s() -> float:
    """How long a message is kept before it fails the old way (alert to the owner, no more tries)."""
    return _env_min("INBOUND_RETRY_GIVE_UP_HOURS", 24) * 3600


def hold_after_s() -> float:
    """How long a person waits before the holding reply. 0 turns the holding reply off."""
    return _env_min("INBOUND_HOLD_AFTER_MIN", 20) * 60


def backoff_s(attempts: int) -> float:
    """The wait after the `attempts`-th failure: 1m, 2m, 4m … capped at INBOUND_RETRY_MAX_MIN."""
    return min(first_delay_s() * (2 ** max(0, attempts - 1)), max_delay_s())


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _at(s: str | None, default: datetime) -> datetime:
    """A stored time; a missing or mangled one reads as `default` rather than stalling the queue."""
    try:
        return datetime.fromisoformat(s) if s else default
    except (TypeError, ValueError):
        return default


def schedule(item: dict, cls: str | None, why: str, now: datetime | None = None) -> bool:
    """Keep `item` for another try. False when it cannot be kept: not a retryable class, or kept
    past INBOUND_RETRY_GIVE_UP_HOURS. On True the item is `retry_wait` with its next try due."""
    if cls not in RETRYABLE:
        return False
    now = now or datetime.now()
    r = dict(item.get("retry") or {})
    first = _at(r.get("first_at"), now)
    if (now - first).total_seconds() >= give_up_s():
        return False
    r["attempts"] = int(r.get("attempts") or 0) + 1
    r.update(first_at=_ts(first), last_at=_ts(now), cls=cls, why=(why or "")[:300],
             next_at=_ts(now + timedelta(seconds=backoff_s(r["attempts"]))))
    item["retry"] = r
    item["status"] = STATUS
    item["outcome"] = {"status": STATUS, "why": r["why"], "class": cls}
    return True


def due(items: list[dict], now: datetime | None = None) -> list[dict]:
    """The kept items whose next try has come, oldest message first."""
    now = now or datetime.now()
    out = [it for it in items if it.get("status") == STATUS
           and _at((it.get("retry") or {}).get("next_at"), now) <= now]
    return sorted(out, key=lambda it: (it.get("retry") or {}).get("first_at") or "")


def holding_owed(item: dict, now: datetime | None = None) -> bool:
    """A kept message whose sender has waited INBOUND_HOLD_AFTER_MIN and has not been told yet."""
    r = item.get("retry") or {}
    after = hold_after_s()
    if item.get("status") != STATUS or not after or r.get("held") or not r.get("first_at"):
        return False
    now = now or datetime.now()
    return (now - _at(r["first_at"], now)).total_seconds() >= after


def mark_held(item: dict, ts: str, now: datetime | None = None) -> None:
    item.setdefault("retry", {})["held"] = {"ts": ts, "at": _ts(now or datetime.now())}


def alert_tail(cls: str | None) -> str | None:
    """What the owner's "Solo cannot draft" alert says happens to new messages, for a kept class."""
    if cls not in RETRYABLE:
        return None
    hold = (f" anyone waiting more than {hold_after_s() / 60:.0f} min gets a short holding reply."
            if hold_after_s() else "")
    return (f"_new messages are kept and retried as accounts free up;{hold} One still waiting after "
            f"{give_up_s() / 3600:.0f} h fails with its own alert._")
