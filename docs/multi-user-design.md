# Solo for a team: design

Status: proposal, 2026-10-13. Nothing here is implemented. No file under `scripts/` was changed.

Marker convention. `[verified: file:function]` means I read the code and it does this today.
`[inferred]` means a claim about Slack, Claude Code or macOS that I did not verify in this session;
each one is listed again in the appendix with how to check it. Facts the team measured this session
(app id, token types, no bot user, `AUTOREPLY_HOME`, mode read at startup) are taken as given.

## 0. The recommendation in one paragraph

Keep Solo a strictly per-person stack: one Slack app per person (created from the same manifest), one
daemon per person on that person's own machine, one `~/.slack-autoreply` per person, drafting on that
person's own Claude Code login. Nothing central. The shared thing is the code. The reason is not taste:
a single shared Slack app with Socket Mode delivers events per app, not per user, so N laptops holding
the same `xapp-` token cannot each receive only their owner's messages, and drafting cannot move off the
person's machine without moving their Claude login, their files and their MCP credentials with it. The
four features the team asked for (own Solo, trainable without a babysitter, shadow mode, pause) are all
small additions to the existing daemon, ledger and CLI; the one genuinely new piece is a `control.json`
the daemon re-reads at runtime. Ship pause and runtime mode first, shadow second, self-serve onboarding
third. A shared app with a relay, and per-person always-on boxes, are kept as documented escape hatches
for when the Slack admin refuses N apps or when someone needs cover while their laptop is closed.

## 1. What is true today

All of this is in the code as of 1.0.0.

**One person, one process, one directory.**
- Every path is under `STATE_DIR = $AUTOREPLY_HOME or ~/.slack-autoreply` `[verified: config.py top level]`. `install.sh` creates it with `chmod 700` and `.env` with `chmod 600`.
- The daemon is `Daemon(Slack)` `[verified: slackctl.py:Daemon]`. It opens one Socket Mode connection with `SLACK_APP_TOKEN` and makes every Web API call with `SLACK_USER_TOKEN` `[verified: slackctl.py:Daemon.run, Slack.__init__]`. `auth.test` tells it who it is (`self.me`); every guard and every learning hook compares event user ids to `self.me`.
- The Socket Mode listener acknowledges, dedups on `event_id`, and hands `payload["event"]` to `on_event`. It discards `payload["authorizations"]` and `payload["team_id"]` `[verified: slackctl.py:Daemon.run listener]`. For one person with their own app that is harmless.

**Mode and policy.**
- `MODE` is read into `CFG["mode"]` once at import `[verified: config.py:CFG]` and passed as `base_mode` at three call sites: `policy_preview`, `execute_decision` (twice: simulated branch and real branch) and `Ledger.scorecard`. Allow/block/share lists are resolved once in `Daemon.__init__`. There is no runtime switch of any kind; changing anything means editing `.env` and restarting the service.
- `Ledger.effective_mode(route, sender, channel, base_mode)` returns `draft` for `escalate`, `auto` for `ack` and `ignore`, then `base_mode` if it is `auto` or `draft`, then the trust rules for `learn` `[verified: learning.py:Ledger.effective_mode]`. Note the order: `ack`/`ignore` return before `base_mode` is consulted. That matters for shadow (section 5).
- `execute_decision` already contains a complete "compute what would happen, touch nothing" branch for simulated items `[verified: slackctl.py:execute_decision, the `item.get("simulated")` block]`. Shadow is that branch applied to real items.

**Learning.**
- Outcomes are rows in `outcomes.jsonl` with `good` in {True, False, None} `[verified: learning.py:Ledger.record]`. `GOOD = {approved, clean}`, `BAD = {dropped, replaced, corrected, deleted, missed}`; `expired` and `followed_up` are neutral. Trust is computed from `GOOD`/`BAD` only.
- The comparison hook exists: `Daemon.on_owner_message` finds a pending draft for the same channel and thread when the owner replies themselves, records `replaced` with a correction pair `{draft, actual}`, and separately records `missed` when the owner answers something triage ignored within 2 h `[verified: slackctl.py:Daemon.on_owner_message]`. Both are keyed off `state.json`: `pending` is keyed by the self-DM `draft_ts`, `ignored` by channel.
- Stand-down: after the 20–90 s delay, `on_inbound` re-reads the conversation and gives up if the owner already replied `[verified: slackctl.py:Daemon.on_inbound]`. Right for live modes, wrong for shadow.
- Cost per run is captured: `item["run"]["cost_usd"]` from the `result` event of `claude -p --output-format stream-json` `[verified: slackctl.py:Daemon.handle_with_claude]`. Nothing aggregates it yet.

**Drafting.**
- `claude_command` builds the invocation: `--permission-mode dontAsk`, `--allowedTools` with `Read(//<dir>/**)` per `CONTEXT_DIRS`, `mcp__<name>__*` for servers found in `~/.claude.json` and `<cwd>/.mcp.json`, `--add-dir` per dir, `ANTHROPIC_API_KEY` popped from the environment unless `DISPATCH_USE_API_KEY` `[verified: slackctl.py:claude_command, discover_mcp_names]`. One session per conversation via `--session-id`/`--resume`, bookkept in `state.json["sessions"]`.
- The cwd for `claude -p` is `CLAUDE_CWD` or the first context dir `[verified: claude_command]`, so the person's project `CLAUDE.md` and `.mcp.json` apply.

**Onboarding** is AGENTS.md Phases 0–7: a human creates the app from the manifest, generates the `xapp-` token, clicks "Install to Workspace" for the `xoxp-` token, pastes both into `.env`, runs `doctor`, runs `simulate`, then sits with Claude Code for `setup` (voice, three questions, dossiers, three dry runs), then `listen` and `make service`.

## 2. What blocks multi-user before anything can be built on

In order of severity. The first two are real blockers; the rest are inconveniences that become bugs at N > 1.

1. **A shared Slack app cannot feed N per-person daemons over Socket Mode.** Events are delivered per app. An event's `authorizations` array lists one installing user even when several are entitled to it; the full list requires `apps.event.authorizations.list` (confirmed by the Slack docs for the `presence_change` payload, which explicitly say so). With several Socket Mode connections open on the same app, Slack hands each event to one of them `[VERIFIED 2026-10-13 against docs.slack.dev/apis/events-api/using-socket-mode: "When multiple connections are active, each payload may be sent to _any_ of the connections. It's best not to assume any particular pattern for how payloads will be distributed across multiple open connections." Limit: "up to 10" concurrent connections per app]`. So with the current app (`A0XXXXXXXXX`) installed by five people and five laptops running `listen`, Priya's DM can arrive at Barada's daemon, which will discard it (`sender == self.me` fails, `on_inbound` runs guards against the wrong owner) or, worse, treat it as an inbound to Barada. The daemon's listener does not even look at `authorizations`. Verdict: **either one app per person, or one listener per app that routes.** There is no third option on the Slack side.
2. **Drafting cannot be centralised without moving the person.** `claude -p` needs the person's Claude Code login (OAuth state under `~/.claude`), their `CONTEXT_DIRS` on local disk, and their MCP servers from `~/.claude.json` (including OAuth-backed connectors). A "central dispatcher" is only a router in front of N execution environments; it does not remove the need for them.
3. **`MODE` is a startup constant.** Pause, shadow, per-sender graduation all need the daemon to read something at runtime. Small fix (section 6), but everything else depends on it.
4. **Installing a shared app as a second user needs an OAuth redirect.** "Install to Workspace" on api.slack.com is for the app's collaborators. Anyone else must go through `https://slack.com/oauth/v2/authorize?client_id=…&user_scope=…` → redirect URL → `oauth.v2.access` with the `client_secret` `[verified in Slack docs: AuthorizeUrlGenerator; App Distribution note]`. Today there is no redirect endpoint and the manifest has no `redirect_urls`. Per-person apps avoid this entirely; that is one of the reasons to prefer them.
5. **The listener trusts the stream.** No assertion that `authorizations[0].user_id == self.me`. One line of defence worth adding even with per-person apps.
6. **Default `MODE=learn` means a fresh install posts drafts to the self-DM immediately** and, after five approvals, sends on its own. For a teammate who has never seen Solo, the default should be `shadow`.

## 3. Two axes, resolved separately

### 3a. Slack side: how events reach each person's Solo

| Option | How | Isolation | Infra | Admin | Verdict |
|---|---|---|---|---|---|
| **A. One app per person** (same manifest, name "Solo (handle)") | Each person creates their app, gets their own `xapp-` and `xoxp-`; their daemon is the sole Socket Mode client of their app | Complete. One person's `xapp-` cannot see another's events. Rate-limit budgets are per app | None | N near-identical apps to approve if the workspace requires approval | **Recommended** |
| B. Shared app, N daemons on Socket Mode | Everyone pastes the same `xapp-` | Broken: events land on arbitrary daemons (blocker 1) | None | One app | Rejected: does not work |
| C. Shared app, one relay listener | One always-on process holds the `xapp-` (and no user tokens), resolves `apps.event.authorizations.list` per event, forwards each event to the right person's daemon over an authenticated per-user queue (long-poll or SSE) | The relay sees everyone's message text in transit. Must be stateless and log nothing. Needs an app-level scope beyond `connections:write` for `authorizations.list` `[inferred]` | A small service on an always-on box, plus a per-user secret; per-person daemons change transport from Socket Mode to the relay | One app | Escape hatch when the admin refuses N apps or N > ~10 |
| D. Shared app, Events API over HTTPS to a relay | Same as C without Socket Mode; needs a public URL (a Cloudflare Worker would do) | Same as C | Public endpoint, signing-secret verification | One app | Same as C; only if Socket Mode's 10-connection ceiling or relay uptime becomes the problem |

Why A over C: C adds a service that must be up for anyone's Solo to work, that transiently sees everyone's DMs, and that holds the one secret able to read the whole team's event stream. A has none of that and costs each person three clicks. The only argument for C is admin ergonomics, which is a question for the admin (section 11).

Note on app distribution: within one workspace, A needs nothing. Distribution is only required if someone in another workspace installs the same app; with A each workspace's people create their own. Leave `org_deploy_enabled: false`.

### 3b. Execution side: where `claude -p` runs

| Option | Who executes | Who pays | Files and MCP | Laptop sleeps | Blast radius | Verdict |
|---|---|---|---|---|---|---|
| **1. Daemon on the person's own laptop** (today) | The person's machine, their login | Their Claude plan | All there already | Solo is off while the lid is closed. Events during sleep are lost, not queued `[inferred: Socket Mode has no replay]` | Their own machine | **Recommended default.** Honest limit: covers "at my desk but heads-down", not "on holiday" |
| 2. Per-person always-on box (own Mac mini, or own macOS user on a shared mini) | A box the person owns or a user account only they can log into | Their plan via `claude setup-token` (long-lived OAuth for the subscription) `[inferred beyond README's mention]` | Must be mirrored: one-way sync of `CONTEXT_DIRS`; MCP servers re-added with `claude mcp add` and re-authorised there (claude.ai connectors with browser OAuth are the painful part) `[inferred]` | Covers absence | The box holds their Slack token, their Claude OAuth token and copies of their files. On a shared box, anyone with admin on it can read all of that | Offer as an opt-in for people who want absence cover. Shared box only with one OS user per person and no shared sudo |
| 3. Central dispatcher that shells out per person | A central box runs `claude -p` as each person | Either each person's `setup-token` stored centrally, or one team API key | Every person's files and MCP credentials copied to one machine | Covers absence | One machine holds everyone's Slack tokens, Claude tokens, files and connector OAuth. Whoever runs it can read all of it | **Rejected.** It is option 2 with all the isolation removed. Nothing about it is cheaper except the number of boxes |
| 4. Central, one team API key, no files/MCP ("Solo Lite") | Central box, `DISPATCH_USE_API_KEY=true`, empty `CONTEXT_DIRS`, no MCP | A team key, so costs are pooled and visible | None; replies come from Slack context + VOICE.md only. The share gate becomes moot | Covers absence | Everyone's `xoxp-` on one box | Rejected for now. It is the only design where "who pays" is the team, but it also removes the thing that makes replies substantive, and it still centralises every user token |

What is genuinely blocked versus merely inconvenient:
- Blocked: running a person's replies on someone else's login (wrong bill, wrong files, and Claude Code logins are not shareable). Blocked: drafting with that person's MCP connectors from a machine they have not authorised. Blocked: option B above.
- Inconvenient: laptop sleep (accept it, or move that person to option 2). Inconvenient: `PATH` for `claude` inside launchd (already handled by `install-service.sh`). Inconvenient: `claude setup-token` lifetime and rotation on an always-on box.

### 3c. Recommended architecture

```
per person (laptop, or their own always-on box)
┌──────────────────────────────────────────────────────────────────┐
│ Slack app "Solo (handle)"  xapp- (own)  xoxp- (own)              │
│        │ Socket Mode (one connection, one owner)                 │
│        ▼                                                         │
│ slackctl listen ── control.json (pause / mode / per-sender)      │
│        │  Jev triage (own or team TYPESAFE key, see §11)         │
│        ▼                                                         │
│ claude -p  (own login, own CONTEXT_DIRS, own MCP servers)        │
│        ▼                                                         │
│ ~/.slack-autoreply/  outcomes · corrections · shadow · dossiers  │
│        ▲                                                         │
│ self-DM = console (drafts, shadow comparisons, commands, digest) │
└──────────────────────────────────────────────────────────────────┘
shared: this repo, the manifest, a #solo channel for support
```

Nothing crosses the box boundary except Slack traffic on the person's own tokens and `claude -p` on the person's own login.

## 4. Pairing and onboarding

### What a new teammate does (target, after Phase 2)

1. `git clone … && ./install.sh` (unchanged). Prints `42 checks passed`.
2. `slackctl pair`. New command. It:
   - prints the manifest with `display_information.name` set to `Solo (<their handle>)` and opens `https://api.slack.com/apps?new_app=1&manifest_yaml=…` `[inferred: Slack supports a create-from-manifest deep link; if not, it prints the manifest and the URL and the person pastes]`;
   - prompts (hidden input) for the `xapp-` and `xoxp-` tokens, validates each (`apps.connections.open` for the app token, `auth.test` for the user token, checks it starts with `xoxp-` and has the manifest's user scopes), and writes `.env` at mode 600. The person never opens `.env`;
   - asks for `CONTEXT_DIRS`, offering candidates: the cwd's parent, `~/Projects`, `~/Documents`, plus any `projects` keys in `~/.claude.json` that exist on disk (reusing `discover_mcp_names`'s file reading) `[verified the file is already parsed: slackctl.py:discover_mcp_names]`;
   - writes `MODE=shadow`, runs `doctor`, then runs `build-voice` so the later `setup` session starts with samples already pulled (that is the slow, rate-limited step; doing it here takes it off the interactive clock);
   - ends with the exact next command.
3. `claude "/solo:slack-autoreply setup"`, ~10 min now that samples exist. The three questions (who to be careful with, what never to commit to, what is fine on autopilot) stay human; everything else in SKILL.md `setup` is already automated by the skill.
4. `make service`. The daemon starts in shadow. First self-DM from Solo: "Shadow mode: I'm watching how you reply and will show you what I'd have said. I post nothing to anyone. `status`, `pause`, `mode` work here."

Which of today's phases that removes: Phase 2 steps 2–3 (token generation stays a browser click; the paste, validate and store is automated), all of Phase 3, all of Phase 4 (`pair` runs `simulate` and shows the result), the `build-voice` half of Phase 5. Phases 0, 1, 6 are already commands. Phase 7's hand-over text becomes Solo's first self-DM.

### If the admin insists on one shared app

Then pairing must mint a per-user `xoxp-` through OAuth:
- Path (i): add the teammate as an app collaborator so "Install to Workspace" works for them. Collaborators see the client secret, signing secret and all app-level tokens `[inferred: collaborator permissions]`. Acceptable for three people who already share a laptop drawer; not for a team.
- Path (ii): a redirect endpoint. `slackctl pair --shared` opens the authorize URL with `user_scope=` from the manifest and a `state`; a tiny HTTPS endpoint (a Cloudflare Worker is enough; `cf` is already set up on this machine) exchanges the code with `oauth.v2.access` and shows the `xoxp-` once for the person to paste. Redirect URLs must be HTTPS `[inferred]`, which is why localhost alone does not work. The Worker holds the client secret; it never sees Slack messages. This is the same Worker the relay in 3a-C would need, so if you ever build C you get (ii) for free.
Either path still has blocker 1, so a shared app means building the relay. Do not do this for the first ten people.

## 5. Shadow mode

### Definition

Shadow is a fourth base mode: `MODE=shadow`. Solo triages every message, composes what it would have said, and waits to see what the person actually does. It compares, scores, and shows. Solo writes to Slack only in the person's self-DM, and only comparison notices and the daily digest. No reaction, no message, no draft reaches any other conversation. The `ack` route posts nothing. `ignore` is unchanged (it never posted anyway) and keeps feeding `missed`.

Decision on the self-DM: notices to self are not "posting" in the sense that matters (nobody else can see them), and the comparison surface has to live somewhere. Default `SHADOW_NOTIFY=each`; `digest` and `none` available. Nothing in shadow ever calls `Slack.post` or `Slack.react` on a channel other than `self_dm`; the offline test suite should assert that with the fake Slack.

### What it records

New outcome vocabulary in `learning.py`, kept out of `GOOD`/`BAD` so shadow can never graduate anything to `auto`:

| outcome | when | good |
|---|---|---|
| `shadow_match` | person replied; comparator says same substance (`same` or `close`) | True, but only for `shadow_stats()` |
| `shadow_miss` | person replied; comparator says `different` | False, same |
| `shadow_unscored` | person replied; no comparator available (no Jev key, or Jev error) | None; shown for a manual 👍/👎 |
| `shadow_unneeded` | Solo chose `reply`/`hold`, person never replied within `SHADOW_WINDOW_HOURS` (24) | None; shown in the digest as "would have replied where you stayed silent" |
| `missed` | existing: Solo chose `ignore`, person replied within 2 h | already False in `BAD`; in shadow it also counts for `shadow_stats()` |

Each scored row carries a correction pair `{draft: solo_text, actual: owner_text}` through the existing `correction=` argument of `Ledger.record`, so `review` sees shadow comparisons with no new code path. Add `shadow: true` and `verdict`/`overcommits` fields to the row.

`Ledger` additions:
- `SHADOW_GOOD = {shadow_match}`, `SHADOW_BAD = {shadow_miss, missed}`. Store shadow rows with `good=None` and a separate `shadow_good` field; add `Ledger.shadow_stats(route, sender)` that reads `shadow_good`. Keeps `effective_mode` arithmetic untouched.
- One more line, or shadow dirties the live record: `missed` is already in `BAD`, so a shadow-period miss would land with `good=False` and show up in `Ledger.stats(sender=…)`, which `render_item` prints as "track record with them" and `recall.about` reuses `[verified: slackctl.py:render_item]`. Rule: every row recorded while the item's base mode was shadow carries `shadow: true`; `Ledger.stats()` skips rows with `shadow: true`; `shadow_stats()` reads only those. A person entering `learn` then starts with a clean per-sender record, which is the point.
- `Ledger.effective_mode`: first line becomes `if base_mode == "shadow": return "shadow", "shadow: watching only"`. It must precede the `ack`/`ignore` branch, otherwise `execute_decision` will react 👀 `[verified: ordering in learning.py:Ledger.effective_mode]`.

### Mechanism, function by function

- `config.py`: accept `shadow` in `MODE`; new keys `SHADOW_NOTIFY`, `SHADOW_WINDOW_HOURS=24`, `SHADOW_SAMPLE=1.0`.
- `Daemon.on_inbound`: when the effective base mode is shadow, (a) capture the full `CONTEXT_MESSAGES` transcript before the delay and set `delay = 0`, (b) skip the "owner replied, stand down" branch, (c) do not refresh the transcript after the delay. Reason: the comparison needs Solo's draft to be written without seeing the person's answer; today's refresh after the delay would hand Claude the answer `[verified: slackctl.py:Daemon.on_inbound re-fetches transcript after the sleep]`. Residual leak: Claude may run `history` or `search` and find the owner's reply if it lands within the run; mitigate by recording `item["frozen_ts"]` and having `recall.history` filter messages newer than it when `AUTOREPLY_DISPATCH=1` and the item is shadow. Accept for Phase 1, fix in Phase 2.
- `execute_decision`: new branch after the simulated branch, modelled on it: compute `would` exactly as the simulated branch does but with `base_mode="learn"` (so the person is told "this would have been a draft" or "this would have gone out on its own, you've earned it"), store `item["shadow"] = {action, text, would, at}`, `item["status"] = "shadowed"`, write `state["shadow"][session_key(item)] = {item, at, ts}` (a parallel to `state["ignored"]`, keyed by conversation because shadow items have no `draft_ts`), audit, return. No `slack.*` call.
- `Daemon.on_owner_message`: after the pending-draft loop, look up `state["shadow"]` for this conversation and thread within the window. If found and the owner's text is usable, run the comparator, `ledger.record(item, outcome=…, correction={draft, actual})`, pop the entry, and if `SHADOW_NOTIFY=each` post one self-DM:
  ```
  Shadow · Priya in DM · reply (would have been a draft)
  > them: can you send me the latest deck?
  Solo:  it's in the shared drive under Q4 — sending the link now
  You:   drive link, sec
  verdict: close · Solo was longer and more formal
  (👍 / 👎 to overrule)
  ```
  The notice's `ts` goes into `state["shadow_notices"][ts] = item_id` so a 👍/👎 reaction in `on_reaction` flips the outcome to `shadow_match`/`shadow_miss` with `note="manual"`. This reuses the exact `on_reaction` → `state["pending"]` pattern `[verified: slackctl.py:Daemon.on_reaction]`.
- `Daemon.tick`: sweep `state["shadow"]` entries older than the window: record `shadow_unneeded` if Solo's action was reply/hold, otherwise drop silently. Post the daily digest at `DIGEST_HOUR` (section 7).
- `triage.py`: a second question set `compare_questions(me)` for Jev, same request shape as `questions()` `[verified: triage.py:questions]`:
  - `verdict` (choice): `same` (same substance and same commitment, wording aside), `close` (same direction, different detail or tone), `different` (different answer, different commitment, or different stance), `other`.
  - `solo_overcommits` (noul): does Solo's text promise something the owner's reply does not?
  - `tone_off` (noul): would the recipient notice it was not the owner?
  State: `{owner, owner_notes, latest_message, recent_conversation, solo_route, solo_text, owner_reply}`. A new `Triager.compare(state)` method with the same Jev-then-fallback structure as `Triager.run`. No Jev and no `ANTHROPIC_API_KEY` → `shadow_unscored`.

Cost: shadow runs `claude -p` for every message routed `reply`/`hold`, including ones the person would have answered in ten seconds, so it roughly doubles the person's Solo runs compared to `learn` (where stand-down skips those). `SHADOW_SAMPLE=0.5` halves it at the cost of a longer shadow period. Solo shares the person's plan rate window with their own interactive Claude Code use; a busy morning in shadow can eat into it. Say so in the first self-DM.

### What the person sees and when they can leave

`slackctl trust` (new) prints, per sender and per route: shadow comparisons scored, match rate, last five verdicts, `missed` count, and a readiness line. The same table is the Monday section of the digest. Readiness, shown and never enforced:
- per (route, sender): at least 5 scored comparisons, match rate ≥ 0.8, no `different` with `solo_overcommits` among the last five;
- per route overall: at least 20 scored at ≥ 0.85;
- triage health: `missed` below 10% of shadow items over the period.

Graduation is a human act and lands in `learn`, never `auto`: `slackctl trust @priya learn` (or `slackctl mode learn` for everyone), or reacting ✅ on the digest's "ready to hand over: reply ↔ Priya" line (tracked in `state["offers"][ts]`, same pattern again). Per-sender overrides live in `control.json` (section 6) and are consulted by `control.mode_for(sender)`, which replaces `CFG["mode"]` at the three call sites. Once in `learn`, the existing ledger starts at zero for that pair; shadow matches do not count toward `auto`. That is deliberate: `auto` must be earned on real approvals.

## 6. Pause, and runtime mode in general

### Semantics (decided)

Pause means: Solo takes no action and scores nothing, but keeps listening and keeps learning the person's voice. Precisely:
- inbound messages: triaged (cheap, Jev only) and written to the inbox as `queued` with `paused_at`, so `inbox` shows what arrived; no delay, no `claude -p`, no reaction, no draft, no `ignored` bookkeeping (so no `missed` is scored for a message the person was always going to answer);
- items in `handling` when pause lands: the `claude -p` run finishes (killing it mid-run wastes the spend and leaves a session half-written); `handle_with_claude` checks the control file after `run_claude` returns and, if paused, stores the decision and sets `status=parked` instead of calling `execute_decision`;
- pending drafts in the self-DM: stay; ✅ and ❌ still work, because a reaction is the human pushing the button, not Solo acting. Documented alternative: `pause --hard` also ignores reactions. Not recommended;
- owner messages: `on_owner_message` keeps closing follow-ups, recording `corrected`/`deleted` for sends that happened before the pause, and appending voice samples. It stops matching pending drafts as `replaced` (the person replying while paused says nothing about the draft);
- follow-up reminders: still fire (they are promises already made, to the person's own self-DM);
- `tick`: still scores `clean` for pre-pause sends, still expires drafts.

Resume: `queued`/`parked` items older than `RESUME_STALE_MINUTES` (30) are closed as `stale` and never scored; newer `parked` items run `execute_decision` after a fresh stand-down check with `newer_messages` (the person may have answered during the pause); newer `queued` items go through `handle_with_claude` normally. Default conservative: a reply arriving 40 minutes late, as if from the person, is worse than no reply.

### Triggers

- CLI: `slackctl pause [--for 2h | --until 17:30] [--why "in a board meeting"]`, `slackctl resume`, `slackctl mode shadow|learn|draft|auto`, `slackctl trust <@who> shadow|learn|draft|auto`.
- Self-DM text, handled in `on_owner_message` where `channel == self.self_dm` is already a special case `[verified: it is excluded from voice sampling there]`: `pause`, `pause 2h`, `pause until 5pm`, `resume`, `status`, `mode learn`. Solo answers in the self-DM in one line. Works from the phone.
- Reaction: ⏸️ on any Solo message in the self-DM pauses for `PAUSE_DEFAULT=2h`; ▶️ resumes. Hooks into `on_reaction` beside the ✅/❌ handling.
- Expiry: `tick` clears `paused.until` when past, and posts "resumed" to the self-DM.

### Mechanism

New `scripts/control.py` (about 60 lines), one JSON file `control.json` in `STATE_DIR`:
```json
{"paused": {"since": "…", "until": "…|null", "by": "cli|self-dm|reaction", "why": ""},
 "mode": null,
 "sender_modes": {"U0123": "learn"}}
```
`control.load()` caches on mtime and is called at the top of `Daemon.on_event` (one `stat` per event); `control.set_paused`, `control.resume`, `control.set_mode`, `control.set_sender_mode` write atomically with the same `fcntl` lock pattern as `State.update` `[verified: slackctl.py:State.update]`. `control.mode_for(sender)` returns `sender_modes[sender] or control.mode or CFG["mode"]`. Base mode from `.env` stays the default; the control file is the override; `status` prints both so nobody is confused about why `.env` says `learn` and the daemon is in `shadow`.

Touch points: `policy_preview`, `execute_decision`, `Ledger.scorecard` (take a `mode_for` callable or the resolved mode instead of `CFG["mode"]`), `Daemon.on_inbound` (paused check before triage delay; shadow branch), `Daemon.handle_with_claude` (parked), `Daemon.on_owner_message` (commands; skip `replaced` while paused), `Daemon.on_reaction` (⏸️/▶️, 👍/👎, ✅ on offers), `Daemon.tick` (expiry, digest, shadow sweep), `State._read` (its hard-coded default keys `pending, sent, ignored, last_auto, sessions` gain `shadow`, `shadow_notices`, `offers`, otherwise every `.get()` on them needs a guard `[verified: slackctl.py:State._read]`), `cmd_status` (print control state), and new `cmd_pause`/`cmd_resume`/`cmd_mode`/`cmd_trust`. The allow/block/share lists resolved in `Daemon.__init__` can stay startup-only; nothing here needs them live.

SKILL.md changes, small but required: `setup` step 6 tells the person to start the daemon in `MODE=learn`; it should say shadow and explain the comparisons. `status` should summarise control state (paused, mode, per-sender overrides) and shadow readiness. `review` step 2's list of patterns gains "shadow misses and overcommits", since the correction pairs now include them. `handle` is unchanged: Claude never knows whether it is in shadow, which is what keeps the comparison honest.

## 7. The console

### What a person needs to see and do

| need | surface today | surface proposed |
|---|---|---|
| pending drafts, approve/drop | self-DM ✅/❌; `drafts`, `approve`, `drop` | unchanged |
| shadow comparisons, overrule | none | self-DM notice per comparison with 👍/👎 (section 5) |
| trust and graduation | `status` scorecard; `review-data` | `slackctl trust` table; Monday digest with "ready to hand over" lines and ✅ to accept |
| pause / resume / mode | edit `.env`, restart | self-DM commands, reactions, CLI (section 6) |
| voice | `voice`; `review` skill edits VOICE.md | unchanged, plus the weekly headless review below |
| dossiers | `about`, `dossier`, `note` | unchanged |
| cost | per-run log line | `slackctl cost --days 30`, and a line in the digest, aggregated from `item["run"]["cost_usd"]` already on disk |
| health | `doctor`, `status` | `doctor` gains checks for control state, STATE_DIR permissions, and "was offline N hours since last event" |

### What it should be

Recommendation: **the Slack self-DM is the console, the CLI is the console's engine, and the `review` skill stays the thing that edits VOICE.md.** Reasons given this architecture:
- State is on the person's disk, behind their OS login. Any web console is either local or it exfiltrates. A published Artifact is rejected on that ground alone: it would need the person's drafts, colleagues' messages and dossiers uploaded to a shared host, and its sharing model is wrong for data that must never leave the person.
- The self-DM is already the inbox for drafts and the person already reacts there. Adding comparisons, commands and a digest costs about 200 lines in `slackctl.py`, works on the phone, needs no auth, and dies with the daemon (nothing to keep running).
- Block Kit buttons would be nicer than reactions, but interactive payloads on a user-scopes-only app with no bot user are unverified `[inferred]`. Reactions and text commands are the baseline; buttons are an upgrade if a ten-minute test shows they arrive over Socket Mode.
- A local web page (`slackctl console` on `127.0.0.1`, stdlib `http.server`, one HTML file, reads the same JSON) is realistic to build in a day and is the right place for side-by-side shadow comparisons over weeks, a trust matrix, and VOICE.md editing. It is also another thing to keep working across Python versions and browsers. Defer to Phase 3 and build it only if people complain that the self-DM is noisy or they cannot find old comparisons.
- `/solo:slack-autoreply status|review|simulate` in Claude Code remain for people who like the terminal.

### The training loop without a coding agent

The loop is: Solo drafts or shadows → the person reacts, replies, edits or overrules → outcomes and correction pairs accumulate → `review` folds patterns into VOICE.md and dossiers. Only the last step needs Claude Code, and today it needs a human to start it and watch it. Make it headless and scheduled:
- `slackctl review --apply` spawns `claude -p "/solo:slack-autoreply review" --allowedTools "Edit(~/.slack-autoreply/VOICE.md),Edit(~/.slack-autoreply/people/**),Edit(~/.slack-autoreply/channels/**),Bash(python3 …/slackctl.py review-data*)"`, snapshots VOICE.md before and after, and posts the diff (unified, truncated to 40 lines) to the self-DM with "revert" as a reaction option (keep `VOICE.md.prev`).
- `tick` runs it weekly at `REVIEW_WEEKLY=mon 09:00` and the Monday digest links it.
- SKILL.md `review` step 5 ("report in a few lines") already produces the summary that goes into the digest.
This is the difference between "an engineer ran review for the owner every week" and "every teammate's Solo reviews itself and shows them ten lines". Setup's three questions remain the only human-in-the-loop requirement, and they happen once.

## 8. Isolation and privacy

### What already supports it

- Everything per person is under one directory the person owns, `700`, `.env` `600` `[verified: install.sh]`; the directory is per OS user, so two people on two laptops cannot read each other by construction.
- `claude -p` reads only `CONTEXT_DIRS` via `Read(//dir/**)` rules and `--add-dir`, with `Write`, `Edit`, `Bash` (except `slackctl`) and `Agent` disabled `[verified: slackctl.py:claude_command]`. MCP is limited to the person's own configured servers.
- Tokens are never in the service unit; `install-service.sh` only copies `PATH` `[verified: deploy/install-service.sh]`.
- The share gate stops context-backed replies to untrusted senders `[verified: execute_decision gate]`.
- SKILL.md rule 3 treats Slack content as data, not instructions.

### What is missing

- **Claude Code's own transcripts.** Each `claude -p` session is persisted by Claude Code under `~/.claude/projects/<cwd-encoded>/<session>.jsonl` `[inferred, high confidence]`. They contain colleagues' message text, dossiers as printed by `show`, and every draft. They are per OS user, so isolation holds, but they are outside `AUTOREPLY_HOME`, outside any retention, and a `rm -rf ~/.slack-autoreply` does not remove them. Document it; add `slackctl prune --days 90` that deletes expired inbox items, old `state.json` entries and, optionally, those session files for sessions listed in `state["sessions"]`.
- **Retention.** Inbox items hold other people's messages at rest indefinitely. Same `prune`, scheduled from `tick`.
- **`doctor` checks**: STATE_DIR mode is 700 and owned by the user; STATE_DIR is not under a synced folder (iCloud Drive, Dropbox, Google Drive paths); `~/.claude.json` is 600. A synced `~/.slack-autoreply` is the most likely way a token leaks to a second machine by accident.
- **Listener assertion**: `authorizations[0].user_id == self.me` and `team_id == self.team`, else log and drop. Defence in depth even with per-person apps; mandatory with a relay.
- **Dossiers are private notes about colleagues.** The temptation at team scale is "team dossiers" (everyone's notes about Priya merged). Reject it: the notes are one person's working context and may contain things another person should not see. Nothing in the design shares them.
- **Shared always-on box** (3b option 2): one macOS user per person, FileVault, no shared admin account, `AUTOREPLY_HOME` under that user's home, `claude setup-token` done by the person in their own session, `CONTEXT_DIRS` mirrored one-way into their home. Anyone with `sudo` on that box can read everything; say so in the recipe and keep the box count small.
- **Jev key.** If `TYPESAFE_API_KEY` is a team key, one vendor receives every teammate's inbound messages, thread context and dossier excerpts `[verified: Daemon.on_inbound state dict]`. That is a data-processing decision, not an engineering one (section 11).

## 9. What breaks at team scale

1. **Socket Mode and the shared app-level token.** Blocker 1. Resolved by one app per person; the 10-connection ceiling `[inferred]` becomes irrelevant because each app has one.
2. **Slack rate limits.** Per inbound message the daemon makes three `conversations.history`/`replies` calls (`transcript` for the initial 8-message context, `newer_messages` for the stand-down check, `transcript` again for the full `CONTEXT_MESSAGES` refresh), plus `chat.getPermalink`, `users.info` on first sight, and `users.getPresence` once for the triage state and once more if `ONLY_WHEN_AWAY` is on `[verified: Daemon.on_inbound]`; `build-voice` makes a few hundred calls. Limits are per app per workspace, so per-person apps give each person their own budget; a shared app would pool N people into one. Separately, Slack tightened `conversations.history` and `conversations.replies` for non-Marketplace apps created after mid-2025 to a very low tier `[inferred; the team's measured "it works" for one person is not evidence about which tier the app is on]`. If that applies, `transcript()` is the bottleneck for everyone, and it argues again for per-person apps rather than one shared one. Verify before Phase 1 (appendix).
3. **Per-person cost and rate windows.** Every handled message is a `claude -p` run on that person's plan. Shadow roughly doubles runs. Solo competes with the person's interactive use for their plan's rate window. Mitigations already in the config: `CLAUDE_MODEL` (point Solo at a cheaper model), `MAX_REPLIES_PER_HOUR_PER_CONVERSATION`, `CLAUDE_MAX_TURNS`; add `SHADOW_SAMPLE` and `slackctl cost`. There is no team-level view of spend, by design; if finance needs one, each person's digest line is what they report.
4. **Workspace admin approval.** If the workspace requires approval for app installs, option A means N approvals of an identical manifest. This is the one thing that can push the design to a shared app plus relay. Ask before Phase 1.
5. **Laptop sleep and offline gaps.** Events during disconnection are lost `[inferred]`; `slack_sdk`'s Socket Mode client reconnects on wake `[inferred]`. Do not add catch-up: replaying hours of DMs would produce late replies as if from the person. Instead `tick` notices a gap in event timestamps and posts "Solo was offline 3h; 4 DMs arrived and were not handled" to the self-DM. People who need absence cover move to 3b option 2.
6. **Support burden.** N machines, N `PATH`s, N Python installs, N launchd units. `doctor` is the lever: add `doctor --report` producing a redacted bundle (versions, control state, last 50 log lines with tokens and message text stripped) to paste into `#solo`. Pin `VERSION`; `install.sh` is already idempotent and never touches `.env`/`VOICE.md`. AGENTS.md is rewritten from "coding agent deploys for the owner" to "owner runs `pair`; coding agent only for failures".
7. **Dedup and sessions** are per process `[verified: Daemon._dup, state.json sessions]` and stay per person; nothing to change.

## 10. Phased plan

**Phase 0: unblock (small, two to three days).**
`control.py` with `control.json`; pause/resume/mode/trust CLI and self-DM commands; `mode_for(sender)` replacing `CFG["mode"]` at the three call sites; parked/stale handling; `MODE=shadow` accepted by `config.py` and made the default in `.env.example`; listener assertion on `authorizations`; `doctor` checks for STATE_DIR permissions and synced folders. Offline tests extended with the fake Slack asserting no posts while paused. Unblocks: everything below, and gives the current single install a pause button this week.

**Phase 1: shadow, and a second person (about a week).**
Shadow branch in `execute_decision`; `on_inbound` shadow handling (frozen context, no stand-down, no delay); `state["shadow"]` and the `on_owner_message` comparison; `Triager.compare` with Jev; new outcomes and `Ledger.shadow_stats`; self-DM notices with 👍/👎; `slackctl trust`; Monday digest with offers. Then onboard two teammates by hand (today's AGENTS.md plus the new commands), each with their own app on their own laptop, in shadow. Unblocks: evidence that shadow comparisons are legible and that per-person apps are approved by the admin without drama. Exit criterion: one teammate graduates one sender to `learn` on their own, from the self-DM, with nobody watching.

**Phase 2: self-serve onboarding and the autonomous training loop (about a week).**
`slackctl pair`; `build-voice` inside pairing; weekly headless `review --apply` with diff to self-DM; `slackctl cost`; `slackctl prune` on a schedule; `doctor --report`; AGENTS.md rewritten for the owner; a one-page "Solo for teammates" doc. Unblocks: anyone on the team installing without an engineer.

**Phase 3: conditional.**
Only as needed, in this order of likelihood: (a) always-on per-person box recipe with `claude setup-token`, one-way `CONTEXT_DIRS` sync and `claude mcp add` re-authorisation, for people who want absence cover; (b) local web console if the self-DM proves too noisy; (c) shared app plus relay and the OAuth Worker, only if the admin refuses N apps or the team passes about ten installs; (d) Block Kit buttons if the Socket Mode interactivity test passes.

## 11. Open questions that need a human decision

1. **Slack admin:** does the workspace require app approval, and would the admin rather approve N identical "Solo (handle)" apps or one shared app that needs a relay service? This decides 3a.
2. **Jev data processing:** is one team `TYPESAFE_API_KEY` acceptable given it receives everyone's inbound messages and dossier excerpts, or does each person get their own key, or does triage fall back to Claude Code deciding the route (slower, no comparator)?
3. **Absence cover:** does anyone need Solo to work while their laptop is closed? If yes, who pays for the box and who administers it, given that the administrator can read everything on it?
4. **Retention:** how long may colleagues' message text sit in a teammate's inbox items and Claude Code transcripts? Proposed default 90 days.
5. **Pause and reactions:** confirm that ✅ on an existing draft should still send while paused (section 6). I recommend yes.
6. **Default notify level in shadow:** one self-DM per comparison (`each`) or a daily digest. I recommend `each` for the first two weeks.
7. **Disclosure policy at team scale:** `DISCLOSE_IF_ASKED=true` is per person today. Should it be a team rule that nobody may turn off?
8. **Cost visibility:** does anyone need a team-level spend number, and if so is a self-reported digest line enough?
9. **Who is allowed to run `auto`:** a team norm, or each person's call? The ledger enforces earning it; it does not enforce who may turn `MODE=auto` on.

## Appendix: inferred claims and how to verify each

| claim | where used | how to check (human-run; this session must not touch Slack) |
|---|---|---|
| ~~Socket Mode hands each event to one of up to 10 connections on the same app~~ **VERIFIED 2026-10-13** | §2 blocker 1, §3a | Confirmed against docs.slack.dev/apis/events-api/using-socket-mode: "each payload may be sent to _any_ of the connections"; limit "up to 10". No empirical test needed — blocker 1 stands |
| `apps.event.authorizations.list` needs an app-level scope beyond `connections:write` | §3a option C | Slack method reference for `apps.event.authorizations.list` |
| Create-app-from-manifest deep link (`?new_app=1&manifest_yaml=`) | §4 `pair` | Try the URL once; fall back to printing the manifest |
| App collaborators can see client secret and app-level tokens | §4 shared-app path (i) | Slack "Collaborators" docs or one look at the app's settings as a collaborator |
| OAuth redirect URLs must be HTTPS | §4 path (ii) | Slack OAuth docs; try adding `http://127.0.0.1:…` as a redirect URL |
| Non-Marketplace apps created after mid-2025 get a very low `conversations.history`/`replies` tier | §9 item 2 | Slack changelog for 2025 rate limits; or read the `Retry-After` behaviour in `listen.log` during a burst |
| Events during a Socket Mode disconnection are not replayed; `slack_sdk` reconnects on wake | §9 item 5 | Close the lid for ten minutes with a DM sent meanwhile; read the log on wake |
| Interactive (Block Kit) payloads arrive over Socket Mode for a user-scopes-only app with no bot | §7 | Post one message with a button via `chat.postMessage` on the `xoxp-` token with interactivity enabled in the manifest; see whether an `interactive` envelope arrives |
| `claude setup-token` yields a long-lived subscription token usable headless; its lifetime and rotation | §3b option 2 | Claude Code docs; README already recommends it for headless boxes |
| Claude Code persists `-p` sessions under `~/.claude/projects/<cwd>/` | §8 | `ls ~/.claude/projects` after one handled message |
| claude.ai connectors cannot be authorised headlessly on a second box | §3b option 2 | Attempt `claude mcp add` for one connector on a box without a browser session |
