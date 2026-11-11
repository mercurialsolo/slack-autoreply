#!/usr/bin/env bash
# Build Attention.app from attention/app/Sources and install it for this user.
#
#   attention/app/build.sh            build → ~/Applications/Attention.app, (re)start it, keep it running (LaunchAgent)
#   attention/app/build.sh --remove   stop it and remove the LaunchAgent (the app bundle stays)
#
# Needs Xcode or the Command Line Tools (swiftc). Ad-hoc signed: it runs on this machine only,
# which is the point. First launch: macOS asks whether Attention may send notifications — Allow.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HOME/Applications/Solo.app"
LABEL="io.solo.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  pkill -x Solo 2>/dev/null || true
  say "removed $LABEL — $APP kept"
  exit 0
fi

command -v swiftc >/dev/null || die "swiftc not found — install Xcode or: xcode-select --install"
BUILD="$(mktemp -d)/Solo.app"
mkdir -p "$BUILD/Contents/MacOS" "$BUILD/Contents/Resources"
cp "$HERE/Info.plist" "$BUILD/Contents/Info.plist"
# Solo's icon, from the repo's docs/icon.png, as an .icns
ICON_SRC="$HERE/../../docs/icon.png"
if [ -f "$ICON_SRC" ]; then
  SET="$(mktemp -d)/Solo.iconset"; mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$ICON_SRC" --out "$SET/icon_${s}x${s}.png" >/dev/null 2>&1
    sips -z $((s*2)) $((s*2)) "$ICON_SRC" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null 2>&1
  done
  iconutil -c icns "$SET" -o "$BUILD/Contents/Resources/Solo.icns" 2>/dev/null || true
fi
say "compiling…"
swiftc -swift-version 5 -O \
  -target arm64-apple-macosx14.0 \
  -framework AppKit -framework SwiftUI -framework UserNotifications -framework UniformTypeIdentifiers \
  "$HERE"/Sources/AskStore.swift "$HERE"/Sources/Notifier.swift "$HERE"/Sources/AskView.swift "$HERE"/Sources/BannerPanel.swift "$HERE"/Sources/main.swift \
  -o "$BUILD/Contents/MacOS/Solo"
codesign --force --sign - --deep "$BUILD" >/dev/null 2>&1 || die "ad-hoc codesign failed"

mkdir -p "$HOME/Applications" "$HOME/Library/LaunchAgents"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -x Solo 2>/dev/null || true
rm -rf "$APP"
cp -R "$BUILD" "$APP"
# Notifications are only granted to an app LaunchServices knows and launched: register the bundle,
# and start it with `open`, never by its executable path (that path gets "not allowed").
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP" >/dev/null 2>&1 || true
say "installed $APP"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-W</string><string>-a</string><string>$APP</string></array>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardErrorPath</key><string>$HOME/.attention/app.err.log</string>
</dict>
</plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 1
if pgrep -x Solo >/dev/null; then say "running  $LABEL — Solo's bell in the menu bar; allow notifications when asked"; else die "did not start — see ~/.attention/app.err.log"; fi
