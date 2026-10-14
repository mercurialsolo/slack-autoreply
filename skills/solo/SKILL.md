---
name: solo
description: Tell a colleague something on Slack, in the owner's voice, via Solo. Use when work finishes that a specific person is actually waiting on, when something is blocked by someone else, or when the owner asks you to let someone know. Also use to check who is waiting (`solo followups`) or what Solo has queued. Do NOT use to broadcast progress, to narrate what you are doing, or to message anyone who has not asked for it.
---

# Telling someone, through Solo

You are a coding agent. You do not have a voice on Slack and must not invent one — the owner
has exactly one Slack presence and **Solo** is the only thing that writes as them. When a person
genuinely needs to hear something, hand Solo the *intent* and it writes the message.

```
solo say @sri "the overnight warmup finished — 4 accounts active, 2 still pending" --from <you>
solo say '#adair-homes' "Thursday's E2E run is clean" --from <you> --why "gate for go-live"
```

`--from` is who is asking (your agent or task name). It appears on the draft so the owner knows
who wanted it sent.

## The judgement call, which is the hard part

Default to **not** messaging. A Slack message interrupts a person; a commit does not. Ask:

**Is a specific named person actually waiting on this?** Not "would someone find it interesting" —
waiting. They asked, or they are blocked until it lands, or the owner said to tell them. If you
cannot name the person and why they are waiting, do not send.

Send when:
- Someone asked to be told when a thing was done, and it is done.
- You are blocked and only a particular person can unblock you — say exactly what you need.
- The owner told you to let someone know.
- Something you changed affects work a named person has in flight right now.

Do not send when:
- You just want to report progress, or the step is one of many.
- Nobody asked. Interesting ≠ awaited.
- It belongs in the repo or the wiki — a commit message, a `learnings/` entry, a PR description.
- You are unsure who it is for. Ask the owner instead of picking someone.
- It is the third message on one topic. Batch it, or say nothing until it is settled.

One message per finished thing, to one person. If you are composing a second, you are narrating.

## What to hand over, and what not to

Give the **intent**: what happened, what it means for them, what you need back if anything. Give
facts you have verified. Do not write finished prose — Solo reads the recipient's dossier and the
owner's voice guide and writes it; your register is not the owner's, and a message that reads like
an agent wrote it defeats the point.

Never hand Solo: a credential or one-time code, a commitment to a date the owner has not made, a
number you have not verified, anything about another client, another team, or work outside this
workspace. Solo will escalate those rather than send them, but do not make it the last line of
defence — an agent asking does not make a thing allowed.

## What happens after you call it

Solo composes in the owner's voice, then it **waits for the owner's ✅**. An agent-requested
message can never auto-send, whatever the trust ledger says, so your call is a proposal and the
owner is the one who sends. That is also why a borderline "should I?" is cheap to get wrong in the
direction of asking — but noise still costs the owner a decision, so do not lean on it.

You will not get a delivery receipt. Do not wait for one, and do not re-send.

## Finding out who, and what is outstanding

```
solo followups          promises the owner has made, and to whom — often the real audience
solo about @who         who they are, what you work on with them, how the owner talks to them
solo drafts             what is already queued for approval (avoid duplicating it)
solo status             is Solo running, what mode, what is waiting
```

`solo about` before messaging someone you have not messaged before: it tells you whether they are
internal, a customer, or a partner, which changes what may be said at all.

## If `solo` is not on PATH

It lives with the slack-autoreply repo and is normally symlinked into `~/.local/bin/solo`. Without
it, the same commands exist on the machine running the daemon via
`python3 ~/.claude/skills/slack-autoreply/scripts/slackctl.py …`. If neither is reachable, say so
to the owner rather than finding another way to send Slack messages — there isn't one that keeps
their voice.
