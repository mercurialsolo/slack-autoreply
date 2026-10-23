---
name: deploy
description: Install, configure and start Solo on this machine by following its deployment runbook phase by phase. Use when the owner says "set up Solo", "deploy Solo", "install slack-autoreply", or after `claude plugin install solo`.
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/install.sh:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/slackctl.py:*), Bash(${CLAUDE_PLUGIN_ROOT}/deploy/install-service.sh:*), Bash(python3 ${CLAUDE_PLUGIN_ROOT}/tests/test_offline.py:*), Read
---

# Deploy Solo

The plugin is installed; nothing runs yet. Your job is to put the daemon in place on this machine,
following the runbook at `${CLAUDE_PLUGIN_ROOT}/AGENTS.md` **phase by phase**. Every phase there
ends with a verification command and the output that means "done". Do not skip one, do not move
on while one fails, and never send anything to Slack during setup.

Read `${CLAUDE_PLUGIN_ROOT}/AGENTS.md` now and start at Phase 0. Where it says `./install.sh`,
run `${CLAUDE_PLUGIN_ROOT}/install.sh`; where it says `slackctl`, that is
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/slackctl.py`. Tokens go only in `~/.slack-autoreply/.env`,
and the owner pastes them there themselves.

The daemon runs from this plugin's directory, so after `claude plugin update solo` run this skill
again: it re-points the service at the new version and restarts it.
