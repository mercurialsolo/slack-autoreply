"""Time-bound holds: "I'll get back to you by X" is a promise, so track it.

hold_deadline() turns Jev's urgency score into a concrete deadline and the
phrase the owner would use for it. Followups persists each promise, reminds the
owner in their self-DM shortly before it's due, and closes it the moment the
owner replies in that conversation themselves.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

log = logging.getLogger("autoreply.followups")

EOD_HOUR = int(os.getenv("HOLD_EOD_HOUR", "18"))
START_HOUR = int(os.getenv("HOLD_START_HOUR", "10"))


def _next_business_day(d: datetime) -> datetime:
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def hold_deadline(urgency: float, now: datetime | None = None) -> tuple[str, datetime]:
    """(phrase, due) — e.g. ("by end of day", 18:00 today). Phrase is how the owner would say it."""
    now = now or datetime.now()
    u = int(round(urgency))
    if u >= 5:
        due = now + timedelta(hours=1)
        return "within the hour", due
    if u == 4:
        due = now + timedelta(hours=3)
        if due.hour >= EOD_HOUR + 2:  # don't promise 11pm
            nb = _next_business_day(now).replace(hour=START_HOUR, minute=0, second=0, microsecond=0)
            return ("tomorrow morning" if nb.date() == (now + timedelta(days=1)).date() else "Monday morning"), nb
        return f"by {due.strftime('%-I%p').lower()}", due
    if u == 3:
        if now.hour < EOD_HOUR - 2 and now.weekday() < 5:
            return "by end of day", now.replace(hour=EOD_HOUR, minute=0, second=0, microsecond=0)
        nb = _next_business_day(now).replace(hour=START_HOUR, minute=0, second=0, microsecond=0)
        return ("tomorrow morning" if nb.date() == (now + timedelta(days=1)).date() else "Monday morning"), nb
    if u == 2:
        nb = _next_business_day(now).replace(hour=EOD_HOUR - 1, minute=0, second=0, microsecond=0)
        return ("tomorrow" if nb.date() == (now + timedelta(days=1)).date() else "on Monday"), nb
    # u <= 1
    days_to_fri = (4 - now.weekday()) % 7
    if days_to_fri == 0 and now.hour >= EOD_HOUR - 2:
        days_to_fri = 7
    if now.weekday() >= 5:
        days_to_fri = (4 - now.weekday()) % 7 or 7
    due = (now + timedelta(days=days_to_fri)).replace(hour=EOD_HOUR - 1, minute=0, second=0, microsecond=0)
    return ("this week" if days_to_fri < 5 else "early next week"), due


@dataclass
class Followup:
    id: str
    channel: str
    thread_ts: str | None
    counterpart: str
    counterpart_name: str
    summary: str
    phrase: str
    due: str          # ISO local time
    created: str
    status: str = "open"   # open | reminded | done
    permalink: str | None = None


class Followups:
    def __init__(self, path: str, remind: Callable[[Followup], None], lead_minutes: int = 15):
        self.path = Path(path)
        self.remind = remind
        self.lead = timedelta(minutes=lead_minutes)
        self.lock = threading.Lock()
        self.items: dict[str, Followup] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._load()
        for f in self.items.values():
            if f.status == "open":
                self._arm(f)

    # -- persistence
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for row in json.loads(self.path.read_text(encoding="utf-8")):
                f = Followup(**row)
                self.items[f.id] = f
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("could not read %s: %s", self.path, e)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(f) for f in self.items.values()], indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # -- api
    def add(self, *, channel: str, thread_ts: str | None, counterpart: str, counterpart_name: str,
            summary: str, phrase: str, due: datetime, permalink: str | None = None) -> Followup:
        f = Followup(
            id=uuid.uuid4().hex[:8], channel=channel, thread_ts=thread_ts, counterpart=counterpart,
            counterpart_name=counterpart_name, summary=summary[:200], phrase=phrase,
            due=due.isoformat(timespec="minutes"), created=datetime.now().isoformat(timespec="minutes"),
            permalink=permalink,
        )
        with self.lock:
            self.items[f.id] = f
            self._save()
        self._arm(f)
        return f

    def resolve(self, channel: str, thread_ts: str | None = None) -> list[Followup]:
        """Owner replied in this conversation → the promise is kept."""
        done = []
        with self.lock:
            for f in self.items.values():
                if f.status == "done" or f.channel != channel:
                    continue
                if thread_ts and f.thread_ts and f.thread_ts != thread_ts:
                    continue
                f.status = "done"
                done.append(f)
                t = self._timers.pop(f.id, None)
                if t:
                    t.cancel()
            if done:
                self._save()
        return done

    def open(self) -> list[Followup]:
        with self.lock:
            return sorted((f for f in self.items.values() if f.status != "done"), key=lambda f: f.due)

    # -- timers
    def _arm(self, f: Followup) -> None:
        due = datetime.fromisoformat(f.due)
        delay = max(1.0, (due - self.lead - datetime.now()).total_seconds())
        t = threading.Timer(delay, self._fire, [f.id])
        t.daemon = True
        self._timers[f.id] = t
        t.start()

    def _fire(self, fid: str) -> None:
        with self.lock:
            f = self.items.get(fid)
            if not f or f.status != "open":
                return
            f.status = "reminded"
            self._save()
        try:
            self.remind(f)
        except Exception:
            log.exception("reminder failed")
