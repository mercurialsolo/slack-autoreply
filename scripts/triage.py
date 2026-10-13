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
                "ignore": "No reply needed: an acknowledgement, thanks, an FYI, a bot or system message, something aimed at someone else, or already answered in the thread.",
                "ack": "Just signal it was seen. The sender is sharing something or giving a heads-up; a quick 'seen' or an emoji is enough and no answer is expected yet.",
                "hold": f"The sender needs an answer, decision, or piece of work that takes real effort, judgment, or information {me} would have to go and get. The right move is a time-bound 'I'll get back to you by X'.",
                "reply": f"A direct, low-stakes answer is appropriate and can be given from the conversation plus {me}'s own documents, files, or accounts: a link, a status, a date, a number, a yes on something routine.",
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
    forced: str | None = None  # why policy overrode the model's route
    raw: dict = field(default_factory=dict)

    def summary(self) -> str:
        f = f" (forced: {self.forced})" if self.forced else ""
        return (f"{self.route}{f} · {self.topic} · conf {self.confidence:.2f} · sens {self.sensitivity:.1f} · "
                f"urg {self.urgency:.1f} · ctx {self.needs_context:.2f} · answerable {self.can_answer:.2f} · "
                f"share {self.asks_for_content:.2f} · {self.source}")


@dataclass
class Policy:
    min_confidence: float = 0.6
    sensitivity_escalate: float = 4.0
    # Below this, we don't have what we'd need to answer — promise a time instead of guessing.
    min_answerable: float = 0.5

    def apply(self, t: Triage) -> Triage:
        if t.route not in ROUTES:
            t.route, t.forced = "hold", f"unknown route {t.route!r}"
        if t.sensitivity >= self.sensitivity_escalate:
            # Even an "ignore" is worth a heads-up when it's this sensitive; the draft may still be NO_REPLY.
            t.route, t.forced = "escalate", f"sensitivity {t.sensitivity:.1f}"
        elif t.confidence < self.min_confidence:
            if t.route == "reply":
                t.route, t.forced = "hold", f"low confidence {t.confidence:.2f}"
            elif t.route == "ignore":
                t.route, t.forced = "ack", f"low confidence {t.confidence:.2f}"
        elif t.route == "reply" and t.can_answer < self.min_answerable:
            # Confident it deserves an answer, but we haven't got what the answer needs.
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


def jev(state: dict, me: str, api_key: str, timeout: float = 8.0) -> Triage:
    import httpx

    body = {"model": JEV_MODEL, "state": state, "questions": questions(me)}
    r = httpx.post(
        f"{TYPESAFE_BASE_URL}/systemone",
        json=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    r.raise_for_status()
    resp = r.json()

    route = _lookup(resp, "route") or {}
    sens = _lookup(resp, "sensitivity") or {}
    urg = _lookup(resp, "urgency") or {}
    ctx = _lookup(resp, "needs_context") or {}
    share = _lookup(resp, "asks_for_content") or {}
    topic = _lookup(resp, "topic") or {}
    answerable = _lookup(resp, "can_answer_now") or {}

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
        self.api_key = os.getenv("TYPESAFE_API_KEY", "").strip()
        self.jev_ok = bool(self.api_key)
        if not self.jev_ok and anthropic_client is not None:
            log.warning("TYPESAFE_API_KEY not set — triage runs on %s instead of Jev", fallback_model)
        elif not self.jev_ok:
            log.info("no triage configured (no TYPESAFE_API_KEY / ANTHROPIC_API_KEY) — Claude Code decides routes")

    def run(self, state: dict) -> Triage:
        t: Triage | None = None
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
        return self.policy.apply(t)
