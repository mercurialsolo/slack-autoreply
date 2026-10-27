#!/usr/bin/env python3
"""Offline tests for attention/attn.py: a daemon on 127.0.0.1 with a fake renderer, a sender in-process.

Each ask's body tells the fake renderer what the owner "does":
    answer:<text>   the owner answered with <text>
    decline         the owner pressed "Can't right now"
    close           the dialog went away with no answer (quit, crashed, timed out)
    close-then:<t>  closed the first time, answered <t> the second — p0 must come back
    sleep           the dialog stays open until killed
"""
import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="attn-test-"))
os.environ["ATTN_HOME"] = str(TMP / "home")
os.environ["ATTN_TOKEN"] = "t0k3n"
os.environ["ATTN_NAG_MINUTES"] = "0.02"   # 1.2s between p1 showings
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "attention"))
import attn  # noqa: E402

FAKE = TMP / "fake-renderer.py"
FAKE.write_text(f"""#!/usr/bin/env python3
import json, sys, time
ask = json.load(open(sys.argv[sys.argv.index("--ask") + 1]))
body = ask["body"]
if body.startswith("answer:"):
    print(json.dumps({{"state": "answered", "answer": body[7:]}}))
elif body == "decline":
    print(json.dumps({{"state": "declined"}}))
elif body.startswith("close-then:"):
    print(json.dumps({{"state": "answered", "answer": body[11:]}} if ask["shown"] >= 2 else {{"state": "closed"}}))
elif body == "sleep":
    time.sleep(30)
else:
    print(json.dumps({{"state": "closed"}}))
""")
os.environ["ATTN_RENDERER"] = f"{sys.executable} {FAKE}"
ESCALATED = TMP / "escalated.txt"
FALLBACK = TMP / "fallback.txt"
for name, target in (("escalate", ESCALATED), ("fallback", FALLBACK)):
    s = TMP / f"{name}.sh"
    s.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{target}"\n')
    s.chmod(0o755)
os.environ["ATTN_ESCALATE_CMD"] = str(TMP / "escalate.sh")
os.environ["ATTN_FALLBACK_CMD"] = str(TMP / "fallback.sh")

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}  {detail}")
        shutil.rmtree(TMP, ignore_errors=True)
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


# ------------------------------------------------------------ a daemon on a free port
daemon = attn.Daemon()
srv = attn.ThreadingHTTPServer(("127.0.0.1", 0), attn.make_handler(daemon, "t0k3n"))
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
os.environ["ATTN_URL"] = f"http://127.0.0.1:{PORT}"


def run(*argv):
    """The sender CLI in-process → (exit code, parsed stdout JSON or raw text, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            attn.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int) and e.code:
                err.write(str(e.code))
    text = out.getvalue().strip()
    try:
        data = json.loads(text.splitlines()[-1]) if text else {}
    except (json.JSONDecodeError, IndexError):
        data = text
    return code, data, err.getvalue()


# ------------------------------------------------------------ the answer comes back
code, r, _ = run("ask", "TOTP for solo@staffai.so", "--body", "answer:123456", "--priority", "p0",
                 "--reply", "text", "--deadline", "30s", "--from", "login-bot", "--wait")
check("a p0 with a text reply returns the owner's answer", code == 0 and r["state"] == "answered" and r["answer"] == "123456", str(r))
check("the answer is never in the log", "123456" not in attn.LOG.read_text())
check("the ask file is private", (attn.ask_path(r["id"]).stat().st_mode & 0o777) == 0o600)

code, r, _ = run("ask", "Ship v0.48 now?", "--body", "answer:ship", "--priority", "p1", "--reply", "choice",
                 "--choices", "ship,hold", "--wait")
check("a choice comes back as the option picked", code == 0 and r["answer"] == "ship", str(r))

code, r, _ = run("ask", "Approve the price?", "--body", "decline", "--priority", "p0", "--deadline", "30s", "--wait")
check("'Can't right now' is an answer: declined, exit 3", code == 3 and r["state"] == "declined", f"{code} {r}")

# ------------------------------------------------------------ p0 is fixed: it comes back when closed
code, r, _ = run("ask", "Code needed", "--body", "close-then:777", "--priority", "p0", "--reply", "text",
                 "--deadline", "30s", "--wait")
ask = attn.load_ask(r["id"])
check("a p0 that was closed without an answer is shown again until answered",
      code == 0 and r["answer"] == "777" and ask["shown"] >= 2, f"{code} {r} shown={ask and ask['shown']}")

# ------------------------------------------------------------ p2 is one banner, nothing persistent
code, r, _ = run("notify", "deploy finished", "--body", "close", "--wait")
check("notify shows once and is done", code == 0 and r["state"] == "shown" and attn.load_ask(r["id"])["shown"] == 1, str(r))

# ------------------------------------------------------------ expiry and escalation
t0 = time.time()
code, r, _ = run("ask", "Decide before the call", "--body", "close", "--priority", "p1", "--deadline", "3s", "--wait")
check("a p1 nobody answers expires at its deadline, exit 4", code == 4 and r["state"] == "expired" and time.time() - t0 < 10, f"{code} {r}")
check("…after being shown more than once", attn.load_ask(r["id"])["shown"] >= 2, str(attn.load_ask(r["id"])["shown"]))

code, r, _ = run("ask", "Blocked: need the code", "--body", "close", "--priority", "p0", "--deadline", "4s", "--wait")
check("an unanswered p0 expires too", code == 4 and r["state"] == "expired", f"{code} {r}")
check("and ATTN_ESCALATE_CMD fired once it was half way to the deadline",
      ESCALATED.exists() and "Blocked: need the code" in ESCALATED.read_text() and ESCALATED.read_text().count("\n") == 1,
      ESCALATED.read_text() if ESCALATED.exists() else "no escalation")

# ------------------------------------------------------------ cancel kills what is on screen
code, r, _ = run("ask", "Still needed?", "--body", "sleep", "--priority", "p0", "--deadline", "60s")
check("without --wait the id comes back at once", code == 0 and r["state"] == "open" and r["id"].startswith("ask-"), str(r))
time.sleep(0.5)
code, text, _ = run("cancel", r["id"])
check("cancel is acknowledged", code == 0 and "cancelled" in text, str(text))
time.sleep(0.5)
check("the dialog process is gone", r["id"] not in daemon.procs)
check("status shows nothing open afterwards", run("status")[1] == "nothing waiting on the owner", str(run("status")[1]))

# ------------------------------------------------------------ the door is locked
os.environ["ATTN_TOKEN"] = "wrong"
code, _, err = run("ask", "x", "--wait")
os.environ["ATTN_TOKEN"] = "t0k3n"
check("a bad token is refused", code != 0 and "401" in err, err)

code, _, err = run("ask", "", "--wait")
check("a title is required", code != 0 and "title" in err, err)

# ------------------------------------------------------------ the desktop is off: fall back, say so
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
os.environ["ATTN_URL"] = f"http://127.0.0.1:{dead_port}"
code, _, err = run("ask", "Need you", "--body", "a decision", "--priority", "p0", "--wait")
os.environ["ATTN_URL"] = f"http://127.0.0.1:{PORT}"
check("an unreachable daemon is exit 6 and goes through ATTN_FALLBACK_CMD",
      code == 6 and FALLBACK.exists() and "[p0] Need you" in FALLBACK.read_text() and "unreachable" in err, f"{code} {err}")

# ------------------------------------------------------------ a restarted daemon re-shows what was open
stale = attn.new_ask("Left open", "answer:later", "p1", "test", 60, "text", [])
attn.save_ask(stale)
d2 = attn.Daemon()
for a in attn.open_asks():
    d2.start(a)
for _ in range(50):
    if attn.load_ask(stale["id"])["state"] != "open":
        break
    time.sleep(0.1)
check("an ask left open by a previous daemon is shown again on start", attn.load_ask(stale["id"])["answer"] == "later")

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} checks passed")
