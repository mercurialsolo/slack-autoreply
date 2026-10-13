PY ?= python3
SKILL := $(HOME)/.claude/skills/slack-autoreply
CTL := $(PY) $(SKILL)/scripts/slackctl.py

.PHONY: install doctor test setup listen service service-status service-remove status review-data logs

install:        ## copy skill to ~/.claude/skills, create ~/.slack-autoreply, install deps
	./install.sh

doctor:         ## preflight: tokens, scopes, claude login, skill, context dirs, Jev
	$(CTL) doctor

test:           ## offline test suite (fake Slack, fake claude) — no tokens needed
	$(PY) tests/test_offline.py

setup:          ## interactive voice training in Claude Code
	cd $(HOME) && claude "/slack-autoreply setup"

listen:         ## run the daemon in the foreground
	$(CTL) listen

service:        ## install + start as launchd/systemd service
	deploy/install-service.sh

service-status:
	deploy/install-service.sh --status

service-remove:
	deploy/install-service.sh --remove

status:
	$(CTL) status

review-data:
	$(CTL) review-data

logs:
	tail -f $(HOME)/.slack-autoreply/listen.log 2>/dev/null || journalctl --user -u slack-autoreply -f

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'
