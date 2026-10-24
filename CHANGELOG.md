# Changelog

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
