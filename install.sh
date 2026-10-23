#!/usr/bin/env bash
# Installs the Solo plugin (to ~/.claude/skills/solo from a checkout; in place from a marketplace install),
# creates ~/.slack-autoreply, installs deps.
# Idempotent. Re-run after unzipping a new version. Never overwrites your .env or VOICE.md.
#
#   ./install.sh            copy (default)
#   ./install.sh --link     symlink instead of copy (keep editing this checkout)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
STATE="${AUTOREPLY_HOME:-$HOME/.slack-autoreply}"
MODE="${1:-copy}"
PY="${PYTHON:-python3}"

say() { printf '%s\n' "$*"; }

# --- python
if ! command -v "$PY" >/dev/null 2>&1; then
  say "python3 not found. Install Python 3.10+ and re-run."; exit 1
fi
"$PY" - <<'EOF' || { echo "Python 3.10+ required"; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

# --- the plugin
# Solo is a Claude Code plugin named `solo`; its skills are /solo:slack-autoreply (the writer) and
# /solo:solo (how other agents report). The daemon runs FROM the plugin root: the skill tells the
# writer to call `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/slackctl.py`, and the daemon pre-approves
# only its own path, so they must be one directory. Two routes get there:
#   marketplace  `claude plugin install solo@solo` put us in ~/.claude/plugins/cache/solo/solo/<v>;
#                /solo:deploy runs this script from there. Nothing to copy — this IS the root.
#   clone        copy this checkout to ~/.claude/skills/solo, which Claude Code loads as the plugin
#                solo@skills-dir. (--link symlinks instead, for people editing the checkout.)
PLUGIN_CACHE="${CLAUDE_PLUGIN_CACHE:-$HOME/.claude/plugins/cache}"
case "$SRC" in
  "$PLUGIN_CACHE"/*) DEST="$SRC"; say "plugin  running from the installed plugin at $DEST";;
  *)
    DEST="$SKILLS_DIR/solo"
    # Refuse to become a second plugin named solo. A marketplace install already provides the
    # skills; two copies would load two /solo:slack-autoreply and the daemon's allow-list could
    # only ever match one of them.
    if command -v claude >/dev/null 2>&1; then
      other=$(claude plugin list --json 2>/dev/null | "$PY" -c '
import json, sys, pathlib
dest = pathlib.Path(sys.argv[1]).resolve()
for p in json.load(sys.stdin) or []:
    if p.get("id", "").startswith("solo@") and pathlib.Path(p.get("installPath", "")).resolve() != dest:
        print(p["id"], p.get("installPath", "")); break' "$DEST" 2>/dev/null || true)
      if [ -n "$other" ]; then
        say "solo is already installed as a plugin: $other"
        say "run /solo:deploy inside Claude Code to set up the daemon from there, or uninstall it first."
        exit 1
      fi
    fi
    # Before 1.1.0 install.sh dropped the agent skill at ~/.claude/skills/solo/SKILL.md on its own.
    # The plugin provides it now (skills/solo/), and a stray SKILL.md at the plugin root would
    # load as a third skill. install.sh wrote it, so install.sh may remove it.
    if [ -f "$DEST/SKILL.md" ] && [ ! -d "$DEST/.claude-plugin" ] && [ ! -L "$DEST" ]; then
      rm -f "$DEST/SKILL.md"; say "removed the pre-1.1.0 standalone solo skill at $DEST/SKILL.md (the plugin provides it)"
    fi
    mkdir -p "$SKILLS_DIR"
    if [ "$MODE" = "--link" ]; then
      if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then say "$DEST exists and is not a symlink; remove it or run without --link"; exit 1; fi
      ln -sfn "$SRC" "$DEST"
      say "linked  $DEST -> $SRC"
    elif [ "$SRC" != "$(cd "$DEST" 2>/dev/null && pwd -P)" ]; then
      mkdir -p "$DEST"
      # The whole tree minus git and caches, so the plugin manifest, skills, scripts, deploy
      # and tests travel together and cannot drift from each other.
      (cd "$SRC" && tar --exclude .git --exclude '__pycache__' --exclude '*.pyc' -cf - .) | (cd "$DEST" && tar -xf -)
      say "copied  $SRC -> $DEST  (loads as plugin solo@skills-dir)"
    else
      say "already installed at $DEST"
    fi
    if [ -f "$SKILLS_DIR/slack-autoreply/SKILL.md" ]; then
      say "note    $SKILLS_DIR/slack-autoreply is the pre-plugin layout and still defines /slack-autoreply."
      say "        once the service runs from $DEST (deploy/install-service.sh), remove it:  rm -r $SKILLS_DIR/slack-autoreply"
    fi
    ;;
esac
# Codex reads skills from ~/.codex/skills as drop-in directories, same SKILL.md frontmatter. The
# agent-reporting skill is installed there from the same file so the two cannot drift.
if [ -d "$HOME/.codex/skills" ] && [ -f "$SRC/skills/solo/SKILL.md" ]; then
  mkdir -p "$HOME/.codex/skills/solo"
  cp "$SRC/skills/solo/SKILL.md" "$HOME/.codex/skills/solo/SKILL.md"
  say "skill   solo -> $HOME/.codex/skills/solo (codex sessions too)"
fi
chmod +x "$DEST/scripts/slackctl.py" "$DEST/install.sh" "$DEST/deploy/install-service.sh" 2>/dev/null || true

# --- interpreter
# Solo owns its interpreter. Homebrew upgraded python3 3.14 -> 3.15 under a running daemon once:
# site-packages do not cross a minor version, so every new `slackctl` invocation died on
# ModuleNotFoundError while the already-running process kept working off its loaded modules —
# invisible until the next restart, which would then have failed to come up at all.
# --copies rather than symlinks so the venv survives the old formula being cleaned up.
VENV="$STATE/venv"
if [ -z "${AUTOREPLY_NO_VENV:-}" ]; then
  if [ ! -x "$VENV/bin/python3" ] || ! "$VENV/bin/python3" -c '' 2>/dev/null; then
    [ -e "$VENV" ] && rm -rf "$VENV"
    mkdir -p "$STATE"
    if "$PY" -m venv --copies "$VENV" 2>/dev/null; then
      say "venv    created at $VENV ($("$VENV/bin/python3" -V))"
    else
      say "venv    could not be created — falling back to $PY"
    fi
  fi
  [ -x "$VENV/bin/python3" ] && PY="$VENV/bin/python3"
fi
# install-service.sh derives both the service program and its PATH from PYTHON, so the daemon and
# the dispatched agent's bare `python3 slackctl.py` resolve to the same interpreter as this one.
export PYTHON="$PY"

# --- deps
if "$PY" -m pip --version >/dev/null 2>&1; then
  "$PY" -m pip install -q --user -r "$SRC/requirements.txt" 2>/dev/null \
    || "$PY" -m pip install -q --break-system-packages -r "$SRC/requirements.txt" 2>/dev/null \
    || "$PY" -m pip install -q -r "$SRC/requirements.txt"
  say "deps    installed ($( "$PY" -c 'import slack_sdk,dotenv,httpx; from importlib.metadata import version; print("slack_sdk", version("slack_sdk"))' 2>/dev/null || echo "slack_sdk, python-dotenv, httpx"))"
else
  say "pip not available for $PY — install: $(tr '\n' ' ' < "$SRC/requirements.txt")"
fi

# --- state dir
mkdir -p "$STATE/inbox" "$STATE/people" "$STATE/channels"
if [ ! -f "$STATE/.env" ]; then
  cp "$SRC/.env.example" "$STATE/.env"
  chmod 600 "$STATE/.env"
  say "created $STATE/.env  ← fill in SLACK_APP_TOKEN, SLACK_USER_TOKEN, CONTEXT_DIRS"
else
  say "kept    $STATE/.env"
fi
chmod 700 "$STATE" 2>/dev/null || true

say ""
say "next:  $PY $DEST/scripts/slackctl.py doctor"
