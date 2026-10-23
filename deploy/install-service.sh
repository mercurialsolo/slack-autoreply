#!/usr/bin/env bash
# Keeps `slackctl listen` running: launchd on macOS, systemd --user on Linux.
#   deploy/install-service.sh            install + start
#   deploy/install-service.sh --remove   stop + uninstall
#   deploy/install-service.sh --status
# The unit's PATH must contain `claude`; this script copies your current PATH into it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"     # the plugin root this script ships in: the daemon runs from here
PY="$(command -v "${PYTHON:-python3}")"
CLAUDE_BIN="$(command -v claude || true)"
if [ -z "$CLAUDE_BIN" ] && [ "${1:-}" != "--remove" ]; then
  echo "claude not on PATH — install Claude Code first (the daemon spawns it). Or set CLAUDE_BIN in ~/.slack-autoreply/.env and re-run with PATH including it."; exit 1
fi
UNIT_PATH="$(dirname "$CLAUDE_BIN"):$(dirname "$PY"):$PATH"

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.slack-autoreply.plist"
    case "${1:-}" in
      --remove) launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"; echo "removed $PLIST"; exit 0;;
      --status) launchctl print "gui/$(id -u)/com.slack-autoreply" 2>/dev/null | sed -n '1,12p' || echo "not loaded"; exit 0;;
    esac
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__HOME__|$HOME|g" -e "s|__ROOT__|$ROOT|g" -e "s|__PYTHON__|$PY|g" -e "s|__PATH__|$UNIT_PATH|g" "$HERE/com.slack-autoreply.plist" > "$PLIST"
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
    echo "installed $PLIST  (daemon runs from $ROOT)"
    echo "logs:   tail -f $HOME/.slack-autoreply/listen.log"
    echo "status: $0 --status"
    ;;
  Linux)
    UNIT="$HOME/.config/systemd/user/slack-autoreply.service"
    case "${1:-}" in
      --remove) systemctl --user disable --now slack-autoreply 2>/dev/null || true; rm -f "$UNIT"; systemctl --user daemon-reload; echo "removed $UNIT"; exit 0;;
      --status) systemctl --user status slack-autoreply --no-pager || true; exit 0;;
    esac
    mkdir -p "$(dirname "$UNIT")"
    sed -e "s|__HOME__|$HOME|g" -e "s|__ROOT__|$ROOT|g" -e "s|__PYTHON__|$PY|g" -e "s|__PATH__|$UNIT_PATH|g" "$HERE/slack-autoreply.service" > "$UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now slack-autoreply
    # keep running after logout (needs sudo once; harmless if it fails)
    loginctl enable-linger "$USER" 2>/dev/null || true
    echo "installed $UNIT  (daemon runs from $ROOT)"
    echo "logs:   journalctl --user -u slack-autoreply -f"
    echo "status: systemctl --user status slack-autoreply"
    ;;
  *) echo "unsupported OS: run manually →  nohup $PY $ROOT/scripts/slackctl.py listen >> $HOME/.slack-autoreply/listen.log 2>&1 &"; exit 1;;
esac
