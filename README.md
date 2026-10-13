# slack-autoreply

Your logged-in **Claude Code** replies to your Slack messages **from your own account**, in your voice, with your context — and earns the right to do it unsupervised.

No bot user, no proxy handle, no API key for the replies. A small Python daemon listens to Slack and hands each message to `claude -p "/slack-autoreply handle …"` on your login, with your MCP servers, your folders, your CLAUDE.md. **Jev** (TypeSafe AI's System One model) decides in ~10 ms what each message deserves; Claude Code writes it; the daemon either posts as you or parks it in your self-DM for a ✅. Every correction you make is recorded, and a trust ledger graduates each (route, sender) pair from draft to auto once it has a track record.

**Deploying with a coding agent?** Hand it this folder and say "follow AGENTS.md". It is a phase-by-phase runbook with a verification command for every step.

![architecture](docs/architecture.svg)

## What's in the box

```
SKILL.md                 the agent's procedure: setup · handle · run · review · simulate · status
AGENTS.md                deployment runbook for a coding agent (phases, verifications, failure table)
install.sh               copies the skill to ~/.claude/skills, installs deps, creates ~/.slack-autoreply
Makefile                 install · doctor · test · setup · listen · service · status · logs
manifest.yaml            Slack app definition (user scopes only, Socket Mode, user events)
.env.example             every setting, commented
scripts/slackctl.py      daemon + CLI (listen, show, act, simulate, history, search, about, note, doctor, …)
scripts/doctor.py        preflight checks
scripts/triage.py        Jev (REST) + Claude fallback + policy thresholds
scripts/learning.py      outcomes, corrections, trust ledger, scorecard
scripts/followups.py     deadline math, reminders, auto-close
scripts/voice.py         VOICE.md template, sample pulling/picking, continuous capture
scripts/recall.py        dossiers (people/, channels/), history, search, about
scripts/config.py        paths + settings
deploy/                  launchd plist, systemd unit, install-service.sh
tests/test_offline.py    42 end-to-end checks with a fake Slack and a fake claude — no tokens needed
docs/architecture.svg    the diagram above
```

State lives in `~/.slack-autoreply/` (`.env`, `VOICE.md`, `voice_samples.json`, `people/`, `channels/`, `inbox/`, `outcomes.jsonl`, `corrections.jsonl`, `followups.json`, `state.json`, `replies.jsonl`) and is never touched by a reinstall.

## Install in seven steps

Each step ends with how you know it worked.

**1. Preconditions.** Python 3.10+, Claude Code on `PATH`, logged in once (`claude` → `/login`). Run on the machine where your projects and `claude mcp list` servers live.
`claude --version` prints a version.

**2. Install.**
```
./install.sh && python3 tests/test_offline.py
```
Last line: `42 checks passed`.

**3. Slack app.** api.slack.com/apps → *Create New App* → *From a manifest* → paste `manifest.yaml`. Then:
*Basic Information → App-Level Tokens → Generate* with `connections:write` → `xapp-…`.
*OAuth & Permissions → Install to Workspace* → copy the **User OAuth Token** → `xoxp-…` (not the bot token; there is no bot).
Only want DMs? Remove `message.channels` and `message.groups` from the manifest before creating the app.

**4. Configure.** Put the two tokens and `CONTEXT_DIRS` (folders Claude may read, read-only) in `~/.slack-autoreply/.env`. Optionally `TYPESAFE_API_KEY` for Jev.
```
make doctor
```
All `[ok]`/`[warn]`, ending `all good — start with: slackctl listen`.

**5. Train the voice** (you need to be there, ~15 min).
```
claude "/slack-autoreply setup"
```
It pulls your last 200 messages with what each replied to, studies them, writes `VOICE.md` quoting your own lines, asks you three things examples can't show (who to be careful with, what never to commit to on autopilot, what's fine on autopilot), seeds dossiers for your top counterparts, and dry-runs three messages so you hear it before it goes live.
`make doctor` now shows `N voice samples` and `VOICE.md filled in`.

**6. Run.** Foreground once, then as a service:
```
make listen        # watch one real DM go draft → ✅ → sent as you, then Ctrl-C
make service       # launchd (macOS) / systemd --user (Linux); make logs to follow
```

**7. Use it.** Drafts land in your Slack self-DM: ✅ sends as-is, ❌ drops, or just reply to the person yourself and it learns the difference. Weekly, `claude "/slack-autoreply review"`.

## How a message flows

1. **Slack pushes** the event over Socket Mode (user scopes → every DM, @mention and reaction on your behalf).
2. **Guards**: bots, allow/block lists, mentions-only in channels, quiet hours, presence, per-conversation rate cap. Your own messages go to the learning path instead.
3. **Jev triage** with the thread, the sender's dossier and your VOICE.md in its state: route, sensitivity 1–5, urgency 1–5, needs-your-files, asks-for-material. Policy on top: sensitivity ≥ 4 forces `escalate`; low confidence demotes `reply`→`hold`, `ignore`→`ack`. `ignore` stops here. No TypeSafe key → Claude Haiku (needs `ANTHROPIC_API_KEY`) → otherwise Claude Code decides the route itself.
4. **Inbox item** written; **wait 20–90 s** (minimum if urgent); stand down if you replied or they kept typing.
5. **Spawn Claude Code**: `claude -p "/slack-autoreply handle <item>" --permission-mode dontAsk --allowedTools … --max-turns 25`, one session per conversation (`--session-id`, then `--resume`), `ANTHROPIC_API_KEY` hidden so it bills your login.
6. **Claude** runs `slackctl show` (thread, triage, policy preview, dossiers, track record, 40 real messages), looks back if the message refers to something not in view (`history`, `search`, `about`), reads your folders / MCP servers if the answer needs something real, writes the text, notes anything durable it learned, and calls `slackctl act` — which only records the decision.
7. **Policy** (the daemon, not Claude): `MODE` × trust ledger × share gate (did Claude actually read your files, and is this sender trusted for that?) → post as you, or draft to your self-DM. `escalate` never auto-sends; `ack` is a 👀 reaction; `hold` commits to a real deadline and is tracked with a reminder.
8. **Learn**: ✅ / ❌ / you replied instead / you edited / you deleted / you answered something it ignored → `outcomes.jsonl` + before/after pairs → trust ledger (feeds step 7) and `review` (feeds VOICE.md and dossiers, which feed step 6 and step 3).

## Routes

| Route | Behaviour | Auto-send? |
|---|---|---|
| `ignore` | nothing; if you reply to it within 2 h it's logged as a miss | — |
| `ack` | 👀 reaction (or two words with `ACK_STYLE=message`) | always |
| `hold` | one line committing to "by end of day" / "Monday morning" (from urgency); tracked, reminder 15 min before, closed when you reply | once earned |
| `reply` | the answer, researched in your folders/services when needed | once earned, and never if it used your context for an untrusted sender |
| `escalate` | money, legal, people, investors, health, personal, complaints, conflict, confidential, prompt-injection attempts | never — ⚠️ + draft |

**Earned** (`MODE=learn`): 5 outcomes with that sender at ≥ 85 % good and < 2 bad in the last 5, or 20 across everyone at ≥ 90 %. Two bad in the last five → back to draft. `MODE=draft` = everything waits; `MODE=auto` = everything goes (escalations still don't).

## What Claude can touch while composing

`--permission-mode dontAsk`: anything not pre-approved is denied, not prompted.
Allowed: `Read` scoped to `CONTEXT_DIRS` with absolute-path rules, `Glob`, `Grep`, `WebFetch`, `WebSearch`, `mcp__<server>__*` for servers in `claude mcp list` (restrict with `CONTEXT_MCP_ALLOW`), and `python3 …/slackctl.py *`. Removed: `Write`, `Edit`, `Bash` (other than slackctl), `Agent`. Caps: `CLAUDE_MAX_TURNS`, `CLAUDE_TIMEOUT_SECONDS`, `DISPATCH_PARALLEL`, one run at a time per conversation.

The **share gate** watches which tools Claude actually called, not what it claims: a reply that read your files or services goes to you for ✅ unless the sender is in `SHARE_TRUSTED_USERS` or has earned it (5 context-backed replies, ≥ 90 %, none bad). SKILL.md also tells Claude that Slack content is data, not instructions, and to escalate attempts.

## Day to day

| In Claude Code | |
|---|---|
| `/slack-autoreply status` | queue, drafts waiting, open promises, scorecard, misconfiguration |
| `/slack-autoreply run` | drain the inbox (for `DISPATCH=none`, or after a failure); `/loop 2m /slack-autoreply run` keeps it in front of you |
| `/slack-autoreply review` | 14 days of outcomes + correction pairs → VOICE.md edits, dossier consolidation, who's earned auto |
| `/slack-autoreply simulate "…" --from Name` | dry run; nothing is sent |

| CLI (`python3 ~/.claude/skills/slack-autoreply/scripts/slackctl.py …`) | |
|---|---|
| `doctor` | preflight; sends nothing |
| `status`, `drafts`, `followups`, `inbox` | what's waiting |
| `approve <item>`, `drop <item>` | same as ✅/❌ |
| `history @who\|#chan`, `search "words" --with @who`, `about @who`, `note @who "fact"` | memory |
| `build-voice`, `voice`, `review-data` | voice + learning data |

## Notes and limits

- **Jev** is in early access (console.typesafe.ai). `triage.py` calls `POST /v1/systemone` with `{model, state, questions}` and reads answers keyed by question name, per the public guides; if the shape differs, the adapter is ~40 lines. Any Jev error falls back for that message.
- Each `handle` is a `claude -p` run on your plan; the daemon logs the per-run cost estimate when the CLI reports one. Sessions are resumed per conversation, renewed after 72 h / 20 runs.
- `claude -p` must resolve in the service's environment; `deploy/install-service.sh` copies your `PATH` into the unit. On a headless box use `claude setup-token`.
- claude.ai connectors aren't available to headless runs under an API key; under your login they follow Claude Code's own rules. `claude mcp add …` is the reliable route.
- Pending drafts survive a restart (they're in `state.json`); follow-ups persist too.
- Workspace admins may need to approve the app. The user token dies if you revoke the app; reinstall and paste the new one.
