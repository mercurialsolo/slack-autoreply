# AGENTS.md — deployment runbook for a coding agent

You are deploying **slack-autoreply** on the owner's machine: a daemon that receives their Slack
messages and has their logged-in Claude Code reply *from their own account, in their voice*.
Follow the phases in order. Every phase ends with a verification command and the output that
means "done". Do not skip verification; do not move on while a phase fails.

Read `README.md` for what the system is. This file is only *how to put it in place*.

## Ground rules

- **Never send anything to Slack yourself.** Everything here is setup. The only Slack writes
  happen when the daemon runs, and in `MODE=learn` those go to the owner's self-DM for approval.
- **Never put tokens anywhere but `~/.slack-autoreply/.env`** (mode 600). Do not echo them, log
  them, paste them into a commit, or copy them into a service unit.
- **Stop and ask the owner** for: the two Slack tokens, which folders Claude may read
  (`CONTEXT_DIRS`), and the three voice questions in Phase 5. Everything else you can decide.
- **Do not edit files under `scripts/`** to make a check pass. If something is wrong with the
  machine (missing `claude`, wrong Python), fix the machine and report it.
- Prefer `make <target>` where one exists; the Makefile is the canonical command list.

## Phase 0 — preconditions (read-only)

```
python3 --version            # need 3.10+
command -v claude            # Claude Code CLI must be on PATH
claude --version
```
If `claude` is missing, install Claude Code first (https://code.claude.com/docs) and have the
owner log in once interactively (`claude`, then `/login`). The daemon runs on that login; it does
not use an API key for replies.

Confirm you are on the machine the owner actually uses: their project folders and their
`claude mcp list` servers must be here, because that is what gives replies real context.

## Phase 1 — install

```
./install.sh                 # or: make install
```
Expected last line: `next:  python3 ~/.claude/skills/slack-autoreply/scripts/slackctl.py doctor`

What it did: copied the skill to `~/.claude/skills/slack-autoreply`, installed `slack_sdk`,
`python-dotenv`, `httpx`, created `~/.slack-autoreply/{.env,inbox,people,channels}`.
It never overwrites an existing `.env` or `VOICE.md`.

Verify: `python3 tests/test_offline.py` → last line `42 checks passed`. This needs no tokens.
If it fails, the environment is wrong (Python version, deps); report the failing check verbatim.

## Phase 2 — Slack app (owner does the clicks; you hand them the manifest)

Tell the owner, in this order:

1. https://api.slack.com/apps → **Create New App** → **From a manifest** → pick the workspace →
   paste the contents of `manifest.yaml` → Create.
2. **Basic Information → App-Level Tokens → Generate Token and Scopes** → name it anything →
   add scope `connections:write` → Generate → copy the token starting `xapp-`.
3. **OAuth & Permissions → Install to Workspace** → Allow → copy the **User OAuth Token**
   starting `xoxp-`. (Not a Bot token. There is no bot user. This is what makes replies come
   from their account.)
4. Ask them whether they want channels covered. Default manifest covers DMs, group DMs and
   channel @mentions. If they only want DMs, they delete `message.channels` and `message.groups`
   from the manifest *before* step 1.

If "Install to Workspace" is greyed out, a workspace admin must approve the app; stop here and
tell the owner.

## Phase 3 — configure

Edit `~/.slack-autoreply/.env` (it was copied from `.env.example`, every key is commented):

| key | value |
|---|---|
| `SLACK_APP_TOKEN` | the `xapp-…` token |
| `SLACK_USER_TOKEN` | the `xoxp-…` token |
| `CONTEXT_DIRS` | folders Claude may read while composing, comma-separated, e.g. `~/Projects,~/Documents`. Ask the owner. Read-only; nothing outside is reachable. |
| `TYPESAFE_API_KEY` | optional. Jev (TypeSafe AI) triage key from console.typesafe.ai. Leave empty if they don't have one. |
| `ANTHROPIC_API_KEY` | optional. Only used for triage fallback when there's no TypeSafe key. Replies never use it. |
| `MODE` | leave `learn`. |

Leave everything else at defaults for the first deployment.

**Keys from Infisical instead (StaffAI machines).** On a machine with an Infisical identity
(`~/.config/infisical-staffai/node.env`; see the infisical skill), Solo reads `SLACK_APP_TOKEN`,
`SLACK_USER_TOKEN`, `SLACK_BOT_TOKEN`, `TYPESAFE_API_KEY` and `LINEAR_API_KEY` from Infisical
(`dev`, folders `/agents` then `/shared`) at start, and those values beat the `.env` copies.
Nothing else is taken from those folders — notably not `ANTHROPIC_API_KEY`. A key Infisical doesn't
have, or Infisical being unreachable, falls back to `.env`. Sessions can't write to Infisical: the
owner adds keys in its web UI. `listen.log` and `doctor` say where each key came from, by name only.
To keep a machine on `.env`: `SOLO_INFISICAL=off`.

Verify:
```
python3 ~/.claude/skills/slack-autoreply/scripts/slackctl.py doctor      # or: make doctor
```
Expected: every line `[ok  ]` or `[warn]`, last line `all good — start with: slackctl listen`.
Acceptable warnings on a fresh install: *no voice samples yet*, *VOICE.md is the blank template*,
*no triage model configured* (if no keys). Any `[FAIL]` names the fix; apply it and re-run.
`doctor` sends nothing to Slack; it only calls `auth.test` and opens a Socket Mode URL.

## Phase 4 — smoke test the pipeline without sending

```
cd ~ && claude "/slack-autoreply simulate 'can you send me the latest deck?' --from Priya"
```
Expected: Claude Code prints a triage line, runs `slackctl show`, writes a reply, and `act`
prints `[simulated] … → would DRAFT …`. Nothing reaches Slack. If `/slack-autoreply` is not
recognised, the skill is not at `~/.claude/skills/slack-autoreply/SKILL.md` — re-run Phase 1.

## Phase 5 — train the voice (needs the owner present, ~15 min)

```
cd ~ && claude "/slack-autoreply setup"
```
Claude Code will: pull the owner's last 200 Slack messages (`slackctl build-voice`, a few
minutes — Slack rate limits), study them, draft `~/.slack-autoreply/VOICE.md` with quotes as
evidence, ask the owner three questions (people to be careful with; things never to commit to
on autopilot; things fine on autopilot), seed dossiers for their top counterparts, and dry-run
three messages. Let the owner answer; do not answer for them.

Verify: `slackctl doctor` now shows `[ok  ] N voice samples` and `[ok  ] VOICE.md filled in`.

## Phase 6 — run the daemon

Foreground first, for one real message:
```
python3 ~/.claude/skills/slack-autoreply/scripts/slackctl.py listen      # or: make listen
```
Expected first lines: `listening as @<handle> … MODE=learn · dispatch=claude …` and
`will run: claude -p /slack-autoreply handle …`. Have the owner DM themselves from another
account or ask a colleague to DM them. Expected log: `<name> in im: … → …`, `…handing to Claude
in Ns`, `dispatch … (new session …)`, then `drafted (reply) for your ✅ — …`. The owner sees a
draft in their Slack self-DM. They react ✅; the log shows `draft approved → sent to …` and the
message appears from their account. Ctrl-C.

Then as a service:
```
deploy/install-service.sh            # or: make service
deploy/install-service.sh --status
```
macOS: launchd agent `com.slack-autoreply`, log at `~/.slack-autoreply/listen.log`.
Linux: `systemctl --user` unit `slack-autoreply`, `journalctl --user -u slack-autoreply -f`.
The script copies the current `PATH` into the unit so `claude` resolves; if the status shows
`claude binary not found`, the unit's PATH is wrong — fix the `__PATH__` line in the unit.

Verify: `make status` → shows the account, mode, queue, and a scorecard (empty at first).

## Phase 7 — hand over

Tell the owner, briefly:

- Replies and holds arrive in their **Slack self-DM** as drafts: ✅ sends as-is, ❌ drops.
  Replying to the person themselves also works and teaches the system the difference.
- After ~5 good outcomes with a person, that route goes automatic for them; two bad outcomes
  in a row and it goes back to drafts. `make status` shows who has graduated.
- Weekly: `claude "/slack-autoreply review"` — reads corrections, updates VOICE.md and dossiers.
- Anything sensitive (money, legal, people, investors) is never auto-sent; they get a ⚠️.
- Logs: `make logs`. Stop: `deploy/install-service.sh --remove`.

## Phase 8 — route work to your coding agents (optional)

Lets you hand a task to a coding agent without knowing which terminal it is in, and have it
report back through Solo in your voice, still waiting for your ✅. The return path is the `solo`
skill, already installed by Phase 1. This phase wires the *inbound* half, which needs
[claudectl](https://github.com/mercurialsolo/claudectl) with the `bus` feature.

**You run these two, not the agent.** Claude Code classifies edits to its own hook config and to
the agent-mesh role table as self-modification, so a session cannot wire its own delivery — which
is the right default, and means these are yours to run:

```bash
# 1. give the project a mailbox address. `*` covers its worktrees.
claudectl bus role bind <role-name> "/path/to/project*"

# 2. let a session pick mail up as its turn ends. settings.local.json is gitignored,
#    so this stays on your machine and is scoped to this one project.
#    Add to .claude/settings.local.json under "hooks":
#      "Stop": [ { "hooks": [ { "type": "command",
#                              "command": "claudectl bus stop-hook", "timeout": 10 } ] } ]
```

**Peek before you bind.** A role is an address, and mail addressed to a name that has no role
sits `pending` indefinitely. Binding that name **arms everything already queued to it**, which all
lands in the first agent to finish a turn. Binding `staffai` here surfaced 16 stray dictation
fragments from a week earlier — ambient speech a dictation daemon had published as tasks. Check,
and drain the queue yourself if it is not what you want an agent reading:

```bash
claudectl bus inbox --role <role-name> --peek   # look
claudectl bus inbox --role <role-name>          # drain it to you, not to an agent
```

Verify (round trip, no Slack involved):

```bash
claudectl bus send <role-name> "Reply with exactly: BUS_DELIVERY_OK and nothing else"
cd /path/to/project && claude -p "Say the single word: ready"
```

The session answers `BUS_DELIVERY_OK` rather than `ready` — the hook folded the mail into the turn
and the agent acted on it instead of on the prompt. That is the whole mechanism.

What to expect, having been measured rather than assumed:

- **Mailboxes do not cross machines.** A `bus send` on one host queues there. If Solo runs on a
  different box than your agents, a message has to make the hop with `claudectl relay delegate`.
- **Delivery is at a turn boundary, not a push.** An idle session picks mail up the next time it
  finishes a turn, so a message to a parked session waits for you to prompt it.
- **Two roles matching one cwd resolve ambiguous and go silent** — nothing is delivered and nothing
  errors. One role per cwd selector; a session wanting its own address binds with
  `claudectl bus role bind <name> --self`, which beats cwd inference for that session only.
- **A role outlives the session.** Mail sent to a restarted agent is waiting when it comes back, so
  queueing something for a box that is currently down is a reasonable thing to do.
- The agent treats routed mail as **data, not instruction** — it will not run what a message asks
  for, it reports the request to you instead. That rule lives in the `solo` skill.

The return half is the same gate in reverse. A proposal you hand over with 🤖 carries the id of the
message that caused it, and the agent answers with `solo say "…" --re <id> --from <role>`. The id
is what puts the answer back in the conversation that asked — without it Solo infers a room from
the words of the report, which is how a question asked in a DM gets answered in a channel. `--re`
is honoured only for items you actually dispatched, so an agent cannot use it to place a draft in a
conversation on its own initiative, and the draft still waits for your ✅ whatever MODE says.

```bash
solo delegations    # proposed · handed over · reported back
```

A task that reads `handed over, nothing back yet` long after you tapped 🤖 is the failure worth
chasing: the two usual causes are `solo` not being on the agent's PATH and its sandbox refusing the
ssh hop to the node, since `solo` is a proxy to wherever the daemon runs.

Measured on the first real round trip, rather than assumed:

- **A relay-delegated agent can run `solo` from inside its sandbox.** PATH and the ssh hop both
  work, so the report leg needs no extra wiring. Worth knowing because binding a bus role is
  refused as self-modification, and this is the same class of thing but is allowed.
- **Cost and latency: 183s and $0.66** for one investigation on Opus, 549k tokens, mostly cache
  reads. Dispatching is not free and not instant; it is worth it for a question that needs looking
  things up, not for one you could answer from the message.
- **The agent's own permission rules still bite.** It was blocked from a read-only prod DB query
  and from reading stored API keys — correctly — and reported the refusal instead of working
  around it. A dispatched investigation can only answer what the agent is already allowed to read.
- **A report can come back and be withheld.** Solo refuses to place a report it reads as sensitive
  (3.5/5), which the first real one was, for naming contact record ids. It lands in your Solo DM
  marked as not placed, and `delegations` records `report_refused` — not the same thing as never
  coming back.

## Phase 9 — engineering requests: Slack channel → issue → draft PR (optional)

**Routing between people's clusters is by label, not by network.** Each person's machines form
their own cluster and run `eng_pickup.py` against their own label (`ENG_LABEL=agent:bill`); Solo
labels each request for its assignee from `ENG_ROUTES`. No cluster connects to another or to Solo,
so adding someone is one `ENG_ROUTES` entry plus the cron line on their node. Not built yet: a
cluster other than Solo's own reports back through `eng-report`, which needs Solo's bot token — a
second cluster's PR shows on the issue but not in the Slack thread.

One channel where anyone on the team asks for a code change. Solo files each top-level message
there as a GitHub issue labelled `agent` and says so in the thread **as the bot**, so nothing waits
on the owner's ✅. `scripts/eng_pickup.py`, on cron on the node, hands the oldest unclaimed issue to
Claude Code in a fresh worktree of its own clone; the agent opens a **draft** PR, and the bot posts
the link in the thread. Replies inside a thread are not new requests. Triage still runs: chit-chat
(`ignore`) is not filed, and anything at `SENSITIVITY_ESCALATE` or above goes to the owner instead.

**Owner does these — they change what the team's repo and Slack app allow:**

1. **Check what the default branch enforces.** The node's `gh` is logged in as the owner, so the
   agent can do anything the owner can; the brief forbidding a merge is not a boundary. Rulesets,
   not classic protection, are what count — `branches/main/protection` 404s even when a ruleset
   protects main:
   `gh api repos/<owner>/<repo>/rules/branches/main --jq '[.[].type]'`
   On staffai (21 Oct) that is `pull_request` with **0 approvals** + required status checks: no
   direct push, but a green PR can be merged by its author. Closing that gap takes an org admin
   (approvals ≥ 1) *and* a separate GitHub identity for the agent, since an author cannot approve
   their own PR.
2. **Slack app**: Event Subscriptions → *Subscribe to events on behalf of users* must include
   `message.channels` (and `message.groups` for a private channel). On Solo it already does —
   Solo just ignores every other channel unless you are @mentioned — so no reinstall is needed.
3. **Invite the bot** in the request channel: `/invite @Solo`. Without it the bot cannot answer in
   the thread, and the owner gets a ⚠️ in the console instead.
4. **Node `.env`** (back it up first):
   ```
   ENG_CHANNEL=C0C3J97SZ8D            # #pop-priorities; never write the '#', it reads as a comment
   ENG_REPO=StaffAI-Inc/staffai
   ENG_MATCH=^\*?SR-\d+
   ENG_ROUTES=U0AKNHXJQ73=agent:barada
   ENG_LABEL=agent:barada
   ```
   Replies in a filed SR's thread are copied onto the issue as comments; the pickup waits
   `ENG_SETTLE_MINUTES` (15) after filing so the first ones are there when the agent reads it.
   then `solo restart`. Verify: `solo logs` shows no `ENG_REPO is not set` warning, and a test
   message in the channel logs `eng: filed #N` and gets a bot reply with the link.
   To try the pipeline without leaving tickets behind, put `[test]` anywhere in the SR
   (`ENG_TEST_MATCH`). It is filed and worked like any other but labelled `sr:test`
   (`ENG_TEST_LABEL`), so `-label:sr:test` filters it out, and the pickup closes the issue and its
   draft PR right after reporting. The branch stays; both reopen with one click.
   **SRs for anyone: the people table.** With `~/.slack-autoreply/eng.json`, every SR in the
   channel is filed, whoever it is for, and `ENG_ROUTES` no longer applies:
   ```json
   {"linear_team": "ENG",
    "people": {"U0AKNHXJQ73": {"name": "Barada Sahu", "github": "mercurialsolo",
                               "linear": "<Linear user id>", "machine": "mini"}}}
   ```
   For each SR, Solo opens the Linear issue in `linear_team`. If an issue already names that SR
   in its title, Solo adds the thread to it instead. Solo then opens the GitHub issue. Both are
   assigned to the person; in Linear that needs a Linear user, otherwise the description says who
   it is for. Both carry `agent:<machine>` when the person has one, which is the label Linear
   already uses. The GitHub body carries `Linear:` and `Branch:` lines, and the pickup works on
   that branch, so Linear's GitHub app attaches the PR. Slack exposes no emails here, so the table
   is the only join between Slack, GitHub and Linear. The owner confirms it, because filing
   assigns and notifies real people.

   **Any machine can pull.** A machine works the SRs labelled for it: it runs the pickup with
   `ENG_LABEL=agent:<machine>` from cron, and needs only `gh` and `claude` logged in. Solo watches
   the issues it filed and reports in the Slack thread itself once one is labelled `agent:done` or
   `agent:needs-human`, so the machine needs no Slack token. With `LINEAR_API_KEY`, for example
   from Infisical, the pickup also comments on the Linear issue; without it, Linear still
   attaches the PR by branch name.

5. **Cron on the node**, once a filed issue exists to try it on:
   ```
   */10 * * * * PATH=$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin ENG_LABEL=agent:mini ~/.slack-autoreply/venv/bin/python3 ~/.claude/skills/slack-autoreply/scripts/eng_pickup.py >> ~/.slack-autoreply/eng_pickup.log 2>&1
   ```
   For Linear, add `ENG_LINEAR_TEAM=ENG` (in `.env` or at the front of the cron line) and write
   `~/.slack-autoreply/eng-mcp.json` with the Linear MCP server (see `.env.example`). Passing it
   explicitly is what lets the agent's clone reuse the Linear login the owner's checkout already
   has — a project-scoped MCP is not visible from `~/agent-work`. Check from there:
   `claude -p "list the Linear teams" --mcp-config ~/.slack-autoreply/eng-mcp.json --allowedTools mcp__linear-server__list_teams`
   Verify by running it once by hand: the issue gains `agent:working`, then `agent:done` with a
   draft PR or `agent:needs-human` with a comment, and the bot posts in the thread either way.

The agent works in `ENG_REPO_DIR` (default `~/agent-work/<repo>`), never in a checkout a person
uses. `ENG_MAX_TURNS` (150) and `ENG_TIMEOUT` (5400 s) cap one run. To stop pickups, remove the
cron line; to stop filing, unset `ENG_CHANNEL` and `solo restart`.

## If something breaks

| symptom | cause → fix |
|---|---|
| `doctor`: `SLACK_USER_TOKEN … that's a bot token` | they copied `xoxb-`; need the *User* OAuth Token (`xoxp-`) |
| `doctor`: `missing user scopes` | app was created by hand, not from the manifest; re-create from `manifest.yaml`, reinstall, paste new token |
| `doctor`: `claude -p failed` | not logged in, or onboarding incomplete: run `claude` interactively once; for unattended boxes `claude setup-token` |
| `listen` logs `missing_scope` | same as missing scopes above |
| Solo DM says `⚠️ Solo cannot draft` | the node's writer is dead — almost always logged out of Claude Code. Run `claude /login` on the node (it is interactive; ssh in with `-t`). `solo status` shows the same state as a `writer:` line, and `solo doctor` re-tests it. Nothing is lost while it is down; pending drafts still send on ✅ |
| `listen` logs `Claude returned no decision` | run `claude "/slack-autoreply run"` interactively to see the error; usually a timeout (`CLAUDE_TIMEOUT_SECONDS`) or `CONTEXT_DIRS` pointing at a missing folder |
| service runs but nothing happens on a DM | `ONLY_WHEN_AWAY=true` while owner is active, quiet hours, or sender in `BLOCK_USERS`; check the log line `skip (…)` |
| `search --with` returns nothing | Slack search modifier differences; `history @handle` covers the same ground |
| drafts never appear | self-DM id changed (rare) — restart the daemon; or `MODE=auto` and the sender had earned auto |

Report back with: `doctor` output, the first daemon log lines, and which phase you stopped in.
