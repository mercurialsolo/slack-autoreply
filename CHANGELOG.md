# Changelog

## 1.0.0 — 2026-10-13

First packaged release.

- Claude Code skill (`/slack-autoreply setup | handle | run | review | simulate | status`)
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
