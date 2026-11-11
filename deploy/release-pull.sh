#!/usr/bin/env bash
# Runs on the node (the machine whose service holds Solo's Slack connection): moves its checkout to
# the newest release tag (solo--vX.Y.Z), tests it, installs it, restarts the daemon and checks it came
# back. A release that fails its tests is never installed; one whose daemon does not come back is
# rolled back to the commit it replaced, and that tag is held until a newer one is cut.
#
#   deploy/release-pull.sh              deploy the newest tag if the node is behind it (a timer runs this)
#   deploy/release-pull.sh --check      say what it would do; change nothing
#   deploy/release-pull.sh --tag T      deploy tag T (a hand rollback to an older release: no hold applies)
#
# Exit: 0 up to date, deployed, held or --check; 1 refused (dirty checkout, fetch failed, tests
# failed, a run already going); 2 rolled back; 3 the rollback did not come back either (page a person).
#
# Every run that decides something appends one JSON line to $SOLO_DECISIONS (the decision log):
#   {"at": ..., "kind": "deploy", "tag": ..., "from": ..., "to": ..., "result": ..., "why": ...}
#
# Settings (environment; the defaults suit the launchd/systemd service install-service.sh makes):
#   SOLO_REPO           the checkout to move                         (default: the repo holding this script)
#   SOLO_TAG_GLOB       the release tags                             (default: solo--v*)
#   SOLO_PY             the interpreter for the tests                (default: ~/.slack-autoreply/venv, else python3)
#   SOLO_TEST_CMD       the gate, run in the checkout before install (default: make test PY=$SOLO_PY)
#   SOLO_INSTALL_CMD    run in the checkout to install it            (default: ./install.sh)
#   SOLO_RESTART_CMD    restart the daemon                           (default: launchctl kickstart / systemctl --user restart)
#   SOLO_HEALTH_CMD     exit 0 once the restarted daemon is up       (default: a new "listening as" line since the restart)
#   SOLO_HEALTH_WAIT_S  how long to wait for it                      (default: 90)
#   SOLO_STATE          the state dir                                (default: ~/.slack-autoreply)
#   SOLO_DECISIONS      the decision log                             (default: $SOLO_STATE/decisions.jsonl)
# deploy/install-release-timer.sh runs this every 10 minutes under launchd or systemd --user.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${SOLO_REPO:-$(cd "$HERE/.." && pwd)}"
GLOB="${SOLO_TAG_GLOB:-solo--v*}"
STATE="${SOLO_STATE:-$HOME/.slack-autoreply}"
DECISIONS="${SOLO_DECISIONS:-$STATE/decisions.jsonl}"
HELD="$STATE/release-pull.held"          # the tag that was rolled back; skipped until a newer one exists
WAIT_S="${SOLO_HEALTH_WAIT_S:-90}"
PY="${SOLO_PY:-}"
if [ -z "$PY" ]; then
  if [ -x "$HOME/.slack-autoreply/venv/bin/python3" ]; then PY="$HOME/.slack-autoreply/venv/bin/python3"; else PY="$(command -v python3)"; fi
fi
TEST_CMD="${SOLO_TEST_CMD:-make test PY=$PY}"
INSTALL_CMD="${SOLO_INSTALL_CMD:-./install.sh}"
LOG="$STATE/listen.log"

check=0; want=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) check=1;;
    --tag) want="${2:?--tag needs a tag}"; shift;;
    -h|--help) sed -n '2,28p' "$0"; exit 0;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 1;;
  esac
  shift
done

say() { printf 'release-pull: %s\n' "$*"; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
# One decision line. Every value is a tag, a sha or a fixed phrase from this file, so none needs escaping
# beyond the quotes and backslashes a tag name can not hold (git refuses them in a ref).
decide() {  # result why [tag from to]
  mkdir -p "$(dirname "$DECISIONS")"
  printf '{"at": "%s", "kind": "deploy", "tag": "%s", "from": "%s", "to": "%s", "result": "%s", "why": "%s"}\n' \
    "$(now)" "${3:-$tag}" "${4:-$from}" "${5:-$to}" "$1" "$2" >> "$DECISIONS"
  say "$1: $2"
}

case "$(uname -s)" in
  Darwin) RESTART_DEFAULT="launchctl kickstart -k gui/$(id -u)/com.slack-autoreply";;
  *)      RESTART_DEFAULT="systemctl --user restart slack-autoreply";;
esac
RESTART_CMD="${SOLO_RESTART_CMD:-$RESTART_DEFAULT}"

# The default health check: the daemon logs "listening as @..." once it holds the Socket Mode
# connection. A line written after the restart counts; one from before it does not.
mark=""
health_mark() {
  if [ -n "${SOLO_HEALTH_CMD:-}" ]; then return; fi
  case "$(uname -s)" in
    Darwin) mark=$( { [ -f "$LOG" ] && wc -c < "$LOG"; } 2>/dev/null | tr -d ' '); mark=${mark:-0};;
    *)      mark=$(date +%s);;
  esac
}
healthy_once() {
  if [ -n "${SOLO_HEALTH_CMD:-}" ]; then (cd "$REPO" && sh -c "$SOLO_HEALTH_CMD") >/dev/null 2>&1; return; fi
  case "$(uname -s)" in
    Darwin) [ -f "$LOG" ] || return 1
            # a log cut short since the mark (rotated by hand) is read from its start
            [ "$(wc -c < "$LOG" | tr -d ' ')" -lt "$mark" ] && mark=0
            tail -c "+$((mark + 1))" "$LOG" | grep -q 'listening as';;
    *)      journalctl --user -u slack-autoreply --since "@$mark" --no-pager 2>/dev/null | grep -q 'listening as';;
  esac
}
restart_and_check() {
  health_mark
  (cd "$REPO" && sh -c "$RESTART_CMD") >/dev/null 2>&1 || return 1
  local waited=0
  while [ "$waited" -le "$WAIT_S" ]; do
    healthy_once && return 0
    sleep 2; waited=$((waited + 2))
  done
  return 1
}

# Everything below runs from inside main(): bash reads a function whole before running it, so the
# checkout of a new tag (which rewrites this very file) can not change the script halfway through.
main() {
cd "$REPO" 2>/dev/null || { echo "release-pull: no checkout at $REPO" >&2; exit 1; }
git rev-parse --git-dir >/dev/null 2>&1 || { echo "release-pull: $REPO is not a git checkout" >&2; exit 1; }
mkdir -p "$STATE"

# One run at a time: a timer firing while a slow test run goes would otherwise deploy twice.
LOCK="$STATE/release-pull.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  # a lock left by a run that died (older than an hour) is taken over
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then rmdir "$LOCK" 2>/dev/null; mkdir "$LOCK" 2>/dev/null || exit 1
  else say "another run holds $LOCK"; exit 1; fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

tag=""; from="$(git rev-parse HEAD)"; to=""

if ! git fetch --quiet --tags --force origin 2>/dev/null; then
  [ $check = 1 ] && { say "fetch from origin failed"; exit 1; }
  decide refused "fetch from origin failed" "-" "$from" "-"; exit 1
fi

if [ -n "$want" ]; then tag="$want"
else tag="$(git tag -l "$GLOB" --sort=-v:refname | grep -E '^[A-Za-z0-9._-]*v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"; fi
if [ -z "$tag" ]; then say "no tag matches $GLOB yet"; exit 0; fi
to="$(git rev-parse --verify --quiet "refs/tags/$tag^{commit}")" || { say "no such tag: $tag"; exit 1; }

if [ "$to" = "$from" ]; then say "up to date on $tag"; exit 0; fi
if [ -z "$want" ] && git merge-base --is-ancestor "$to" "$from" 2>/dev/null; then
  # a hand deploy (`solo deploy`) put the node past the tag: never move it back on a timer
  say "node is ahead of $tag ($(git rev-parse --short "$from")); nothing to do"; exit 0
fi
if [ -z "$want" ] && [ -f "$HELD" ] && [ "$(cat "$HELD")" = "$tag" ]; then
  say "$tag was rolled back; held until a newer tag (or --tag $tag by hand)"; exit 0
fi
if [ $check = 1 ]; then say "would deploy $tag ($(git rev-parse --short "$to")) over $(git rev-parse --short "$from")"; exit 0; fi

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  decide refused "checkout has uncommitted changes"; exit 1
fi

say "deploying $tag over $(git rev-parse --short "$from")"
git checkout --quiet --detach "$to" || { decide refused "checkout of the tag failed"; exit 1; }

if ! sh -c "$TEST_CMD" > "$STATE/release-pull.test.log" 2>&1; then
  git checkout --quiet --detach "$from"
  decide refused "tests failed on the tag (release-pull.test.log); nothing installed, the old daemon still runs"
  exit 1
fi

if sh -c "$INSTALL_CMD" > "$STATE/release-pull.install.log" 2>&1 && restart_and_check; then
  rm -f "$HELD"
  decide deployed "tests passed, daemon back within ${WAIT_S}s"
  exit 0
fi

# Roll back: the commit it replaced, installed and restarted the same way.
echo "$tag" > "$HELD"
git checkout --quiet --detach "$from"
if sh -c "$INSTALL_CMD" > "$STATE/release-pull.install.log" 2>&1 && restart_and_check; then
  decide rolled-back "the daemon did not come back on the tag; back on the commit it replaced" "$tag" "$to" "$from"
  exit 2
fi
decide rollback-failed "the daemon did not come back on the tag or after the rollback: a person is needed" "$tag" "$to" "$from"
exit 3
}
main "$@"
exit $?
