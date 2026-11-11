#!/usr/bin/env bash
# Puts deploy/release-pull.sh on a 10-minute timer on the node: launchd on macOS, systemd --user on Linux.
# Run it from the node's git checkout (the one release-pull.sh moves between tags), not the plugin copy.
#   deploy/install-release-timer.sh            install + start
#   deploy/install-release-timer.sh --remove   stop + uninstall
#   deploy/install-release-timer.sh --status
# The daemon itself is deploy/install-service.sh; this only decides when a new release reaches it.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
if [ "${1:-}" != "--remove" ] && ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
  echo "$REPO is not a git checkout: run this from the checkout the node deploys from"; exit 1
fi
UNIT_PATH="$PATH"
mkdir -p "$HOME/.slack-autoreply"

case "$(uname -s)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.slack-autoreply.release-pull.plist"
    case "${1:-}" in
      --remove) launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true; rm -f "$PLIST"; echo "removed $PLIST"; exit 0;;
      --status) launchctl print "gui/$(id -u)/com.slack-autoreply.release-pull" 2>/dev/null | sed -n '1,12p' || echo "not loaded"; exit 0;;
    esac
    mkdir -p "$HOME/Library/LaunchAgents"
    sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO|g" -e "s|__PATH__|$UNIT_PATH|g" "$HERE/com.slack-autoreply.release-pull.plist" > "$PLIST"
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
    echo "installed $PLIST  (every 10 min: $REPO/deploy/release-pull.sh)"
    echo "log:       tail $HOME/.slack-autoreply/release-pull.log"
    ;;
  Linux)
    D="$HOME/.config/systemd/user"
    case "${1:-}" in
      --remove) systemctl --user disable --now slack-autoreply-release-pull.timer 2>/dev/null || true
                rm -f "$D/slack-autoreply-release-pull.service" "$D/slack-autoreply-release-pull.timer"; systemctl --user daemon-reload; echo "removed the release-pull timer"; exit 0;;
      --status) systemctl --user list-timers slack-autoreply-release-pull.timer --no-pager || true; exit 0;;
    esac
    mkdir -p "$D"
    for f in slack-autoreply-release-pull.service slack-autoreply-release-pull.timer; do
      sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO|g" -e "s|__PATH__|$UNIT_PATH|g" "$HERE/$f" > "$D/$f"
    done
    systemctl --user daemon-reload
    systemctl --user enable --now slack-autoreply-release-pull.timer
    echo "installed the release-pull timer  (every 10 min: $REPO/deploy/release-pull.sh)"
    echo "log:       journalctl --user -u slack-autoreply-release-pull"
    ;;
  *) echo "unsupported OS: run $REPO/deploy/release-pull.sh from cron instead"; exit 1;;
esac
echo "decisions: $HOME/.slack-autoreply/decisions.jsonl"
