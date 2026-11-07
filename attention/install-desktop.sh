#!/usr/bin/env bash
# Install the attention daemon on the machine the owner sits at (macOS).
#
#   attention/install-desktop.sh            install or update the LaunchAgent, start it, print the sender settings
#   attention/install-desktop.sh --remove   stop and remove it (the token file stays)
#
# What it does, and nothing else:
#   1. ~/.attention/env (0600) with a fresh ATTN_TOKEN, unless one is already there.
#   2. ~/Library/LaunchAgents/io.solo.attention.plist running `attn.py serve` from this checkout,
#      kept alive across logout and reboot, logs in ~/.attention/.
#   3. Prints the two lines a sender needs (ATTN_URL, ATTN_TOKEN) — put them in the sender's
#      ~/.slack-autoreply/.env on the node, nowhere else.
#
# First run: macOS asks once whether python3 may accept incoming connections. Click Allow —
# the daemon binds the tailscale address, so only the tailnet can reach it, and every request
# needs the token. For the real dialogs (on top, blurred, countdown bar): brew install --cask swiftdialog
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATTN="$HERE/attn.py"
HOME_DIR="${ATTN_HOME:-$HOME/.attention}"
ENV_FILE="$HOME_DIR/env"
LABEL="io.solo.attention"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
[ -f "$ATTN" ] || die "no attn.py next to this script ($ATTN)"
[ "$(uname)" = "Darwin" ] || die "the desktop daemon is macOS only (launchd, osascript)"

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  say "removed $LABEL — $ENV_FILE kept (delete it yourself if the token should die)"
  exit 0
fi

mkdir -p "$HOME_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$HOME_DIR"
if [ ! -f "$ENV_FILE" ] || ! grep -q '^ATTN_TOKEN=' "$ENV_FILE"; then
  TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  { echo "# attention daemon — read by attention/attn.py. Plain KEY=value, 0600."; echo "ATTN_TOKEN=$TOKEN"; } >> "$ENV_FILE"
  say "token   written to $ENV_FILE"
fi
chmod 600 "$ENV_FILE"
# Escalate a p0 that sits unanswered through the same email path the nodes use, when it is set up here.
if ! grep -q '^ATTN_ESCALATE_CMD=' "$ENV_FILE" && command -v node-notify >/dev/null 2>&1 && [ -f "$HOME/.config/node-notify/env" ]; then
  echo "ATTN_ESCALATE_CMD=node-notify blocked" >> "$ENV_FILE"
  say "escalate  node-notify blocked (half way to a p0 deadline, unanswered)"
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>$ATTN</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>$HOME</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$HOME_DIR/serve.out.log</string>
  <key>StandardErrorPath</key><string>$HOME_DIR/serve.err.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
URL="$(sed -n 's/^attn: serving on \(http:[^ ]*\).*/\1/p' "$HOME_DIR/serve.out.log" | tail -1)"
[ -n "$URL" ] || { say "daemon did not report an address yet — check $HOME_DIR/serve.err.log"; exit 1; }
say "daemon  $LABEL running at $URL"
say ""
say "On each sender (the node's ~/.slack-autoreply/.env, nothing else):"
say "  ATTN_URL=$URL"
say "  ATTN_TOKEN=$(sed -n 's/^ATTN_TOKEN=//p' "$ENV_FILE")"
say "  ATTN_FALLBACK_CMD=node-notify blocked      # when this desktop is unreachable"
say ""
say "then from the node:  python3 ~/.claude/skills/solo/attention/attn.py doctor"
