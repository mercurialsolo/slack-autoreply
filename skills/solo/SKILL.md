---
name: solo
description: Report finished work or a result to the owner's team through Solo, which writes it in the owner's voice and decides where it goes. Use when work completes that someone is actually waiting on, when you are blocked and need a person, or when the owner told you to let someone know. Do NOT use to narrate progress, to broadcast steps, or for anything nobody asked for.
---

# Reporting through Solo

You are a coding agent. You have no voice on Slack and must not invent one — the owner has one
Slack presence and **Solo** is the only thing that writes as them.

Your job is to report **what happened**. Solo's job is everything after that: how it is worded,
who hears it, which channel it belongs in, and whether it goes out at all.

```
solo say "the overnight warmup finished — 4 accounts active, 2 still pending verification" --from scout
solo say "blocked on the Adair import: the work order sheet has no zip column" --from builder
```

That is the whole interface. **Do not name a channel or a person.** You do not have the channel
map and should not grow one — an agent choosing who hears what is how reporting quietly turns into
deciding. Solo has learned what each channel is for and routes it; if it cannot place something
confidently it asks the owner rather than guessing.

`--from` is you — your agent or task name. It appears on the draft so the owner knows who asked.

## What makes a good report

Facts you have actually verified, in plain terms, with the thing that matters first:

- **What happened**, concretely. Numbers you have checked, not estimates.
- **What it means** for whoever reads it — done, blocked, needs a decision.
- **What you need back**, if anything, and from whom in general terms ("needs someone with
  Facebook admin"), never a named routing instruction.

Write it as a brief, not as a message. Do not write finished prose: your register is not the
owner's, and Solo rewriting an agent-shaped message into the owner's voice is worse than Solo
writing it from your facts. No greeting, no sign-off, no "just wanted to flag".

## When not to report, which is most of the time

Default to silence. A Slack message interrupts a person; a commit does not.

Report when someone is genuinely **waiting**: they asked to be told, they are blocked until it
lands, or the owner said to tell them. Report when you are blocked and only a person can clear it.

Do not report:
- progress, or one step of several — wait until the thing is finished
- anything nobody asked for, however interesting
- what belongs in a commit message, a PR, or the wiki's `learnings/`
- a second message on a topic you already reported — batch it, or wait until it is settled

One report per finished thing. If you are writing a second, you are narrating.

## What never to hand over

Credentials or one-time codes. A date or commitment the owner has not made. A number you have not
verified. Anything about another client, another team, or work outside this workspace. Solo will
escalate these rather than send them, but do not make it the last line of defence — you asking
does not make a thing allowed.

## What happens next

Solo writes it in the owner's voice, places it, and **waits for the owner's ✅**. An
agent-requested message can never send on its own, whatever trust exists for that recipient. So
your report is a proposal, and the owner is the one who sends.

You get no delivery receipt. Do not wait for one, and do not re-send.

## If `solo` is not on PATH

It is normally symlinked to `~/.local/bin/solo` from the slack-autoreply repo. If it is missing,
tell the owner — do not look for another way to put a message into Slack, because there isn't one
that keeps their voice.
