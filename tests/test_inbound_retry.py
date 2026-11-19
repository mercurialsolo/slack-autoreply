"""inbound_retry: which failures are kept, when the next try is due, when the holding reply is owed."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
for _k in ("INBOUND_RETRY_FIRST_MIN", "INBOUND_RETRY_MAX_MIN", "INBOUND_RETRY_GIVE_UP_HOURS", "INBOUND_HOLD_AFTER_MIN"):
    os.environ.pop(_k, None)
import inbound_retry as R  # noqa: E402

FAILS: list[str] = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" — {detail}"))
    if not ok:
        FAILS.append(name)


T0 = datetime(2026, 11, 19, 19, 39, 0)

check("backoff: 1m, 2m, 4m … capped at 30m",
      [R.backoff_s(n) for n in (1, 2, 3, 4, 5, 6, 7)] == [60, 120, 240, 480, 960, 1800, 1800])

it = {"id": "A", "status": "handling"}
check("a login fault is not kept (it needs a person)", not R.schedule(it, "logged_out", "x", T0) and it["status"] == "handling")
check("no class is not kept", not R.schedule(it, None, "x", T0))

check("a rate limit is kept", R.schedule(it, "rate_limited", "429 " * 200, T0))
r = it["retry"]
check("…status retry_wait, attempt 1, due a minute later, the why trimmed",
      it["status"] == R.STATUS and r["attempts"] == 1 and r["next_at"] == "2026-11-19T19:40:00"
      and len(r["why"]) == 300 and it["outcome"]["status"] == R.STATUS, str(r))
R.schedule(it, "rate_limited", "429", T0 + timedelta(minutes=1))
check("…attempt 2 waits two minutes, first_at unchanged",
      it["retry"]["attempts"] == 2 and it["retry"]["next_at"] == "2026-11-19T19:42:00"
      and it["retry"]["first_at"] == "2026-11-19T19:39:00", str(it["retry"]))
check("…kept past 24 h: not kept again", not R.schedule(it, "rate_limited", "429", T0 + timedelta(hours=24)))

a = {"id": "a", "status": R.STATUS, "retry": {"first_at": "2026-11-19T19:00:00", "next_at": "2026-11-19T19:50:00"}}
b = {"id": "b", "status": R.STATUS, "retry": {"first_at": "2026-11-19T18:00:00", "next_at": "2026-11-19T19:45:00"}}
c = {"id": "c", "status": R.STATUS, "retry": {"first_at": "2026-11-19T17:00:00", "next_at": "2026-11-19T20:30:00"}}
dn = {"id": "d", "status": "done", "retry": {"first_at": "2026-11-19T16:00:00", "next_at": "2026-11-19T19:00:00"}}
check("due: only kept items whose time has come, oldest message first",
      [x["id"] for x in R.due([a, b, c, dn], datetime(2026, 11, 19, 20, 0))] == ["b", "a"])

h = {"status": R.STATUS, "retry": {"first_at": "2026-11-19T19:39:00"}}
check("holding reply: not owed at 19 min", not R.holding_owed(h, T0 + timedelta(minutes=19)))
check("…owed at 20 min", R.holding_owed(h, T0 + timedelta(minutes=20)))
R.mark_held(h, "123.456", T0 + timedelta(minutes=20))
check("…and only once", not R.holding_owed(h, T0 + timedelta(hours=2)) and h["retry"]["held"]["ts"] == "123.456")

check("a mangled next_at counts as due, not as a crash",
      [x["id"] for x in R.due([{"id": "m", "status": R.STATUS, "retry": {"next_at": "garbage"}}])] == ["m"])

check("alert tail: names the holding reply for a kept class", "20 min" in (R.alert_tail("rate_limited") or ""))
check("…none for a class that is not kept", R.alert_tail("logged_out") is None)
os.environ["INBOUND_HOLD_AFTER_MIN"] = "0"
try:
    check("INBOUND_HOLD_AFTER_MIN=0: no holding reply, and the alert does not promise one",
          not R.holding_owed({"status": R.STATUS, "retry": {"first_at": "2000-01-01T00:00:00"}})
          and "holding" not in R.alert_tail("rate_limited"))
finally:
    os.environ.pop("INBOUND_HOLD_AFTER_MIN")

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all inbound_retry checks passed'}")
sys.exit(1 if FAILS else 0)
