#!/usr/bin/env python3
"""Offline tests for scripts/writer_auth.py: the writer's pool lease, against a fake `cluster` CLI.

The fake pool hands out accounts from FAKE_POOL_ACCOUNTS (comma-separated labels) in order and
logs every call to FAKE_POOL_LOG. Its behaviour is set through the environment:
    FAKE_POOL=empty     every lease fails with POOL_EMPTY
    FAKE_POOL=notoken   leases carry no apiKeyHelper (the account has no token file)
Nothing here runs `claude`, touches the real pool, or reads a token.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="writer-auth-test-"))
sys.path.insert(0, str(ROOT / "scripts"))
import writer_auth as W  # noqa: E402

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}  {detail}")
        shutil.rmtree(TMP, ignore_errors=True)
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


LOG = TMP / "calls.jsonl"
NEXT = TMP / "next"
FAKE = TMP / "cluster"
FAKE.write_text(f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
open({str(LOG)!r}, "a").write(json.dumps(a) + "\\n")
if a[:2] == ["pool", "lease"]:
    if os.environ.get("FAKE_POOL") == "empty":
        print("POOL_EMPTY: no account has room", file=sys.stderr); sys.exit(3)
    accts = os.environ["FAKE_POOL_ACCOUNTS"].split(",")
    n = int(open({str(NEXT)!r}).read() or 0) if os.path.exists({str(NEXT)!r}) else 0
    if n >= len(accts):
        print("POOL_EXHAUSTED", file=sys.stderr); sys.exit(4)
    open({str(NEXT)!r}, "w").write(str(n + 1))
    label = accts[n]
    s = {{"cleanupPeriodDays": 30}}
    if os.environ.get("FAKE_POOL") != "notoken":
        s["apiKeyHelper"] = "cat /pool/" + label + "/token"
    print("leased " + label)
    print(json.dumps({{"label": label, "lease_id": "L-" + label, "config_dir": "/pool/" + label,
                      "cli_args": ["--settings", json.dumps(s)]}}))
elif a[:2] == ["pool", "release"]:
    print("released")
""")
FAKE.chmod(0o755)


def reset(accounts="pool-1,pool-2,pool-3", mode=""):
    LOG.write_text("")
    NEXT.unlink(missing_ok=True)
    os.environ.update(CLUSTER_BIN=str(FAKE), WRITER_POOL="on", FAKE_POOL_ACCOUNTS=accounts, FAKE_POOL=mode)
    os.environ.pop("WRITER_POOL_TRIES", None)


def calls(verb):
    return [c for c in (json.loads(x) for x in LOG.read_text().splitlines()) if c[1] == verb]


# --- on/off ------------------------------------------------------------------------------------
reset()
check("on with a cluster CLI", W.enabled())
os.environ["WRITER_POOL"] = "off"
check("WRITER_POOL=off turns it off", not W.enabled() and W.lease(5) is None and not calls("lease"))
os.environ.update(WRITER_POOL="auto", CLUSTER_BIN=str(TMP / "no-such-cluster"))
check("no cluster CLI: off, whatever the mode says", not W.enabled() and W.lease(5) is None)

# --- lease -------------------------------------------------------------------------------------
reset()
ls = W.lease(17)
check("a lease is read from the last line of the CLI's output",
      ls is not None and ls.label == "pool-1" and ls.lease_id == "L-pool-1" and ls.helper == "cat /pool/pool-1/token")
check("…taken as solo-reply from the reserved seats, for the minutes asked",
      calls("lease")[0][2:] == ["solo-reply", "--reserved", "--minutes", "17"])
check("its --settings carries only the apiKeyHelper (the config dir stays the owner's)",
      json.loads(ls.settings_args()[1]) == {"apiKeyHelper": "cat /pool/pool-1/token"})

reset(mode="empty")
check("an empty pool: no lease, nothing to release", W.lease(5) is None and not calls("release"))

reset(mode="notoken")
check("an account with no token file is handed straight back",
      W.lease(5) is None and calls("release") and calls("release")[0][2:] == ["L-pool-1", "ok"])

# --- apply -------------------------------------------------------------------------------------
reset()
env = {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_AUTH_TOKEN": "y", "ANTHROPIC_API_KEY": "z"}
check("no lease held: command and env unchanged", W.apply(["claude", "-p", "hi"], env) == (["claude", "-p", "hi"], env))
with W._holding(W.Lease("pool-2", "L-pool-2", "cat /t")):
    c, e = W.apply(["claude", "-p", "hi"], env)
check("under a lease, every token that outranks the helper is dropped",
      not any(k in e for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")) and e["PATH"] == "/bin")
check("…and the lease's --settings is appended", c[:3] == ["claude", "-p", "hi"] and c[3] == "--settings"
      and json.loads(c[4]) == {"apiKeyHelper": "cat /t"})
check("…without touching the caller's env", env.get("CLAUDE_CODE_OAUTH_TOKEN") == "x")
check("the lease is let go when the block ends", W.current() is None)

# --- run_leased --------------------------------------------------------------------------------
LIMIT = "API Error: 429 {\"type\":\"error\",\"error\":{\"type\":\"rate_limit_error\"}}"

reset()
seen = []
out = W.run_leased(lambda: seen.append(W.current().label) or "drafted", lambda o: "", 5)
check("a run that works: one lease, released ok", out == "drafted" and seen == ["pool-1"]
      and [c[2:] for c in calls("release")] == [["L-pool-1", "ok"]])

reset()
seen = []
answers = iter([LIMIT, "You've hit your weekly limit", "drafted"])
out = W.run_leased(lambda: seen.append(W.current().label) or next(answers), lambda o: o if o != "drafted" else "", 5)
rel = [c[2:4] for c in calls("release")]
check("a 429 fails over to the next account until one works", out == "drafted" and seen == ["pool-1", "pool-2", "pool-3"])
check("…each full account released limit_hit, so the pool cools it down",
      rel == [["L-pool-1", "limit_hit"], ["L-pool-2", "limit_hit"], ["L-pool-3", "ok"]])
check("…with what the API said", calls("release")[0][4:6] == ["--text", LIMIT])

reset()
os.environ["WRITER_POOL_TRIES"] = "2"
n = []
out = W.run_leased(lambda: n.append(1) or LIMIT, lambda o: o, 5)
check("at most WRITER_POOL_TRIES accounts, then the 429 is the caller's", len(n) == 2 and out == LIMIT)

reset()
n = []
out = W.run_leased(lambda: n.append(1) or LIMIT, lambda o: o, 5, may_retry=lambda o: False)
check("a run that may have acted is not run again", len(n) == 1 and calls("release")[0][3] == "limit_hit")

reset(mode="empty")
n = []
out = W.run_leased(lambda: n.append(W.current()) or LIMIT, lambda o: o, 5)
check("no lease to be had: one run, on the host's own login", n == [None] and out == LIMIT)

reset()
try:
    W.run_leased(lambda: 1 / 0, lambda o: "", 5)
    raised = False
except ZeroDivisionError:
    raised = True
check("a run that raises still hands its lease back", raised and calls("release")[0][2:4] == ["L-pool-1", "failed"]
      and W.current() is None)

# --- rate limit wording ------------------------------------------------------------------------
for text in (LIMIT, "Claude AI usage limit reached|1763400000", "You've hit your limit · resets 3pm",
             "Extra usage is required to continue", "rate limited"):
    check(f"a rate limit: {text[:40]}", W.is_rate_limited(text))
for text in ("API Error: 500 overloaded", "Sent the draft to #general", "", "4290 messages"):
    check(f"not a rate limit: {text[:40]!r}", not W.is_rate_limited(text))

# --- auth_source -------------------------------------------------------------------------------
reset()
s = W.auth_source({"CLAUDE_CODE_OAUTH_TOKEN": "secret-value"})
check("doctor: the pool, and the token it drops, by name only",
      "pool lease" in s and "CLAUDE_CODE_OAUTH_TOKEN dropped" in s and "secret-value" not in s)
os.environ["WRITER_POOL"] = "off"
check("doctor: with the pool off, an env token is named as the one fixed account",
      "ANTHROPIC_AUTH_TOKEN" in W.auth_source({"ANTHROPIC_AUTH_TOKEN": "t"})
      and "one fixed account" in W.auth_source({"CLAUDE_CODE_OAUTH_TOKEN": "t"}))
check("doctor: the API key wins over everything", W.auth_source({"ANTHROPIC_API_KEY": "k"}).startswith("ANTHROPIC_API_KEY"))
check("doctor: nothing in the env is the settings/login", "settings" in W.auth_source({}))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} checks passed")
