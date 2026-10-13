#!/usr/bin/env bash
# Installs the skill into ~/.claude/skills/slack-autoreply, creates ~/.slack-autoreply, installs deps.
# Idempotent. Re-run after unzipping a new version. Never overwrites your .env or VOICE.md.
#
#   ./install.sh            copy (default)
#   ./install.sh --link     symlink instead of copy (keep editing this checkout)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
DEST="$SKILLS_DIR/slack-autoreply"
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

# --- skill
mkdir -p "$SKILLS_DIR"
if [ "$MODE" = "--link" ]; then
  if [ -e "$DEST" ] && [ ! -L "$DEST" ]; then say "$DEST exists and is not a symlink; remove it or run without --link"; exit 1; fi
  ln -sfn "$SRC" "$DEST"
  say "linked  $DEST -> $SRC"
else
  if [ "$SRC" != "$DEST" ]; then
    mkdir -p "$DEST"
    # copy the skill files; leave anything else in DEST alone
    for f in SKILL.md README.md AGENTS.md CLAUDE.md manifest.yaml requirements.txt .env.example VERSION CHANGELOG.md install.sh Makefile; do
      [ -e "$SRC/$f" ] && cp "$SRC/$f" "$DEST/$f"
    done
    rm -rf "$DEST/scripts" "$DEST/deploy" "$DEST/tests" "$DEST/docs"
    cp -R "$SRC/scripts" "$DEST/scripts"
    cp -R "$SRC/deploy"  "$DEST/deploy"
    cp -R "$SRC/tests"   "$DEST/tests"
    cp -R "$SRC/docs"    "$DEST/docs"
    find "$DEST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
    say "copied  $SRC -> $DEST"
  else
    say "already installed at $DEST"
  fi
fi
chmod +x "$DEST/scripts/slackctl.py" "$DEST/install.sh" "$DEST/deploy/install-service.sh" 2>/dev/null || true

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
