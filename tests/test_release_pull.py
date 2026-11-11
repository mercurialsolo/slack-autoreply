#!/usr/bin/env python3
"""Offline tests for deploy/release-pull.sh: a bare "origin" repo, a node clone, and fake test,
install, restart and health commands. Nothing here touches launchd, systemd, Slack or a token.

Each tagged release says how it behaves through files it carries:
    FAILTESTS   its test gate fails             -> never installed, node stays where it was
    BROKEN      its daemon never comes back     -> rolled back to the commit it replaced
A node whose ALWAYS_BROKEN marker exists (in the state dir) never comes back on any commit.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "release-pull.sh"
TMP = Path(tempfile.mkdtemp(prefix="release-pull-test-"))
ORIGIN, WORK, NODE, STATE = TMP / "origin.git", TMP / "work", TMP / "node", TMP / "state"
CALLS = STATE / "calls.log"

passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}  {detail}")
        shutil.rmtree(TMP, ignore_errors=True)
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()


def release(tag, files=(), drop=()):
    """Commit to the work repo (adding/removing marker files), push it and, if tag, tag it."""
    for f in files:
        (WORK / f).write_text(f + "\n")
        git(WORK, "add", f)
    for f in drop:
        git(WORK, "rm", "-q", f)
    git(WORK, "commit", "-q", "--allow-empty", "-m", f"release {tag or 'untagged'}")
    git(WORK, "push", "-q", "origin", "HEAD:main")
    if tag:
        git(WORK, "tag", tag)
        git(WORK, "push", "-q", "origin", tag)
    return git(WORK, "rev-parse", "HEAD")


def run(*args, **extra):
    env = {**GIT_ENV,
           "SOLO_REPO": str(NODE), "SOLO_STATE": str(STATE),
           "SOLO_TEST_CMD": f"test ! -f FAILTESTS && echo test >> {CALLS}",
           "SOLO_INSTALL_CMD": f"echo install $(git rev-parse --short HEAD) >> {CALLS}",
           "SOLO_RESTART_CMD": f"echo restart >> {CALLS}",
           "SOLO_HEALTH_CMD": f"test ! -f BROKEN && test ! -f {STATE}/ALWAYS_BROKEN",
           "SOLO_HEALTH_WAIT_S": "0"}
    env.update(extra)
    r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout + r.stderr


def head():
    return git(NODE, "rev-parse", "HEAD")


def decisions():
    p = STATE / "decisions.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def calls():
    out = CALLS.read_text().splitlines() if CALLS.exists() else []
    CALLS.unlink(missing_ok=True)
    return out


# ------------------------------------------------------------------ setup: origin, a work clone, the node
git(TMP, "init", "-q", "--bare", "-b", "main", str(ORIGIN))
git(TMP, "clone", "-q", str(ORIGIN), str(WORK))
git(WORK, "checkout", "-q", "-b", "main")
(WORK / "README").write_text("solo\n")
git(WORK, "add", "README")
git(WORK, "commit", "-q", "-m", "first")
git(WORK, "push", "-q", "origin", "main")
git(TMP, "clone", "-q", str(ORIGIN), str(NODE))
STATE.mkdir()
start = head()

# ------------------------------------------------------------------ no release yet
rc, out = run()
check("no solo--v tag yet: exit 0, nothing moves", rc == 0 and head() == start and "no tag" in out, out)
check("no tag: no decision line", decisions() == [])

# ------------------------------------------------------------------ a good release
v1 = release("solo--v1.0.0")
release("other-v9.9.9")  # a tag outside the release glob is never deployed
rc, out = run("--check")
check("--check says what it would do", rc == 0 and "would deploy solo--v1.0.0" in out, out)
check("--check changes nothing", head() == start and calls() == [] and decisions() == [])

rc, out = run()
check("the newest tag is deployed: exit 0", rc == 0 and head() == v1, out)
c = calls()
check("tests ran before install, then a restart", c == ["test", f"install {v1[:7]}", "restart"], c)
d = decisions()
check("one decision line: deployed, from -> to", len(d) == 1 and d[0]["result"] == "deployed"
      and d[0]["kind"] == "deploy" and d[0]["tag"] == "solo--v1.0.0" and d[0]["from"] == start and d[0]["to"] == v1, d)

rc, out = run()
check("a second run is a no-op", rc == 0 and "up to date" in out and calls() == [] and len(decisions()) == 1, out)

# ------------------------------------------------------------------ versions sort as versions, not text
v10 = release("solo--v1.10.0")
release("solo--v1.9.0")
rc, out = run()
check("solo--v1.10.0 beats solo--v1.9.0", rc == 0 and head() == v10, out)
calls()

# ------------------------------------------------------------------ a release whose tests fail
bad_tests = release("solo--v1.11.0", files=["FAILTESTS"])
rc, out = run()
check("failing tests: refused (exit 1), node stays on the old commit", rc == 1 and head() == v10, out)
check("failing tests: nothing installed, no restart", calls() == [])
check("failing tests: decision line says refused", decisions()[-1]["result"] == "refused"
      and decisions()[-1]["to"] == bad_tests, decisions()[-1])

# ------------------------------------------------------------------ a release whose daemon does not come back
broken = release("solo--v1.12.0", files=["BROKEN"], drop=["FAILTESTS"])
rc, out = run()
check("daemon not back on the tag: rolled back (exit 2)", rc == 2 and head() == v10, out)
c = calls()
check("rollback reinstalled the old commit and restarted again",
      c == ["test", f"install {broken[:7]}", "restart", f"install {v10[:7]}", "restart"], c)
d = decisions()[-1]
check("decision line: rolled-back, from the tag to the commit it replaced",
      d["result"] == "rolled-back" and d["from"] == broken and d["to"] == v10, d)
rc, out = run()
check("a rolled-back tag is held: the next run does not retry it", rc == 0 and head() == v10 and "held" in out and calls() == [], out)
rc, out = run("--tag", "solo--v1.12.0")
check("--tag by hand overrides the hold (and rolls back again)", rc == 2 and head() == v10, out)
calls()

fixed = release("solo--v1.13.0", drop=["BROKEN"])
rc, out = run()
check("a newer tag clears the hold and deploys", rc == 0 and head() == fixed and not (STATE / "release-pull.held").exists(), out)
calls()

# ------------------------------------------------------------------ a rollback that does not come back either
(STATE / "ALWAYS_BROKEN").write_text("")
release("solo--v1.14.0")
rc, out = run()
check("rollback that fails too: exit 3, a person is needed", rc == 3 and decisions()[-1]["result"] == "rollback-failed", out)
check("after a failed rollback the checkout is still the commit it replaced", head() == fixed)
(STATE / "ALWAYS_BROKEN").unlink()
calls()

# ------------------------------------------------------------------ a hand rollback to an older release
rc, out = run("--tag", "solo--v1.0.0")
check("--tag an older release deploys it (a hand rollback)", rc == 0 and head() == v1, out)
calls()

# ------------------------------------------------------------------ the node is ahead of the newest tag
newest = release("solo--v1.15.0")
run()
calls()
ahead = release(None)            # an untagged commit on main, pushed to the node by `solo deploy`
git(NODE, "fetch", "-q", "origin")
git(NODE, "checkout", "-q", "--detach", ahead)
rc, out = run()
check("a node already past the newest tag is never moved back on a timer", rc == 0 and head() == ahead and "ahead" in out, out)
check("node ahead: nothing installed", calls() == [])

# ------------------------------------------------------------------ refusals
git(NODE, "checkout", "-q", "--detach", newest)
release("solo--v1.16.0")
(NODE / "README").write_text("edited on the node\n")
n = len(decisions())
rc, out = run()
check("uncommitted changes on the node: refused, nothing moves", rc == 1 and head() == newest and calls() == [], out)
check("dirty refusal is logged", len(decisions()) == n + 1 and decisions()[-1]["why"].startswith("checkout has uncommitted"))
git(NODE, "checkout", "-q", "--", "README")

(STATE / "release-pull.lock").mkdir()
rc, out = run()
check("a run already going: the second exits 1 and moves nothing", rc == 1 and head() == newest and "another run" in out, out)
(STATE / "release-pull.lock").rmdir()

git(NODE, "remote", "set-url", "origin", str(TMP / "nowhere.git"))
rc, out = run()
check("fetch fails: refused and logged, nothing moves", rc == 1 and head() == newest
      and decisions()[-1]["why"] == "fetch from origin failed", out)
git(NODE, "remote", "set-url", "origin", str(ORIGIN))

rc, out = run("--tag", "solo--v0.0.404")
check("--tag that does not exist: exit 1", rc == 1 and "no such tag" in out, out)

# ------------------------------------------------------------------ the decision log is one JSON object a line
lines = (STATE / "decisions.jsonl").read_text().splitlines()
check("every decision line parses and has the fixed keys",
      all(set(json.loads(x)) == {"at", "kind", "tag", "from", "to", "result", "why"} for x in lines), lines)
check("no lock is left behind", not (STATE / "release-pull.lock").exists())

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{passed} checks passed")
