"""Which channel and which modality a piece of information goes out in — learnt per person, not ruled.

Owner, 2026-11-12 23:10Z: "Information adaptation by channel is also a nuance that needs to be learnt e.g.
which info is best shared or asked for in which channel and how - explaining something using visuals, text
or conversation are all different modalities."

So there is no if/else table here. Three things happen:

  1. choose()   a model picks the channel (slack, sms, voice, meeting, email, dashboard) and the modality
                (spoken, short_text, structured_text, visual) for one outbound piece — an update, an ask,
                an explanation, a confirm — from the content, its urgency, what the person must do with it,
                where they are now, and that person's learnt profile (context, never a rule).
  2. record_*() every choice, its outcome (answered or not, how fast, asked again) and every correction
                ("send me that on Slack", "show me", "too long") is appended per person.
  3. learn()    the nightly pass: behaviour as data. It folds the log into a per-person profile in the store
                (people_channels.json), live at once with no release. The next choose() reads it.

The profile's `examples` are illustrative: what happened, for the model to weigh, not a lookup.
Run the nightly pass with `python3 scripts/channel_choice.py learn` (a schedule calls it).
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from statistics import median

import config

CHANNELS = ("slack", "sms", "voice", "meeting", "email", "dashboard")
MODALITIES = ("spoken", "short_text", "structured_text", "visual")
KINDS = ("update", "ask", "explanation", "confirm")
LOG = "channel_events.jsonl"
PROFILES = "people_channels.json"
EXAMPLES_KEPT = 8
TIMEOUT_S = 60
# A choice holds for a person and a kind of piece this long, so a conversation pays one chooser call, not one
# per reply. A correction from that person, or a newly learnt profile, ends it at once.
CHOICE_TTL_S = 600
_choices: dict[tuple, tuple[float, dict]] = {}
_choices_lock = threading.Lock()
_clock = time.monotonic
# The chooser reads no untrusted text beyond a short content summary: no tools, no MCP, no session.
DEFAULT_CHOOSER = ('{claude} -p --model sonnet --output-format text --tools "" --strict-mcp-config '
                   '--no-session-persistence')

PROMPT = """You choose how one piece of information reaches {person}: the channel and the modality.

Channels: {channels}. Modalities: {modalities} (visual = a chart, diagram, screenshot or shared screen;
structured_text = text with buttons or a short list; spoken = a call or voice note).

The piece:
  kind: {kind}
  urgency: {urgency} (1 low .. 5 now)
  what they must do with it: {action}
  where they are now: {where}
  content: {content}

What {person}'s history says (learnt from what they answered, how fast, and what they corrected). The examples
are illustrative — weigh them, do not copy them as rules:
{profile}

Answer with JSON only: {{"channel": "...", "modality": "...", "why": "one short sentence"}}"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append(row: dict) -> None:
    row.setdefault("at", now_iso())
    with open(config.p(LOG), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def events(person: str | None = None) -> list[dict]:
    path = config.p(LOG)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and (person is None or r.get("person") == person):
            out.append(r)
    return out


def profiles() -> dict:
    try:
        d = json.loads(config.p(PROFILES).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def profile(person: str) -> dict:
    return profiles().get(person) or {}


# --- recording ---------------------------------------------------------------------------------------------

def record_choice(person: str, choice: dict, *, kind: str, content: str, urgency: int = 3,
                  where: str = "", piece: str | None = None) -> str:
    """One outbound piece and how it went out. Returns the piece id outcomes and corrections refer to."""
    piece = piece or uuid.uuid4().hex[:12]
    _append({"type": "choice", "person": person, "piece": piece, "kind": kind if kind in KINDS else "update",
             "channel": choice.get("channel"), "modality": choice.get("modality"), "why": choice.get("why", ""),
             "urgency": urgency, "where": where, "content": " ".join(content.split())[:200]})
    return piece


def record_outcome(person: str, piece: str, *, answered: bool, latency_s: float | None = None,
                   asked_again: bool = False) -> None:
    _append({"type": "outcome", "person": person, "piece": piece, "answered": bool(answered),
             "latency_s": latency_s, "asked_again": bool(asked_again)})


def record_correction(person: str, said: str, piece: str | None = None) -> None:
    """The person told us how they want it: kept verbatim, the nightly pass reads it."""
    _append({"type": "correction", "person": person, "piece": piece, "said": " ".join(said.split())[:300]})
    forget(person)


def forget(person: str | None = None) -> None:
    """Drop the held choices of `person` (everyone when None): the next choose() asks the model again."""
    with _choices_lock:
        for k in [k for k in _choices if person is None or k[0] == person]:
            del _choices[k]


# --- the nightly pass --------------------------------------------------------------------------------------

def learn(person: str | None = None) -> dict:
    """Fold the log into each person's profile. Stats only — what was answered, how fast, what was asked
    again, what was corrected — so the profile is evidence for the chooser, not a decision."""
    started = now_iso()   # a correction logged while this runs is after it, so the chooser still hears it
    rows = events(person)
    choices = {r["piece"]: r for r in rows if r.get("type") == "choice" and r.get("piece")}
    by_person: dict[str, dict] = {}
    for r in rows:
        p = by_person.setdefault(r.get("person") or "?", {"by_route": {}, "corrections": [], "examples": []})
        if r.get("type") == "correction":
            c = choices.get(r.get("piece") or "")
            p["corrections"].append({"said": r.get("said", ""), "at": r.get("at"),
                                     **({"was": f"{c.get('kind')} by {c.get('channel')}/{c.get('modality')}"} if c else {})})
        elif r.get("type") == "outcome":
            c = choices.get(r.get("piece") or "")
            if not c:
                continue
            route = f"{c.get('channel')}/{c.get('modality')}"
            s = p["by_route"].setdefault(route, {"sent": 0, "answered": 0, "asked_again": 0, "latencies": [],
                                                 "kinds": {}})
            s["sent"] += 1
            s["answered"] += 1 if r.get("answered") else 0
            s["asked_again"] += 1 if r.get("asked_again") else 0
            if r.get("answered") and isinstance(r.get("latency_s"), (int, float)):
                s["latencies"].append(float(r["latency_s"]))
            s["kinds"][c.get("kind")] = s["kinds"].get(c.get("kind"), 0) + 1
            p["examples"].append({"kind": c.get("kind"), "route": route, "content": c.get("content", "")[:80],
                                  "answered": bool(r.get("answered")), "latency_s": r.get("latency_s")})
    store = profiles()
    for who, p in by_person.items():
        for s in p["by_route"].values():
            lat = s.pop("latencies")
            s["median_latency_s"] = round(median(lat), 1) if lat else None
            s["answer_rate"] = round(s["answered"] / s["sent"], 2) if s["sent"] else None
        p["corrections"] = p["corrections"][-EXAMPLES_KEPT:]
        p["examples"] = p["examples"][-EXAMPLES_KEPT:]
        p["learnt_at"] = started
        store[who] = p
    tmp = config.p(PROFILES + ".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(config.p(PROFILES))
    forget(person)
    return store


# --- the choice --------------------------------------------------------------------------------------------

def profile_text(p: dict) -> str:
    if not p:
        return "  (nothing learnt yet)"
    lines = []
    for route, s in sorted((p.get("by_route") or {}).items()):
        lines.append(f"  {route}: sent {s.get('sent')}, answered {s.get('answered')}"
                     f"{', median ' + str(s['median_latency_s']) + 's' if s.get('median_latency_s') is not None else ''}"
                     f"{', asked again ' + str(s['asked_again']) if s.get('asked_again') else ''}")
    for c in p.get("corrections") or []:
        lines.append(f"  correction: \"{c.get('said')}\"" + (f" (after {c['was']})" if c.get("was") else ""))
    for e in p.get("examples") or []:
        lines.append(f"  e.g. {e.get('kind')} by {e.get('route')}: "
                     f"{'answered in ' + str(e.get('latency_s')) + 's' if e.get('answered') else 'not answered'}")
    return "\n".join(lines) or "  (nothing learnt yet)"


def _model(prompt: str, run=subprocess.run) -> str:
    cmd = config.env("CHANNEL_CHOOSER_CMD") or DEFAULT_CHOOSER.format(claude=shlex.quote(config.env("CLAUDE_BIN", "claude")))
    import staff_intake   # lazily: its reader env (no secrets) is the one place that knows what to strip
    p = run(shlex.split(cmd), input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S,
            env=staff_intake.reader_env())
    if p.returncode != 0:
        raise RuntimeError(f"the chooser exited {p.returncode}")
    return p.stdout or ""


def _said_since(person: str, prof: dict) -> str:
    """Corrections the nightly pass has not folded in yet: a person who said "too long" is heard now. Times
    are to the second, so one made in the second of learning is repeated rather than risk dropping it."""
    since = prof.get("learnt_at") or ""
    said = [e.get("said", "") for e in events(person) if e.get("type") == "correction" and e.get("at", "") >= since]
    return "".join(f"\n  said since (not learnt yet): \"{s}\"" for s in said[-3:])


def choose(person: str, *, kind: str, content: str, urgency: int = 3, action: str = "read it",
           where: str = "unknown", model=None, default: dict | None = None,
           available: tuple[str, ...] | None = None) -> dict:
    """{channel, modality, why}. The model decides; the person's profile is its context. When the model
    cannot answer, the `default` the caller is already in (a Slack thread, say) stands — never a table.
    `available` is the channels the caller can actually send on: the model chooses among those only, so
    what is recorded as chosen is what went out."""
    channels = tuple(c for c in (available or CHANNELS) if c in CHANNELS) or CHANNELS
    prof = profile(person)
    # a newly learnt profile (the nightly pass runs in its own process) is a new key, so it is read at once
    held_key = (person, prof.get("learnt_at"), kind, urgency, action, where, channels,
                json.dumps(default or {}, sort_keys=True), model)
    now = _clock()
    with _choices_lock:
        for k in [k for k, (at, _) in _choices.items() if now - at >= CHOICE_TTL_S]:
            del _choices[k]
        held = _choices.get(held_key)
    if held:
        return dict(held[1])
    fallback = dict(default or {"channel": "slack", "modality": "structured_text"}, why="the chooser did not answer")
    prompt = PROMPT.format(person=person, channels=", ".join(channels), modalities=", ".join(MODALITIES),
                           kind=kind, urgency=urgency, action=action, where=where,
                           content=" ".join(content.split())[:400],
                           profile=profile_text(prof) + _said_since(person, prof))
    try:
        out = (model or _model)(prompt)
        m = re.search(r"\{.*\}", out or "", re.S)
        v = json.loads(m.group(0)) if m else {}
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return fallback
    if not isinstance(v, dict) or v.get("channel") not in channels or v.get("modality") not in MODALITIES:
        return fallback
    got = {"channel": v["channel"], "modality": v["modality"], "why": str(v.get("why") or "")[:200]}
    with _choices_lock:
        _choices[held_key] = (now, got)   # only a real answer is held; a fallback asks again next time
    return dict(got)


if __name__ == "__main__":
    if sys.argv[1:2] == ["learn"]:
        got = learn(sys.argv[2] if len(sys.argv) > 2 else None)
        print(f"learnt {len(got)} profile(s)")
    else:
        print("usage: channel_choice.py learn [person]")
        sys.exit(2)
