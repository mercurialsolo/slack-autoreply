# attention — interrupt the owner, on purpose

Slack and email wait to be read. Some things cannot: a one-time code a login is waiting on, a
decision that blocks everything queued behind it. `attn` puts those on the owner's screen, on top
of whatever they are doing, keeps them there until dealt with, and shows how long they have.

The **sender decides the priority and the words**. This decides what that looks like.

| priority | what it means | what the owner sees |
|---|---|---|
| `p0` | something is blocked right now | a small window pinned on top in a corner (never over the whole screen: you keep working), countdown to the deadline, a sound every 30s, shown again if closed; a "Can't right now" button, always |
| `p1` | a decision is needed soon | on top with a countdown, one sound, shown again every `ATTN_NAG_MINUTES` until answered or the deadline passes |
| `p2` | worth knowing | one Notification Center banner, nothing persistent |

An ask ends in exactly one of: `answered` (with the text or choice), `declined` (the owner said not
now — that is an answer, not a failure), `expired` (the deadline passed), `cancelled` (the sender
withdrew it). A `p2` ends `shown`.

## Sending

From any machine on the tailnet with `ATTN_URL` and `ATTN_TOKEN` set:

```
attn ask "TOTP for solo@staffai.so" --body "the login on the mini is waiting" \
     --priority p0 --deadline 90s --reply text --from login-bot --wait
# → {"id": "ask-…", "state": "answered", "answer": "123456"}     exit 0

attn ask "Ship v0.48 to the box?" --priority p1 --deadline 20m --reply choice --choices ship,hold --wait
attn ask "Solve this" --image captcha.png --priority p0 --deadline 2m --reply text --wait      # the image is shown with the ask
attn ask "Upload the statement" --reply file --out ./statement.csv --wait                      # the owner picks a file; it lands at --out
attn fetch ask-… --out ./statement.csv                                                        # or later
attn notify "overnight warmup finished, 4 of 4 active" --from scout
attn cancel ask-…            # the sender no longer needs it: it comes off the screen
attn status                  # what is waiting on the owner
attn doctor                  # is this machine set up to send / to serve
```

Exit codes with `--wait`: 0 answered (or shown), 3 declined, 4 expired, 5 cancelled, 6 the desktop
was unreachable. On 6 the summary went through `ATTN_FALLBACK_CMD` (if set) so it still reached a
person; design for it — a 90s ask that cannot reach a screen should fail now, not queue.

On a Solo node use the same thing through Solo's config, so an agent needs none of its own:
`python3 ~/.claude/skills/solo/scripts/slackctl.py attn "<title>" --priority p0 …`, or from the
laptop `solo attn "<title>" …`. Only time-sensitive things draw attention, and Jev decides what is time-sensitive: triage asks
`needs_attention` ("does this need the owner's eyes in the next few minutes — a code, a live call,
someone blocked until they decide"), and Solo raises a `p1` for an escalation at or above
`ATTN_NEEDS` (0.7), taking it down again when the owner ✅/❌s the draft. The agent door is gated
the same way: `slackctl attn` scores the title and body with Jev and refuses a p0/p1 that reads
as able to wait, with the score; a `p2` banner is never gated. `ATTN_URGENCY` (4.5) stands in
only for a message Jev did not triage.

Replies are `none`, `text`, `choice` (`--choices a,b,c`) or `file`. Up to six images (png, jpg,
gif, webp, heic, pdf; 6 MB together) go with an ask and are shown in the dialog — in Preview beside
it on the AppleScript fallback. A file the owner answers with is copied beside the ask on the
daemon side and fetched by the sender; the owner's own path never leaves the machine.

Answers can be secrets. Asks are 0600 files under `~/.attention/asks/`, the log carries titles
only, and `--wait` prints the answer to stdout and nowhere else.

## Serving (the owner's machine, macOS)

```
attention/install-desktop.sh        # token, LaunchAgent io.solo.attention (the daemon), prints the sender settings
attention/app/build.sh              # Solo.app: the banner, the bell in the menu bar, the popover (needs Xcode / CLT)
```

**Solo.app** (attention/app) is what the owner sees. A banner at the top right that looks like a
macOS notification but is our own floating panel: it shows over everything, fullscreen apps
included, needs no notification permission, and holds the whole ask — context and images, a
paste-friendly field, choice buttons or a file drop zone, a live countdown, Not now and Send.
A bell in the menu bar carries the most urgent countdown and opens a popover with every open ask.
Answers are written straight into the ask file the daemon is polling. Sound: Basso for `p0`
(again on each nag), Glass once for `p1`, Tink for `p2`, which also slides away after 10s.

Real macOS notifications (reply field, action buttons) are attempted too, but macOS only grants
them to a Developer-ID-signed app; an ad-hoc build is refused, so the panel is the surface.
Without Solo.app running, the daemon falls back to a swiftDialog card (`brew install --cask
swiftdialog`) and then to AppleScript dialogs.

The daemon binds this machine's tailscale IPv4 (or `ATTN_BIND`) on `ATTN_PORT` (9911) and requires
the bearer token on everything but `/health`. macOS asks once whether python3 may accept incoming
connections: allow it. Config lives in `~/.attention/env` (0600):

```
ATTN_TOKEN=…                          # both ends
ATTN_ESCALATE_CMD=node-notify blocked # daemon: a p0 half way to its deadline, unanswered, also goes here
ATTN_NAG_MINUTES=5                    # daemon: how often a p1 is shown again
ATTN_URL=http://100.85.22.1:9911      # senders: where the daemon is
ATTN_FALLBACK_CMD=node-notify blocked # senders: when the daemon is unreachable
```

Open asks survive a daemon restart: they are shown again on start.

## HTTP, for a sender in another language

`POST /ask` `{"title", "body", "priority", "from", "deadline_s", "reply": "none|text|choice", "choices": []}` → 201 with the ask;
`GET /ask/<id>` → the ask (poll `state`); `POST /ask/<id>/cancel`; `GET /asks` → open ones; `GET /health` (no token).
`Authorization: Bearer <token>` on all but health.

## Not yet

- A phone. `ATTN_ESCALATE_CMD` is the hook; email through `node-notify` is what fills it today.
- `tailscale serve` for tailnet HTTPS with identity headers instead of a shared token.
- Linux or Windows renderers: `attn.py render` is the one place to add them.
