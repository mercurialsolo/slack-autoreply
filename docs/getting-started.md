# Get Solo running: hand this to your coding assistant

Solo makes your logged-in Claude Code answer your Slack messages from your own account, in your
voice. Drafts wait for your ✅ until a sender has earned auto. Nothing is shared: one Slack app,
one daemon, one Claude login, all yours.

## What you need

- A Slack workspace where you can create an app, or an admin who will approve one.
- A Mac or Linux machine that stays on while you want cover. A laptop that sleeps is a Solo that
  misses messages.
- A Claude subscription. Replies run on your Claude Code login, not an API key.
- About 30 minutes, 15 of them with you present to train the voice.

## The instruction

Paste this into Claude Code, or into any coding assistant that can run commands on that machine:

```
Set up Solo for me from https://github.com/mercurialsolo/slack-autoreply.

1. If `claude` is not installed, install Claude Code (https://code.claude.com/docs) and tell me to
   run `claude` once and `/login`; wait until I confirm.
2. If you are Claude Code: run `/plugin marketplace add mercurialsolo/slack-autoreply`, then
   `/plugin install solo@solo`, then `/solo:deploy`.
   If you are not Claude Code: `git clone https://github.com/mercurialsolo/slack-autoreply`,
   then follow its AGENTS.md from Phase 0.
3. Follow AGENTS.md phase by phase. Every phase ends with a verification command; do not move on
   while one fails. Stop and ask me for: the two Slack tokens, which folders Claude may read, and
   the three voice questions. Never put tokens anywhere but ~/.slack-autoreply/.env, and never
   send anything to Slack during setup.
4. Finish by reporting `doctor` output and the first lines of the daemon log.
```

## What happens, in order

| Phase | Who | What |
|---|---|---|
| 0 | assistant, you | Checks Python 3.10+ and Claude Code. You log in once; that part is interactive and cannot be done for you. |
| 1 | assistant | Installs the plugin and its daemon, creates `~/.slack-autoreply`. |
| 2 | you | Create the Slack app from `manifest.yaml`, copy two tokens. The assistant tells you where to click. |
| 3 | you, assistant | Paste the tokens and your folders into `.env`. `doctor` goes green. |
| 4 | assistant | A dry run. Nothing reaches Slack. |
| 5 | you, assistant | Voice training: it reads your last 200 messages, drafts your voice guide, asks you three questions. |
| 6 | assistant | Starts the daemon as a service and watches one real message go draft → ✅ → sent as you. |

## Afterwards

- Drafts arrive in a Slack DM from the Solo bot. ✅ sends, ❌ drops, or reply yourself and it learns.
- Weekly: `claude "/solo:slack-autoreply review"`.
- Stop it: `deploy/install-service.sh --remove` from the plugin directory, or `slackctl pause`.
- Update: `claude plugin update solo@solo`, then `/solo:deploy` again so the service follows.

The full runbook is [AGENTS.md](../AGENTS.md); what the system does and why is the [README](../README.md).
