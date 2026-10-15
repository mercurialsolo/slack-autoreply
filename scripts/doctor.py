"""Preflight: everything that has to be true before `listen` can do its job.

Each check prints ok / warn / FAIL with the fix. Exit 1 if anything FAILs.
Safe to run any time; makes no changes and sends nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import config
import voice
from config import CFG

OK, WARN, FAIL = "ok  ", "warn", "FAIL"
REQUIRED_SCOPES = {"channels:history", "channels:read", "chat:write", "groups:history", "groups:read", "im:history", "im:read",
                   "im:write", "mpim:history", "mpim:read", "reactions:read", "reactions:write", "search:read", "users:read"}


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []
        self.failed = False

    def add(self, level: str, what: str, detail: str = ""):
        self.rows.append((level, what, detail))
        if level == FAIL:
            self.failed = True

    def render(self) -> str:
        out = []
        for level, what, detail in self.rows:
            out.append(f"[{level}] {what}" + (f"\n       {detail}" if detail else ""))
        out.append("")
        out.append("all good — start with: slackctl listen" if not self.failed else "fix the FAILs above, then run doctor again")
        return "\n".join(out)


def check_python(r: Report):
    v = sys.version_info
    if v < (3, 10):
        r.add(FAIL, f"python {v.major}.{v.minor}", "needs 3.10+ (str | None syntax, dataclasses)")
    else:
        # Which interpreter, not just which version. A Homebrew minor-version bump once left the
        # daemon running on an interpreter whose modules had moved, and the version line alone
        # looked perfectly healthy while every new invocation failed.
        import config as _cfg
        own = str(_cfg.STATE_DIR / "venv") in sys.prefix
        r.add(OK, f"python {v.major}.{v.minor}.{v.micro} — "
                  + ("Solo's own venv" if own else f"{sys.executable} (not Solo's venv — "
                                                   "run install.sh so a python upgrade cannot break it)"))
    for mod, pkg in (("slack_sdk", "slack_sdk"), ("dotenv", "python-dotenv"), ("httpx", "httpx")):
        try:
            __import__(mod)
            r.add(OK, f"module {pkg}")
        except ImportError:
            r.add(FAIL, f"module {pkg} missing", f"pip install {pkg}   (or: pip install -r {config.SKILL_DIR}/requirements.txt)")


def check_state(r: Report):
    if not config.STATE_DIR.exists():
        r.add(FAIL, f"state dir {config.STATE_DIR} missing", "run install.sh, or: mkdir -p ~/.slack-autoreply && cp .env.example ~/.slack-autoreply/.env")
        return
    r.add(OK, f"state dir {config.STATE_DIR}")
    env = config.STATE_DIR / ".env"
    if not env.exists():
        r.add(FAIL, ".env missing", f"cp {config.SKILL_DIR}/.env.example {env} and fill in the tokens")
    else:
        mode = oct(env.stat().st_mode & 0o777)
        if env.stat().st_mode & 0o077:
            r.add(WARN, f".env is {mode}", f"chmod 600 {env}   (it holds your Slack user token)")
        else:
            r.add(OK, f".env present ({mode})")


def check_slack(r: Report):
    app = config.env("SLACK_APP_TOKEN")
    usr = config.env("SLACK_USER_TOKEN")
    if not app.startswith("xapp-") or len(app) < 20 or app.endswith("..."):
        r.add(FAIL, "SLACK_APP_TOKEN not set", "Basic Information → App-Level Tokens → generate with connections:write (starts xapp-); paste into ~/.slack-autoreply/.env")
    else:
        r.add(OK, "SLACK_APP_TOKEN looks right")
    if not usr.startswith("xoxp-") or len(usr) < 20 or usr.endswith("..."):
        hint = "that's a bot token — replies would come from a bot, not you" if usr.startswith("xoxb-") else "OAuth & Permissions → User OAuth Token (starts xoxp-); paste into ~/.slack-autoreply/.env"
        r.add(FAIL, "SLACK_USER_TOKEN not set" if not usr or usr.endswith("...") else "SLACK_USER_TOKEN", hint)
        return None
    try:
        from slack_sdk import WebClient
        c = WebClient(token=usr)
        who = c.auth_test()
        r.add(OK, f"Slack auth: @{who['user']} in {who.get('team')} ({who['user_id']})")
        scopes = set((who.get("response_metadata") or {}).get("scopes") or [])
        if not scopes:
            # auth.test doesn't always echo scopes; probe with a cheap call per family
            try:
                c.conversations_open(users=who["user_id"]); scopes |= {"im:write"}
            except Exception:
                pass
        missing = REQUIRED_SCOPES - scopes if scopes else set()
        if scopes and missing:
            r.add(FAIL, f"missing user scopes: {', '.join(sorted(missing))}", "re-create the app from manifest.yaml (or add them under OAuth & Permissions) and reinstall")
        elif scopes:
            r.add(OK, "all required user scopes granted")
        else:
            r.add(WARN, "could not list scopes", "if `listen` logs missing_scope, reinstall the app from manifest.yaml")
        return c, who
    except Exception as e:
        r.add(FAIL, "Slack auth failed", f"{e}".splitlines()[0][:200])
        return None


def check_socket_mode(r: Report):
    app = config.env("SLACK_APP_TOKEN")
    if not app.startswith("xapp-") or len(app) < 20 or app.endswith("..."):
        return
    try:
        from slack_sdk import WebClient
        res = WebClient().apps_connections_open(app_token=app)
        if res.get("ok"):
            r.add(OK, "Socket Mode: connection URL issued (app token valid)")
    except Exception as e:
        r.add(FAIL, "Socket Mode handshake failed", f"{e}".splitlines()[0][:200] + " — is Socket Mode enabled and the token scoped connections:write?")


def check_claude(r: Report):
    if CFG["dispatch"] != "claude":
        r.add(WARN, f"DISPATCH={CFG['dispatch']}", "messages will only be queued; drain with /slack-autoreply run (or /loop 2m /slack-autoreply run)")
        return
    path = shutil.which(CFG["claude_bin"])
    if not path:
        r.add(FAIL, f"`{CFG['claude_bin']}` not on PATH", "install Claude Code, or set CLAUDE_BIN=/full/path/to/claude; for launchd/systemd make sure the unit's PATH includes it")
        return
    r.add(OK, f"claude binary: {path}")
    try:
        v = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20)
        r.add(OK, f"claude {v.stdout.strip() or v.stderr.strip()}"[:80])
    except Exception as e:
        r.add(WARN, "claude --version failed", str(e)[:120])
    # login: a trivial headless call; if it needs auth it fails fast
    env = dict(os.environ)
    if not CFG["dispatch_use_api_key"]:
        env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = subprocess.run([path, "-p", "reply with exactly: pong", "--output-format", "json", "--max-turns", "1"],
                           capture_output=True, text=True, timeout=90, env=env, cwd=str(config.STATE_DIR))
        out = p.stdout.strip()
        if p.returncode == 0 and out:
            try:
                res = json.loads(out.splitlines()[-1])
                r.add(OK, f"claude -p works on your login (cost est ${res.get('total_cost_usd') or 0:.4f})")
            except json.JSONDecodeError:
                r.add(OK, "claude -p works on your login")
        else:
            err = (p.stderr or p.stdout).strip().splitlines()[-1:] or ["no output"]
            r.add(FAIL, "claude -p failed", f"{err[0][:200]} — run `claude` once interactively and /login; for unattended use consider `claude setup-token`")
    except subprocess.TimeoutExpired:
        r.add(FAIL, "claude -p timed out", "run `claude` interactively once to finish login/onboarding")
    # skill resolves
    skill_home = Path.home() / ".claude" / "skills" / "slack-autoreply" / "SKILL.md"
    if skill_home.exists() or (Path(CFG["claude_cwd"] or ".") / ".claude" / "skills" / "slack-autoreply" / "SKILL.md").exists():
        r.add(OK, f"skill installed: {skill_home if skill_home.exists() else 'project .claude/skills'}")
    elif config.SKILL_DIR.name == "slack-autoreply" and config.SKILL_DIR.parent.name == "skills":
        r.add(OK, f"skill dir: {config.SKILL_DIR}")
    else:
        r.add(FAIL, "skill not in ~/.claude/skills/slack-autoreply", f"the daemon calls `/slack-autoreply handle`; copy or symlink {config.SKILL_DIR} there (install.sh does this)")


def check_context(r: Report):
    dirs = [d for d in CFG["context_dirs"] if d.strip()]
    if not dirs:
        r.add(WARN, "CONTEXT_DIRS empty", "Claude can't read any of your files while composing; set e.g. CONTEXT_DIRS=~/Projects,~/Documents")
    for d in dirs:
        if d.strip() == "*":
            r.add(WARN, "CONTEXT_DIRS=*", "Read is unrestricted — fine on a personal machine, not advised otherwise")
            continue
        p = Path(os.path.expanduser(d))
        if p.is_dir():
            r.add(OK, f"context dir {p}")
        else:
            r.add(FAIL, f"context dir {p} does not exist", "fix CONTEXT_DIRS in .env")
    try:
        import slackctl
        names = slackctl.discover_mcp_names(CFG["claude_cwd"] or (str(Path(os.path.expanduser(dirs[0])).resolve()) if dirs and dirs[0] != "*" else None), CFG["mcp_allow"])
        r.add(OK if names else WARN, f"MCP servers Claude may use: {', '.join(names) or 'none found'}",
              "" if names else "add services with `claude mcp add …` (Notion, Drive, GitHub, calendar) so replies can cite real things")
    except Exception as e:
        r.add(WARN, "could not read MCP config", str(e)[:120])


def check_triage(r: Report):
    if config.env("TYPESAFE_API_KEY"):
        try:
            import httpx
            resp = httpx.post(f"{os.getenv('TYPESAFE_BASE_URL', 'https://api.typesafe.ai/v1').rstrip('/')}/systemone",
                              headers={"Authorization": f"Bearer {os.getenv('TYPESAFE_API_KEY')}"},
                              json={"model": os.getenv("JEV_MODEL", "jev-latest"), "state": {"ping": True},
                                    "questions": {"q": {"type": "noul", "instructions": "Is this a ping?"}}}, timeout=10)
            if resp.status_code < 300:
                r.add(OK, f"Jev reachable ({os.getenv('JEV_MODEL', 'jev-latest')})")
            else:
                r.add(WARN, f"Jev returned {resp.status_code}", f"{resp.text[:160]} — triage will fall back per message")
        except Exception as e:
            r.add(WARN, "Jev not reachable", f"{str(e)[:120]} — triage will fall back per message")
    elif config.env("ANTHROPIC_API_KEY"):
        r.add(OK, f"triage: {CFG['triage_fallback_model']} via ANTHROPIC_API_KEY (no TypeSafe key)")
    else:
        r.add(WARN, "no triage model configured", "Claude Code decides the route itself per message (slower, no sensitivity floor). Set TYPESAFE_API_KEY or ANTHROPIC_API_KEY.")


def check_voice(r: Report):
    n = len(voice.load_samples())
    notes = voice.load_notes()
    if n == 0:
        r.add(WARN, "no voice samples yet", "run /slack-autoreply setup in Claude Code (or: slackctl build-voice)")
    else:
        r.add(OK, f"{n} voice samples")
    if not notes or "(lowercase? full stops" in notes:
        r.add(WARN, "VOICE.md is the blank template", "run /slack-autoreply setup — it drafts this from your messages and asks you three questions")
    else:
        r.add(OK, f"VOICE.md filled in ({len(notes.splitlines())} lines)")
    if CFG["mode"] not in ("learn", "draft", "auto"):
        r.add(FAIL, f"MODE={CFG['mode']}", "must be learn | draft | auto")
    else:
        r.add(OK, f"MODE={CFG['mode']}" + ("  (recommended to start)" if CFG["mode"] == "learn" else ""))


def run() -> int:
    r = Report()
    check_python(r)
    check_state(r)
    check_slack(r)
    check_socket_mode(r)
    check_claude(r)
    check_context(r)
    check_triage(r)
    check_voice(r)
    print(r.render())
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(run())
