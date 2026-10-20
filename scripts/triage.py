"""Triage: decide what kind of response a message deserves, before writing a word.

Primary: Jev — TypeSafe AI's "System One" model. One pass, no text generation,
typed answers with probabilities and a confidence score, ~10ms. You hand it the
application state and a set of declared questions; it hands back a choice, a
score, or a yes/no probability per question. That's exactly the shape of
"is this sensitive / urgent / answerable", so it sits in front of the writer.

Fallback when TYPESAFE_API_KEY is unset (or Jev errors): a small Claude call that
answers the same questions as JSON. Slower and pricier, same contract.

Routes:
  ignore    no reply needed
  ack       show it was seen (emoji reaction or a 2-word message), nothing more
  hold      the real answer takes work or info not at hand → time-bound "back to you by X"
  reply     answer it, in the owner's voice, with their context
  escalate  too sensitive to answer automatically → draft + alert the owner, never auto-send
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

import config

log = logging.getLogger("autoreply.triage")

ROUTES = ("ignore", "ack", "hold", "reply", "escalate")

TYPESAFE_BASE_URL = os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1").rstrip("/")
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")

SENSITIVITY_LEVELS = [
    "Routine: scheduling, logistics, casual chat, public information, small talk",
    "Mild: ordinary work questions, project status, where-is-the-doc, quick factual asks",
    "Moderate: customer or partner commitments, pricing hints, roadmap, internal-only details",
    "High: deals, contracts, money, hiring or firing, investor matters, legal questions, performance feedback",
    "Critical: confidential or personal matters, health, conflict, complaints, anything with legal or reputational risk",
]
URGENCY_LEVELS = [
    "No rush, whenever",
    "This week",
    "Today",
    "Within a couple of hours",
    "Right now: blocking, time-critical, or the sender is waiting on it",
]

# What the message is actually about. Routed on by nothing — it is passed to the writer
# so the draft starts from the right intent, and it is what `review` groups outcomes by.
TOPIC_CRITERIA = {
    "status_request": "Asking where something stands: progress, an ETA, whether a thing is done or live yet.",
    "status_update": "Telling the owner where something stands, without asking for anything back.",
    "action_request": "Asking the owner to do something, chase someone, or hand something over.",
    "credentials_access": "One-time codes, OTPs, logins, passwords, verification, account access or account lists.",
    "document_request": "Asking for a file, link, sheet, doc, dataset or numbers.",
    "decision": "Asking the owner to decide, approve, choose between options, or sign off.",
    "commitment": "Asking the owner to commit to a date, deadline, scope or go-live.",
    "scheduling": "Meetings, timing, availability, joining or moving a call.",
    "problem_report": "Something is broken, blocked, failing, banned or rejected.",
    "info_share": "An FYI, a link, news — no action expected.",
    "social": "Greeting, banter, congratulations, small talk.",
    "other": "None of the above fits.",
}


def questions(me: str) -> dict:
    """Declared questions, in Jev's request shape."""
    return {
        "route": {
            "type": "choice",
            "instructions": f"How should {me} handle this latest message right now?",
            "criteria": {
                "ignore": f"Nothing is expected back. Say ignore for anything informational: an FYI, a shared link or document, a status broadcast or numbers posted for the record, a heads-up that asks nothing, thanks or an acknowledgement, a bot or system message, something aimed at someone else, or something already answered in the thread. Informational counts as ignore even when it is addressed to {me} — if there is no question and nothing to do, the right action is no action.",
                "ack": f"A last resort. There is genuinely nothing to say and nothing that can be done, but {me} going silent would be wrong — the sender is handing something over or waiting to know the message landed before they move. Say ack ONLY if neither ignore nor reply fits: if there is any small thing that can simply be done or answered right now, that is reply. An emoji that could have been the answer is noise.",
                "hold": f"The sender needs an answer, decision, or piece of work that takes real effort, judgment, or information {me} would have to go and get. The right move is a time-bound 'I'll get back to you by X'.",
                "reply": f"The ask is small enough to simply handle now. Two kinds. (a) An answer that can be given from the conversation plus {me}'s own documents, files or accounts: a link, a status, a date, a number, a yes on something routine. (b) A small request about how {me} communicates or tracks things — post it in the thread, use this channel, add it to the list, keep me posted, format it this way — which is honoured by doing it and saying so in one line. Prefer reply over ack whenever the ask is this small; acknowledging a thing you could have just done floods the conversation.",
                "escalate": "Too sensitive or high-stakes to answer automatically: money, pricing, contracts, legal, hiring or people issues, investors, health, personal matters, complaints, conflict, anything confidential. A human must look first.",
                "other": "None of the above fits.",
            },
        },
        "sensitivity": {
            "type": "score",
            "instructions": "How sensitive is this message, considering who sent it and what they're asking?",
            "criteria": SENSITIVITY_LEVELS,
        },
        "urgency": {
            "type": "score",
            "instructions": "How soon does the sender need a response?",
            "criteria": URGENCY_LEVELS,
        },
        "needs_context": {
            "type": "noul",
            "instructions": f"Can this only be answered well by looking at {me}'s own files, documents, calendar, code, or connected accounts, rather than from the conversation alone?",
        },
        "asks_for_content": {
            "type": "noul",
            "instructions": f"Is the sender asking {me} to share a file, document, link, data, numbers, or other material?",
        },
        "topic": {
            "type": "choice",
            "instructions": "What is this latest message actually about?",
            "criteria": TOPIC_CRITERIA,
        },
        "can_answer_now": {
            "type": "noul",
            "instructions": (
                f"Is everything needed to answer this well already present — in the conversation above, "
                f"or in {me}'s own files and accounts that can be looked up right now? Answer no if a good "
                f"reply would require guessing, inventing a number or date, or information only someone "
                f"else holds."
            ),
        },
        "asks_to_execute": {
            "type": "noul",
            "instructions": (
                f"Is the sender asking {me} to DO something to a system, rather than asking for information? "
                f"Yes for: run a script or command, deploy, restart, install, change a setting or config, "
                f"grant or revoke access, create, edit or delete a file, record, account or ticket, send "
                f"something on to a third party. No for: asking a question, asking for a status, a number, "
                f"a link, a document, or an opinion — and no for how {me} should communicate or "
                f"track things (reply in the thread, use this channel, keep me posted, format it "
                f"this way, add it to the list), which costs nothing and is simply honoured."
            ),
        },
        "in_scope": {
            "type": "noul",
            "instructions": (
                f"Is this message strictly about the work of THIS Slack workspace — its own projects, "
                f"customers, systems, schedules and people? Answer no if it asks about another company, "
                f"another client or team {me} works with elsewhere, {me}'s other projects, personal matters, "
                f"or what {me} has been working on outside this workspace."
            ),
        },
    }


@dataclass
class Triage:
    route: str
    confidence: float          # confidence in the route, 0..1
    sensitivity: float         # 1..5
    urgency: float             # 1..5
    needs_context: float       # 0..1
    asks_for_content: float    # 0..1
    source: str                # "jev" | "claude" | "default"
    # Added after `source` so existing positional construction keeps working.
    topic: str = "other"       # what the message is about (TOPIC_CRITERIA key)
    can_answer: float = 0.5    # 0..1: is everything needed to answer well already at hand
    asks_to_execute: float = 0.0  # 0..1: wants an action on a system, not an answer
    in_scope: float = 1.0      # 0..1: is about this workspace's own work
    forced: str | None = None  # why policy overrode the model's route
    model_route: str = ""      # what the model said before policy touched it
    chars: int = 0             # length of the message itself, where the execute score goes astray
    raw: dict = field(default_factory=dict)

    def summary(self) -> str:
        f = (f" (forced: {self.forced}; model said {self.model_route})" if self.forced
             else "")
        return (f"{self.route}{f} · {self.topic} · conf {self.confidence:.2f} · sens {self.sensitivity:.1f} · "
                f"urg {self.urgency:.1f} · ctx {self.needs_context:.2f} · answerable {self.can_answer:.2f} · "
                f"share {self.asks_for_content:.2f} · exec {self.asks_to_execute:.2f} · "
                f"scope {self.in_scope:.2f} · {self.source}")


@dataclass
class Policy:
    min_confidence: float = 0.6
    sensitivity_escalate: float = 4.0
    # Below this, we don't have what we'd need to answer — promise a time instead of guessing.
    min_answerable: float = 0.5
    # Solo answers questions. It never performs actions, and never speaks outside this workspace's
    # work. Both of these force escalate — the owner sees it and decides; nothing is auto-approved.
    max_execute: float = 0.5
    min_in_scope: float = 0.5
    # A message this short is where `asks_to_execute` stops describing the message. Measured over
    # 138 scored messages: at or under this length the median is 0.55, above it 0.36. "ty" came in
    # at 0.86, "i dont think so" at 0.92, "421452" at 0.89 — none of them asks for anything. The
    # score is reading the thread they arrived in.
    short_message_chars: int = 30

    def apply(self, t: Triage) -> Triage:
        # Kept so forcing can be measured rather than argued about. The action rule fired on 48
        # of 115 messages — including "ty" at 0.86 — and `raw` is nulled before an item is saved,
        # so there was no way to ask how often it turned a correct silence into a message for the
        # owner. It overrides `ignore` and `ack` too, and that is the case worth counting.
        t.model_route = t.route
        if t.route not in ROUTES:
            t.route, t.forced = "hold", f"unknown route {t.route!r}"
        # These two come first: they are hard boundaries, not preferences, so they outrank
        # every other reason a message might have looked routine.
        if t.asks_to_execute >= self.max_execute:
            # …except where the score is measuring the thread rather than the message, and the
            # model — which read both — chose to stay quiet. The boundary this gate defends is
            # that Solo never performs an action. `ignore` and `ack` perform none: they are
            # silence and an emoji. Forcing them to the owner does not protect anything, it just
            # moves a "ty" into their queue. Observed, not supposed: "i think is the issue",
            # 20 characters, scored 0.55, model said ignore, escalated.
            #
            # `hold` and `reply` are not exempt. "and dont have it run" is 20 characters and a
            # genuine instruction; the owner should see it.
            if 0 < t.chars <= self.short_message_chars and t.model_route in ("ignore", "ack"):
                log.info("not forcing: %d-char message scored %.2f for action, model said %s",
                         t.chars, t.asks_to_execute, t.model_route)
            else:
                t.route, t.forced = "escalate", f"asks for an action, not an answer ({t.asks_to_execute:.2f})"
                return t
        # Scope only matters when Solo would SAY something. Ignoring an out-of-scope message
        # leaks nothing, and a bare FYI with little context scores low on scope for want of
        # signal rather than because it is off-limits — escalating those just moves the flood.
        if t.in_scope < self.min_in_scope and t.route in ("reply", "hold", "ack"):
            t.route, t.forced = "escalate", f"outside this workspace's work ({t.in_scope:.2f})"
            return t
        if t.sensitivity >= self.sensitivity_escalate:
            # Even an "ignore" is worth a heads-up when it's this sensitive; the draft may still be NO_REPLY.
            t.route, t.forced = "escalate", f"sensitivity {t.sensitivity:.1f}"
        elif t.confidence < self.min_confidence:
            if t.route == "reply":
                t.route, t.forced = "hold", f"low confidence {t.confidence:.2f}"
            elif t.route == "ignore":
                t.route, t.forced = "ack", f"low confidence {t.confidence:.2f}"
        elif t.route == "reply" and t.can_answer < self.min_answerable and t.needs_context >= 0.5:
            # Confident it deserves an answer, but the answer needs something we have to go and
            # get. Only then is a hold right: when nothing needs looking up (a small ask about how
            # to communicate, say) "can_answer" reads low simply because there is nothing to
            # answer, and demoting that to a hold would promise a callback for a two-word job.
            t.route, t.forced = "hold", f"not answerable yet {t.can_answer:.2f}"
        return t


# ----------------------------------------------------------------- Jev (REST)


def _score_1_to_n(ans: dict, n: int) -> float:
    """Normalise a Jev score answer to 1..n whatever the index base."""
    probs = ans.get("probabilities")
    if isinstance(probs, dict) and probs:
        try:
            keys = sorted(int(k) for k in probs)
            base = keys[0]
            return max(1.0, min(float(n), sum((int(k) - base + 1) * float(p) for k, p in probs.items())))
        except (TypeError, ValueError):
            pass
    s = float(ans.get("score", 0))
    if s < 1:  # zero-based index
        s += 1
    return max(1.0, min(float(n), s))


def _lookup(resp: dict, key: str) -> dict | None:
    """Find a question's answer whether the response groups by name or by type."""
    answers = resp.get("answers")
    if isinstance(answers, dict) and key in answers:
        return answers[key]
    for bucket in ("choices", "scores", "nouls"):
        b = resp.get(bucket)
        if isinstance(b, dict) and key in b:
            return b[key]
    return None


def jev(state: dict, me: str, api_key: str, timeout: float = 8.0, attempts: int = 3) -> Triage:
    import time

    import httpx

    body = {"model": JEV_MODEL, "state": state, "questions": questions(me)}
    # 429/5xx from Jev are transient (529 = overloaded). Without a retry a single blip
    # drops the message to the fallback, which is slower and may not be configured at all.
    last = None
    for attempt in range(attempts):
        try:
            r = httpx.post(
                f"{TYPESAFE_BASE_URL}/systemone",
                json=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=timeout,
            )
            r.raise_for_status()
            break
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status == 429 or status >= 500
            last = e
            if not retryable or attempt == attempts - 1:
                raise
            delay = 0.4 * (3 ** attempt)
            log.warning("jev %s (attempt %d/%d) — retrying in %.1fs", status or type(e).__name__,
                        attempt + 1, attempts, delay)
            time.sleep(delay)
    else:  # pragma: no cover - loop always breaks or raises
        raise last
    resp = r.json()

    route = _lookup(resp, "route") or {}
    sens = _lookup(resp, "sensitivity") or {}
    urg = _lookup(resp, "urgency") or {}
    ctx = _lookup(resp, "needs_context") or {}
    share = _lookup(resp, "asks_for_content") or {}
    topic = _lookup(resp, "topic") or {}
    answerable = _lookup(resp, "can_answer_now") or {}
    execute = _lookup(resp, "asks_to_execute") or {}
    scope = _lookup(resp, "in_scope") or {}

    choice = str(route.get("choice", "hold"))
    conf = route.get("confidence")
    if conf is None and isinstance(route.get("probabilities"), dict):
        conf = max(float(p) for p in route["probabilities"].values())
    return Triage(
        route=choice,
        confidence=float(conf if conf is not None else 0.5),
        sensitivity=_score_1_to_n(sens, len(SENSITIVITY_LEVELS)),
        urgency=_score_1_to_n(urg, len(URGENCY_LEVELS)),
        needs_context=float(ctx.get("noul", 0.5)),
        asks_for_content=float(share.get("noul", 0.0)),
        topic=str(topic.get("choice", "other")),
        can_answer=float(answerable.get("noul", 0.5)),
        # Fail closed: an unreadable answer must look like "wants an action" / "out of scope",
        # so a malformed Jev response escalates to the owner rather than sailing through.
        asks_to_execute=float(execute.get("noul", 1.0)),
        in_scope=float(scope.get("noul", 0.0)),
        source=f"jev:{resp.get('model', JEV_MODEL)}",
        raw=resp,
    )


# ------------------------------------------------------------ Claude fallback


def claude(state: dict, me: str, client, model: str) -> Triage:
    q = questions(me)
    schema_hint = {
        "route": "one of " + ", ".join(ROUTES),
        "route_confidence": "0..1",
        "sensitivity": f"1..{len(SENSITIVITY_LEVELS)}",
        "urgency": f"1..{len(URGENCY_LEVELS)}",
        "needs_context": "0..1 probability",
        "asks_for_content": "0..1 probability",
        "topic": "one of " + ", ".join(TOPIC_CRITERIA),
        "can_answer_now": "0..1 probability",
        "asks_to_execute": "0..1 probability",
        "in_scope": "0..1 probability",
    }
    prompt = (
        "You are a triage classifier for incoming Slack messages. Answer the questions about the STATE "
        "and return ONLY a JSON object with these keys:\n"
        f"{json.dumps(schema_hint, indent=1)}\n\n"
        f"QUESTIONS:\n{json.dumps(q, indent=1)}\n\nSTATE:\n{json.dumps(state, ensure_ascii=False, indent=1)}"
    )
    resp = client.messages.create(model=model, max_tokens=200, messages=[{"role": "user", "content": prompt}])
    text = "".join(getattr(b, "text", "") for b in resp.content)
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {}
    return Triage(
        route=str(data.get("route", "hold")),
        confidence=float(data.get("route_confidence", 0.5)),
        sensitivity=float(data.get("sensitivity", 3)),
        urgency=float(data.get("urgency", 2)),
        needs_context=float(data.get("needs_context", 0.5)),
        asks_for_content=float(data.get("asks_for_content", 0.0)),
        topic=str(data.get("topic", "other")),
        can_answer=float(data.get("can_answer_now", 0.5)),
        asks_to_execute=float(data.get("asks_to_execute", 1.0)),
        in_scope=float(data.get("in_scope", 0.0)),
        source=f"claude:{model}",
        raw=data,
    )


# ------------------------------------------------------------------ frontend


class Triager:
    def __init__(self, me: str, policy: Policy, anthropic_client, fallback_model: str):
        self.me = me
        self.policy = policy
        self.anthropic = anthropic_client
        self.fallback_model = fallback_model
        self.api_key = config.env("TYPESAFE_API_KEY")
        self.jev_ok = bool(self.api_key)
        if not self.jev_ok and anthropic_client is not None:
            log.warning("TYPESAFE_API_KEY not set — triage runs on %s instead of Jev", fallback_model)
        elif not self.jev_ok:
            log.info("no triage configured (no TYPESAFE_API_KEY / ANTHROPIC_API_KEY) — Claude Code decides routes")

    def run(self, state: dict) -> Triage:
        t: Triage | None = None
        # `apply` gates on a score the message's own length tells us to distrust, and it is the
        # only place the message itself is still in hand.
        chars = len(" ".join((state.get("latest_message") or "").split()))
        if self.jev_ok:
            try:
                t = jev(state, self.me, self.api_key)
            except Exception as e:
                log.warning("Jev call failed (%s) — falling back to %s", e, self.fallback_model)
        if t is None and self.anthropic is not None:
            try:
                t = claude(state, self.me, self.anthropic, self.fallback_model)
            except Exception as e:
                log.error("triage fallback failed too (%s)", e)
        if t is None:
            t = Triage("hold", 0.0, 3.0, 2.0, 0.5, 0.0, "default", forced="triage unavailable")
        t.chars = chars
        return self.policy.apply(t)


def pick_channel(subject: str, catalogue: dict[str, str], api_key: str = "",
                 timeout: float = 8.0) -> tuple[str, str, float]:
    """Which conversation does this belong in? → (id, why, confidence).

    A typed choice over the channels actually learned from the workspace — exactly the shape Jev
    is for, and the reason agents never need to know the channel map: they say what happened,
    Solo decides where it goes.

    Without a Jev key this returns ("", "no triage model", 0.0) so the caller asks the owner
    rather than guessing a channel, which is the expensive mistake.
    """
    api_key = api_key or config.env("TYPESAFE_API_KEY")
    if not api_key or not catalogue:
        return "", "no triage model configured" if not api_key else "nothing learned yet", 0.0
    import httpx
    questions = {"channel": {"type": "choice",
                             "instructions": "Which of these conversations does this belong in? "
                                             "Pick the one whose regular traffic it would sit among "
                                             "naturally. If none of them fits, say other.",
                             "criteria": {**catalogue, "other": "None of these is the right place."}}}
    body = {"model": JEV_MODEL, "state": {"to_be_said": subject}, "questions": questions}
    try:
        r = httpx.post(f"{TYPESAFE_BASE_URL}/systemone", json=body,
                       headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                       timeout=timeout)
        r.raise_for_status()
        ans = _lookup(r.json(), "channel") or {}
    except Exception as e:  # noqa: BLE001 - a failed pick must ask, never guess
        log.warning("channel pick failed (%s)", e)
        return "", f"could not decide ({type(e).__name__})", 0.0
    choice = str(ans.get("choice", "other"))
    conf = ans.get("confidence")
    if conf is None and isinstance(ans.get("probabilities"), dict):
        conf = max(float(p) for p in ans["probabilities"].values())
    if choice == "other":
        return "", "none of the learned channels fits", float(conf or 0.0)
    return choice, catalogue.get(choice, ""), float(conf if conf is not None else 0.5)


# How exposed a channel is. Routing a sensitive report into a broad room is a leak even when
# every word of it is true, so this is scored per channel and compared against the report.
CHANNEL_EXPOSURE_LEVELS = [
    "Closed: a couple of people working one thing together. Little blast radius.",
    "Team: one team's working channel. Internal detail is normal here.",
    "Cross-team: several teams or functions read it. Assume people outside the work see it.",
    "Company-wide: announcements, everyone. Treat as published internally.",
    "Shared or external: customers, partners or contractors are in it, or it is a customer-named "
    "room where a customer could be added. Assume the subject of the message may read it.",
]


def score_channel(name: str, description: str, members: int, api_key: str = "",
                  timeout: float = 8.0) -> tuple[float, str]:
    """(exposure 1..5, one-line read). Jev scores how public a channel effectively is."""
    api_key = api_key or config.env("TYPESAFE_API_KEY")
    if not api_key:
        return 0.0, "no triage model configured"
    import httpx
    qs = {"exposure": {"type": "score",
                       "instructions": "How exposed is this Slack channel — how far does something "
                                       "said here travel, and who might read it?",
                       "criteria": CHANNEL_EXPOSURE_LEVELS}}
    body = {"model": JEV_MODEL,
            "state": {"channel": name, "what_it_is_for": description, "members": members},
            "questions": qs}
    try:
        r = httpx.post(f"{TYPESAFE_BASE_URL}/systemone", json=body,
                       headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                       timeout=timeout)
        r.raise_for_status()
        ans = _lookup(r.json(), "exposure") or {}
    except Exception as e:  # noqa: BLE001
        log.warning("channel exposure scoring failed for %s (%s)", name, e)
        return 0.0, f"could not score ({type(e).__name__})"
    return _score_1_to_n(ans, len(CHANNEL_EXPOSURE_LEVELS)), CHANNEL_EXPOSURE_LEVELS[
        max(0, min(len(CHANNEL_EXPOSURE_LEVELS) - 1,
                   int(round(_score_1_to_n(ans, len(CHANNEL_EXPOSURE_LEVELS)))) - 1))].split(":")[0]
