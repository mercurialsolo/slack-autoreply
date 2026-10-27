# Changelog

## 1.3.0 — 2026-10-27

**The owner's desktop: `attention/attn.py`**
- A new primitive for asks that cannot wait to be read: a one-time code a login is waiting on,
  a decision that blocks everything behind it. The sender chooses the priority and the words;
  the daemon on the owner's machine decides what that looks like. `p0` is a modal on top of
  everything with the screen blurred, a countdown to the deadline, a sound until answered, and
  it comes back if closed — with a "Can't right now" button always, recorded as `declined`.
  `p1` is on top with a countdown and is shown again every few minutes. `p2` is one banner.
- `attn ask … --wait` returns the owner's answer (text or a choice) as JSON; exit codes say
  answered / declined / expired / cancelled / unreachable. Unreachable falls through
  `ATTN_FALLBACK_CMD` so a closed laptop still tells someone. `attn cancel` takes an ask off the
  screen when the sender no longer needs it. Open asks survive a daemon restart.
- HTTP over the tailnet with a bearer token; asks are 0600 files, the log never carries an answer.
  swiftDialog when installed, AppleScript dialogs otherwise. `attention/install-desktop.sh`
  installs the LaunchAgent on the owner's machine; `attention/README.md` has the rest.
- Solo raises a `p1` for an escalation whose triage urgency reaches `ATTN_URGENCY` (4.5 — 5 of the last 47 escalations; 4.0 would have been 16) and
  cancels it when the owner ✅/❌s the draft or it expires. `slackctl attn "<title>" …` (and
  `solo attn …` from the laptop) lets any agent on the node interrupt the owner through Solo's
  config, with `ATTN_URL`/`ATTN_TOKEN` in `~/.slack-autoreply/.env` and nowhere else. The writer
  may not call it: `attn` is on the dispatch-forbidden list.

## 1.2.2 — 2026-10-27

**A `say` the writer cannot compose still reaches the owner**
- Four release notes from the coordinator went missing on 27 Oct: the account `say` runs under
  hit its weekly usage limit at 10:58, every compose came back "You've hit your weekly limit" in
  one second, and each ended in an exit code only the calling agent saw. Now the item is marked
  `failed` with the writer's own reason, and the agent's wording is posted to the console
  (`⚠️ Solo could not write a message from …`) so the owner reads it anyway and can place it,
  rewrite it or drop it. Nothing goes to anyone else. `--re` reports still record `report_failed`.
- A run in which the writer never answered — no `act` — no longer leaves a session to resume.
  The refused prompt sat in the shared owner-DM transcript; the next successful run resumed it,
  "caught up" on four stale prompts in one go, wrote decisions no process was waiting for, and
  told the owner all four had been delivered. The session record is dropped, so the next run in
  that conversation starts clean.
- `act` under dispatch refuses an item that is already `done` or `failed`: a prompt left over in
  an old transcript is not work to do, and the writer is told to say so and stop.
- One writer run per conversation at a time, across processes: a file lock under
  `~/.slack-autoreply/locks/`. Two `say`s resumed one session five seconds apart and the second
  loaded the transcript while the first was mid-tool-call. The daemon's in-memory per-channel
  lock could not see a `say` process.
- The fresh-session retry after a failed resume now carries the compose prompt; it used to fall
  back to `handle`.

## 1.2.1 — 2026-10-25

**A message to the owner is delivered, not drafted**
- An agent's `say` addressed to the owner (`--to @owner`) used to wait in the console for the
  owner to ✅ a message to themselves. Their own DM has nobody else in it, so every gate was
  protecting an empty room. It is now posted to the console as a message from the agent
  (`📨 agent → you`), with a file when one is attached, and nothing to react to. The owner asked
  for exactly this on 2026-10-24. A failed delivery (a scope, a moved file) still falls back to a
  draft that says why. Everything addressed to anyone else drafts as before.

## 1.2.0 — 2026-10-23

**Files go out with a draft**
- `slackctl say … --file <path>` (repeatable) attaches a video, document or image to what an agent
  asks Solo to say. The draft in the console names the file and its size; ✅ uploads it with the
  message in one `files.upload` call, so a failed upload sends nothing rather than words without
  the file. The writer sees an `attached:` line in `show` and writes the words to go with it; it
  cannot attach, drop or read files itself. A draft that carries a file never auto-sends.
- `solo say --file` copies a local file into the node's `~/.slack-autoreply/outbox/` first, since
  the daemon on the node is what uploads it. Paths under dot-directories are refused, and the
  file must still exist when the owner approves — a missing file or a failed upload leaves the
  draft pending with the reason in the console.
- Needs the `files:write` user scope: added to `manifest.yaml`; `doctor` warns (not fails) when an
  existing app lacks it. Add the scope under OAuth & Permissions and reinstall the app.
- `doctor` reads the granted scopes from `auth.test`'s `x-oauth-scopes` header. It used to read a
  body field Slack leaves empty and then report 13 of 14 scopes missing on a working install.

## 1.1.0 — 2026-10-23

Packaged for other people's workspaces. Everything below shipped between 1.0.0 and this tag.

**Packaging**
- Solo is a Claude Code plugin, and the repo is its own marketplace:
  `/plugin install solo --marketplace mercurialsolo/slack-autoreply`, then `/solo:deploy` follows
  AGENTS.md phase by phase. The writer's skill is `/solo:slack-autoreply` (was `/slack-autoreply`);
  the daemon runs from the plugin root, and `doctor` checks that the plugin Claude Code loads is
  the directory the service runs from. From a clone, `install.sh` installs the same plugin under
  `~/.claude/skills/solo`. **Upgrading from 1.0.x:** re-run `install.sh`, then
  `deploy/install-service.sh`, then remove `~/.claude/skills/slack-autoreply`; set
  `SKILL_COMMAND=/slack-autoreply` in `.env` only for the gap between the two.
- MIT license. Real workspace, channel, user, repo and customer identifiers replaced with
  placeholders throughout docs, examples and tests. `deploy/solo` no longer assumes the author's
  machine: it reads `~/.slack-autoreply/solo.env` (`SOLO_NODE`, `SOLO_REMOTE_HOME`,
  `SOLO_REMOTE_REPO`, `SOLO_WIKI`, `SOLO_REMOTE_WIKI`) and refuses with a clear message when one
  is missing. `tools/voice_eval.py pairs` takes `--targets`.
- `install.sh` gives Solo its own venv (`~/.slack-autoreply/venv`) so a system Python upgrade
  cannot take the daemon down, and installs the `solo` skill for Codex too when present.
- README gained a Requirements block and groups the node, wiki, agent and engineering sections
  under one optional heading.

**Console and approvals**
- A notify-only bot user (same app) DMs you that a draft is waiting; Slack never badges your own
  messages. Approvals moved into that bot DM, which also accepts typed commands (`pause`,
  `status`, corrections) from a phone. `NOTIFY_CMD` for an out-of-Slack ping.
- `pause [--for 2h]` / `resume` take effect without a restart. `status` says whether the writer
  can draft right now, and a dead or logged-out writer announces itself.
- A draft can send as two messages (`⏎` is a message boundary). Solo picks the reaction emoji and
  can choose to answer in a thread; once in a thread it stays in it without a fresh @mention.
- Mentions are put back together on the way out so `@Name` actually pings.

**Routing**
- The routing gates are dials (`TRIAGE_MAX_EXECUTE`, `TRIAGE_MIN_ANSWERABLE`,
  `TRIAGE_MIN_IN_SCOPE`, `TRIAGE_SHORT_MESSAGE`); `tools/threshold_sweep.py` replays them on
  already-scored messages. A direct @mention can no longer be answered with silence; a
  thank-you is not a rescue; a short message's action score is not forced to you.
- Learning now also records the owner fixing their *own* messages, and `REVIEW_NUDGE_AFTER`
  reminds you to run `review` once corrections pile up.
- Sessions resume for one hour (the prompt-cache TTL), and every run is charged what it cost.

**Other agents, one voice**
- `say` / `compose`: any agent can ask Solo to tell someone something, in your voice, always
  gated by your ✅. `ask`: agents read Slack in plain language, read-only. `learn-channels` and
  `where` learn what each channel is for. A global `solo` skill for any coding session.
- Solo can propose a read-only agent investigation (`--task`); you hand it over with 🤖 or
  `DELEGATE_MODE=auto` does. Reports come back to the conversation that asked, are shown to the
  writer so it stops re-asking, and a report Solo will not place still reaches you.

**Engineering requests (optional, AGENTS.md Phase 9)**
- One channel where the team asks for code changes. Each SR is filed in Linear and on GitHub,
  assigned to its person and labelled for their machine; `eng_pickup.py` hands it to Claude Code
  in a fresh worktree, which opens a draft PR; the bot reports in the thread. `[test]` SRs clean
  up after themselves; `eng-replay` and `eng-sync` recover from misses.

**Secrets**
- Optional: read Solo's keys from a team Infisical when the machine has an identity file;
  `.env` otherwise. Only Solo's own keys are taken, never a whole folder.

## 1.0.0 — 2026-10-13

First packaged release.

- Claude Code skill (`/solo:slack-autoreply setup | handle | run | review | simulate | status`)
  with `slackctl` daemon: Socket Mode listener on the owner's user token, replies posted from
  their own account.
- Jev (TypeSafe AI System One) triage with Claude Haiku fallback and policy thresholds;
  routes `ignore / ack / hold / reply / escalate`.
- Voice: samples pulled from the owner's own Slack history and kept fresh; VOICE.md drafted
  by Claude Code during `setup`.
- Memory: person/channel dossiers, on-demand `history / search / about`, one Claude Code
  session per conversation (`--session-id` / `--resume`).
- Learning loop: outcomes (approved / dropped / replaced / corrected / deleted / missed /
  clean), correction pairs, per-(route, sender) trust that graduates draft → auto in
  `MODE=learn`; `review` mode folds patterns into VOICE.md.
- Time-bound holds with tracked follow-ups and self-DM reminders.
- Share gate: context-backed replies to untrusted senders always go through the owner.
- Packaging: `install.sh`, `slackctl doctor`, launchd/systemd units with installer,
  offline test suite (42 checks), `AGENTS.md` runbook for coding agents.
