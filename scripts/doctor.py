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
            # auth.test does not echo scopes in its body, but the response HEADER lists them.
            # Measured on a working install: the body had none, the header had all 15, and the
            # probe below had been reporting every scope but im:write as missing for weeks.
            hdr = (getattr(who, "headers", None) or {}).get("x-oauth-scopes") or ""
            scopes = {x.strip() for x in hdr.split(",") if x.strip()}
        if not scopes:
            # no header either; probe with a cheap call per family
            try:
                c.conversations_open(users=who["user_id"]); scopes |= {"im:write"}
            except Exception:
                pass
        missing = REQUIRED_SCOPES - scopes if scopes else set()
        if scopes and missing:
            r.add(FAIL, f"missing user scopes: {', '.join(sorted(missing))}", "re-create the app from manifest.yaml (or add them under OAuth & Permissions) and reinstall")
        elif scopes:
            r.add(OK, "all required user scopes granted")
        if scopes and "files:write" not in scopes:
            r.add(WARN, "files:write user scope not granted", "`say --file` cannot upload until you add it under OAuth & Permissions and reinstall the app (manifest.yaml has it)")
        if not scopes:
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
    import slackctl   # local: importing it at module scope would pull in slack_sdk
    if CFG["dispatch"] != "claude":
        r.add(WARN, f"DISPATCH={CFG['dispatch']}", "messages will only be queued; drain with /solo:slack-autoreply run (or /loop 2m /solo:slack-autoreply run)")
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
        res = None
        try:
            res = json.loads(out.splitlines()[-1]) if out else None
        except json.JSONDecodeError:
            pass
        # Same predicate the daemon's probe uses, so doctor and `status` can never disagree about
        # whether the writer works. A logged-out run answers *with* a result object, so the exit
        # code alone is not the test.
        ok, why = slackctl.writer_healthy(res, f"exit {p.returncode}: {(p.stderr or out).strip()[-200:]}")
        if ok:
            r.add(OK, f"claude -p works on your login (cost est ${(res or {}).get('total_cost_usd') or 0:.4f})")
        else:
            r.add(FAIL, "claude -p failed", f"{why} — run `claude` once interactively and /login; for unattended use consider `claude setup-token`")
    except subprocess.TimeoutExpired:
        r.add(FAIL, "claude -p timed out", "run `claude` interactively once to finish login/onboarding")
    # the plugin resolves. The daemon's prompt is `/solo:slack-autoreply …`, which exists only if
    # Claude Code loads this directory as the plugin named solo: from a marketplace install
    # (~/.claude/plugins/cache/solo/solo/<version>) or as a skills-dir plugin (~/.claude/skills/solo).
    # The daemon must run from that same directory: the skill tells the writer to call
    # `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/slackctl.py`, and the daemon pre-approves only its own
    # path, so a mismatch is denied silently under dontAsk.
    root = config.SKILL_DIR
    if not (root / ".claude-plugin" / "plugin.json").exists():
        r.add(FAIL, f"{root} is not a plugin root", "no .claude-plugin/plugin.json beside scripts/ — re-run install.sh from a current checkout, or `claude plugin install solo@solo`")
    installs = installed_plugins(path)
    mine = [i for i in (installs or []) if i.get("id", "").startswith(f"{PLUGIN}@")]
    here = [i for i in mine if Path(i.get("installPath", "")).resolve() == root]
    if config.SKILL_COMMAND != f"/{PLUGIN}:slack-autoreply":
        r.add(WARN, f"SKILL_COMMAND={config.SKILL_COMMAND}", "the daemon will not invoke the plugin's skill; fine only while this machine still has the old ~/.claude/skills/slack-autoreply copy")
    elif here and all(i.get("enabled", True) for i in here):
        r.add(OK, f"plugin {here[0]['id']} loads from this directory")
    elif here:
        r.add(FAIL, f"plugin {here[0]['id']} is disabled", f"claude plugin enable {here[0]['id']}")
    elif mine:
        other = mine[0]
        r.add(WARN, f"daemon runs from {root}, but plugin {other['id']} loads from {other.get('installPath')}",
              "the writer's slackctl calls would be denied — re-run /solo:deploy (or deploy/install-service.sh) from the installed version")
    elif installs is None:
        r.add(WARN, "could not list plugins (`claude plugin list --json` failed)", f"check by hand that `/{PLUGIN}:slack-autoreply` is a known skill in `claude`")
    else:
        r.add(FAIL, f"no plugin named {PLUGIN} is installed",
              f"claude plugin marketplace add mercurialsolo/slack-autoreply && claude plugin install {PLUGIN}@{PLUGIN}   — or ./install.sh from a checkout, which installs it under ~/.claude/skills/{PLUGIN}")
    old = Path.home() / ".claude" / "skills" / "slack-autoreply"
    if old.exists() and old.resolve() != root:
        r.add(WARN, f"old copy at {old}", "it still defines a /slack-autoreply skill beside the plugin's; remove it once the service runs from the plugin")

PLUGIN = "solo"


def installed_plugins(claude_bin: str) -> list[dict] | None:
    """What `claude plugin list --json` says, or None when that could not be asked."""
    try:
        p = subprocess.run([claude_bin, "plugin", "list", "--json"], capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else data.get("plugins", [])


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
        r.add(WARN, "no voice samples yet", "run /solo:slack-autoreply setup in Claude Code (or: slackctl build-voice)")
    else:
        r.add(OK, f"{n} voice samples")
    if not notes or "(lowercase? full stops" in notes:
        r.add(WARN, "VOICE.md is the blank template", "run /solo:slack-autoreply setup — it drafts this from your messages and asks you three questions")
    else:
        r.add(OK, f"VOICE.md filled in ({len(notes.splitlines())} lines)")
    if CFG["mode"] not in ("learn", "draft", "auto"):
        r.add(FAIL, f"MODE={CFG['mode']}", "must be learn | draft | auto")
    else:
        r.add(OK, f"MODE={CFG['mode']}" + ("  (recommended to start)" if CFG["mode"] == "learn" else ""))


def check_secrets(r: Report):
    line = config.load_secrets()
    r.add(WARN if "failed" in line else OK, line)
    for k, where in sorted(config.SECRET_SOURCES.items()):
        r.add(OK, f"{k} from {where}")


def run() -> int:
    r = Report()
    check_python(r)
    check_state(r)
    check_secrets(r)
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
