#!/usr/bin/env python3
"""Slack <-> factory conversation (scripts/factory_converse.py) and the learnt channel choice
(scripts/channel_choice.py). No Slack, no factory, no model: a fake Slack poster, a fake factory CLI and a
fake judge stand in, so every case runs offline.

Cases from the brief: an update request, a new task, answering an ask by button, cancel with confirm, a
thread follow-up ("and the other one?"), non-owner read-only; from the owner's 23:10Z note: the choice,
its outcome and a correction are recorded, and the learnt profile changes the next choice.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="factory-chat-"))
os.environ["AUTOREPLY_HOME"] = str(HOME)
os.environ["CHANNEL_CHOOSER_CMD"] = "false"   # never a real model: a failing chooser falls back
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import channel_choice as C  # noqa: E402
import factory_converse as F  # noqa: E402

passed = 0
OWNER, STAFF, OTHER = "UOWNER", "USTAFF", "UOTHER"
DM = "DBOT"


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name}\n{detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


# --- the fakes -----------------------------------------------------------------------------------------------

TASKS = {
    "t_aaa11111": {"title": "Fix the login timeout", "state": "running"},
    "t_bbb22222": {"title": "Search results ranking", "state": "running"},
    "t_ccc33333": {"title": "Release 2.4 notes", "state": "blocked"},
    "t_ddd44444": {"title": "Login page copy", "state": "queued"},
}


class FakeFactory:
    def __init__(self):
        self.calls, self.briefs, self.next_id = [], [], "t_eee55555"

    def __call__(self, argv, input=None, **kw):
        self.calls.append(argv[1:])
        args = [a for a in argv[1:] if a != "--"]   # free text goes after "--"; read it the way argparse does
        out = ""
        if args[:2] == ["task", "find"]:
            q = args[-1].lower()
            hits = [{"task_id": t, **v} for t, v in TASKS.items() if all(w in v["title"].lower() for w in q.split()
                                                                         if len(w) > 3)]
            out = json.dumps({"tasks": hits})
        elif args[:2] == ["task", "status"]:
            t = TASKS[args[2]]
            out = (f"{args[2]} {t['state']} on ci (2h)\nworking: {t['title']}\ndoing: tests\nnext: review\n"
                   f"blockers: none\nlast log: ok\n")
        elif args[:2] == ["task", "asks"]:
            out = "t_ccc33333: Ship 2.4 now or hold for the docs?\n"
        elif args[:1] == ["status"]:
            q = "" if args[-1].startswith("--") else args[-1].lower()
            out = json.dumps({"tasks": [{"task_id": t, **v} for t, v in TASKS.items() if q in v["title"].lower()]})
        elif args[:1] == ["submit"]:
            self.briefs.append(Path(args[args.index("--brief") + 1]).read_text())
            TASKS[self.next_id] = {"title": args[1], "state": "queued"}
            out = f"submitted {self.next_id}\n"
        elif args[:2] in (["task", "answer"], ["task", "cancel"], ["task", "instruct"]):
            out = "ok\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def did(self, *prefix):
        return [c for c in self.calls if [a for a in c if a != "--"][:len(prefix)] == list(prefix)]


class FakeJudge:
    """Stands in for the model. `say` maps a message to the verdict; every prompt is kept to check what
    the model was shown."""

    def __init__(self):
        self.say, self.prompts = {}, []

    def __call__(self, argv, input=None, **kw):
        self.prompts.append(input)
        message = input.split("The message, between the two fence lines")[1]
        for key, v in reversed(self.say.items()):   # the newest line wins
            if key in message:
                if v == "FAIL":   # the model could not say
                    return subprocess.CompletedProcess(argv, 1, "", "overloaded")
                return subprocess.CompletedProcess(argv, 0, json.dumps(v), "")
        return subprocess.CompletedProcess(argv, 0, '{"factory": false}', "")


class FakeSlack:
    """chat_postMessage as the bot: what was posted, where, with which buttons."""

    def __init__(self):
        self.posts, self.n = [], 100

    def chat_postMessage(self, channel, text, thread_ts=None, blocks=None):
        self.n += 1
        ts = f"1700000{self.n}.000100"
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts, "blocks": blocks, "ts": ts})
        return {"ts": ts}

    def reactions_add(self, channel, timestamp, name):
        self.reactions = getattr(self, "reactions", []) + [(channel, timestamp, name)]

    def post_fn(self, channel):
        return lambda text, blocks, thread_ts: self.chat_postMessage(channel, text, thread_ts, blocks)["ts"]


def buttons(post):
    for b in post.get("blocks") or []:
        if b.get("type") == "actions":
            return b["elements"]
    return []


fac, jud, slack = FakeFactory(), FakeJudge(), FakeSlack()
conf = {"repo": "slack-autoreply", "factory_bin": "factory", "converse_url": "", "judge_cmd": "judge"}
own = {STAFF: {"t_ddd44444": {"id": "t_ddd44444", "title": "Login page copy", "state": "queued"}}}
chat = F.Conversation(conf, owner=OWNER, run=fac, judge_run=jud, chooser=lambda prompt: "",
                      own_tasks=lambda u: own.get(u, {}))


def say(text, *, user=OWNER, ts, thread_ts=None, channel=DM):
    got = chat.message(channel=channel, ts=ts, thread_ts=thread_ts, user=user, text=text)
    if got is None:
        return None, None, []
    key, reply = got
    choice = chat.route(key, user, reply)
    before = len(slack.posts)
    F.deliver(reply, key, slack.post_fn(channel), choice["modality"])
    return key, reply, slack.posts[before:]


def press(post, label, user=OWNER):
    el = next(e for e in buttons(post) if e["text"]["text"] == label or e["value"].endswith(f'"{label}"}}'))
    key, value = F.parse_press(el["value"]) or (F.thread_key(post["channel"], post["ts"]), el["value"])
    reply = chat.press(key=key, value=value, user=user)
    if reply is not None:
        F.deliver(reply, key, slack.post_fn(post["channel"]))
    return reply


# 1. An update request: a model reads it, the one matching task's status comes back in the thread.
jud.say["login timeout"] = {"factory": True, "verb": "status", "query": "login timeout"}
key, reply, posts = say("how is the login timeout fix going?", ts="1700000001.000100")
check("update: the status was read from the factory", fac.did("task", "status", "t_aaa11111"), fac.calls)
check("update: the reply is in the same thread", posts and all(p["thread_ts"] == "1700000001.000100" for p in posts),
      posts)
check("update: headline then at most three bullets", posts[0]["text"].count("•") <= 3 and "t_aaa11111" in posts[0]["text"],
      posts[0]["text"])
check("update: the rest goes to the thread as details", len(posts) == 2 and "Details" in posts[1]["text"], posts)
check("update: the judge saw the candidates, never a keyword table", "t_aaa11111: Fix the login timeout" in jud.prompts[-1])

# 2. A new task: submitted with a brief that cites the thread; its updates come to this thread.
jud.say["add dark mode"] = {"factory": True, "verb": "submit", "title": "Dark mode for the dashboard",
                        "brief": "Add a dark mode toggle to the dashboard."}
key2, reply, posts = say("please add dark mode to the dashboard", ts="1700000002.000100")
sub = fac.did("submit")
check("new task: submitted to the factory", sub and sub[0][1] == "Dark mode for the dashboard"
      and sub[0][sub[0].index("--repo") + 1] == "slack-autoreply", sub)
check("new task: the brief cites the Slack thread", "slack.com/archives/DBOT/p1700000002000100" in fac.briefs[0]
      and "dark mode toggle" in fac.briefs[0], fac.briefs[0])
check("new task: the reply names the queued task", "t_eee55555" in reply.headline, reply)
check("new task: the thread is bound to it", F.thread(key2).get("bound") == "t_eee55555")

# 3. Answering an ask by button: a factory push note binds its thread; the press is the answer.
push_ts = "1700000003.000100"
F.bind(F.thread_key(DM, push_ts), "t_ccc33333", principal=OWNER, title="Ship 2.4 now or hold?",
       options=["Ship it", "Hold"])
push = {"channel": DM, "ts": push_ts, "blocks": [{"type": "actions", "elements": [
    {"action_id": "solo_answer_0", "text": {"text": "Ship it"}, "value": "Ship it"},
    {"action_id": "solo_answer_1", "text": {"text": "Hold"}, "value": "Hold"}]}]}
check("ask: a stranger's press does nothing", press(push, "Ship it", user=OTHER) is None and not fac.did("task", "answer"))
reply = press(push, "Ship it")
check("ask: the press answered that task", fac.did("task", "answer", "t_ccc33333", "Ship it"), fac.calls)
check("ask: a second press finds the choice closed", "no longer open" in press(push, "Hold").headline)

# 4. Cancel with confirm: Yes/No buttons first; nothing is cancelled until Yes is pressed.
jud.say["stop the dark mode"] = {"factory": True, "verb": "cancel", "task": "t_eee55555"}
_, reply, posts = say("stop the dark mode task", ts="1700000004.000100", thread_ts="1700000002.000100")
labels = [e["text"]["text"] for e in buttons(posts[0])]
check("cancel: asks Yes/No with real buttons", labels == ["Yes", "No"] and posts[0]["thread_ts"] == "1700000002.000100",
      posts[0])
check("cancel: nothing cancelled before the press", not fac.did("task", "cancel"))
check("cancel: the buttons carry the thread", all(e["action_id"].startswith(F.ACTION_PREFIX) for e in buttons(posts[0])))
reply = press(posts[0], "Yes")
check("cancel: the press is the confirm", fac.did("task", "cancel", "t_eee55555") and "Cancelled" in reply.headline,
      fac.calls)
jud.say["cancel the search"] = {"factory": True, "verb": "cancel", "task": "t_bbb22222"}
_, reply, posts = say("cancel the search ranking task", ts="1700000005.000100")
reply = press(posts[0], "No")
check("cancel: No leaves the task alone", not fac.did("task", "cancel", "t_bbb22222") and "Left" in reply.headline)
n_judge = len(jud.prompts)
jud.say["cancel the release notes"] = {"factory": True, "verb": "cancel", "task": "t_ccc33333"}
_, reply, posts = say("cancel the release notes task", ts="1700000005.000200")
_, reply, _ = say("yes", ts="1700000005.000300", thread_ts="1700000005.000200")
check("cancel: a typed yes is the confirm, not a new message for the model",
      fac.did("task", "cancel", "t_ccc33333") and "Cancelled" in reply.headline and len(jud.prompts) == n_judge + 1,
      (fac.calls[-2:], reply))
check("cli: free text goes after --, never read as a flag", all("--" in c for c in fac.did("task", "cancel"))
      and all("--" in c for c in fac.did("task", "find")), fac.did("task", "cancel"))
check("new task: submitted through staff_intake, with its kind", "--kind" in sub[0], sub)
check("bound: a thread with turns is the factory's", F.bound(F.thread_key(DM, "1700000005.000200")))

# 5. A thread follow-up: "and the other one?" means the thread's other task.
jud.say["login things"] = {"factory": True, "verb": "list", "query": "login"}
k5, reply, posts = say("what are the login things doing?", ts="1700000006.000100")
check("follow-up: the list names both", "t_aaa11111" in posts[0]["text"] and "t_ddd44444" in posts[0]["text"],
      posts[0]["text"])
jud.say["login page copy"] = {"factory": True, "verb": "status", "task": "t_aaa11111"}
say("how is the timeout one?", ts="1700000007.000100", thread_ts="1700000006.000100")
jud.say["timeout one"] = {"factory": True, "verb": "status", "task": "t_aaa11111"}
say("how is the timeout one?", ts="1700000007.000200", thread_ts="1700000006.000100")
jud.say["the other one"] = {"factory": True, "verb": "status", "task": "t_ddd44444"}
_, reply, posts = say("and the other one?", ts="1700000008.000100", thread_ts="1700000006.000100")
shown = jud.prompts[-1]
check("follow-up: the judge saw the thread's turns", "what are the login things doing?" in shown
      and "how is the timeout one?" in shown, shown)
check("follow-up: and the thread's tasks", "t_aaa11111" in shown and "t_ddd44444" in shown, shown)
check("follow-up: answered about the other task", fac.did("task", "status", "t_ddd44444")
      and posts[0]["thread_ts"] == "1700000006.000100", fac.calls)
jud.say["made up"] = {"factory": True, "verb": "status", "task": "t_fffffff9", "query": "zzzz"}
_, reply, _ = say("the made up one", ts="1700000009.000100")
check("judge: a made-up id is never used", not fac.did("task", "status", "t_fffffff9") and "could not find" in reply.headline,
      reply)
check("not factory: left to Solo's normal path", say("lunch at noon?", ts="1700000010.000100")[0] is None)

# 6. A thread reply under a push note goes back to the same task.
push2 = "1700000011.000100"
F.bind(F.thread_key(DM, push2), "t_bbb22222", principal=OWNER, title="Ranking is red", kind="update")
jud.say["try the older model first"] = {"factory": True, "verb": "instruct", "text": "try the older model first"}
_, reply, _ = say("try the older model first", ts="1700000012.000100", thread_ts=push2)
check("push thread: a reply goes back to that task", fac.did("task", "instruct", "t_bbb22222", "try the older model first"),
      fac.calls)
check("push thread: the judge saw the push note it replies to", "factory: t_bbb22222: Ranking is red" in jud.prompts[-1],
      jud.prompts[-1][-600:])
n_instruct = len(fac.did("task", "instruct"))
got = chat.message(channel=DM, ts="1700000012.000200", thread_ts=push2, user=OWNER, text="thanks!")
check("push thread: a bare thanks is taken quietly, not forwarded to the task",
      got and got[1] is F.ACKNOWLEDGED and not F.took(got[1]) and len(fac.did("task", "instruct")) == n_instruct,
      (got, fac.calls[-3:]))
F.bind(F.thread_key(DM, "1700000012.000260"), "t_bbb22222", principal=OWNER, title="Ranking is green")
chat.message(channel=DM, ts="1700000012.000270", thread_ts="1700000012.000260", user=OWNER, text="thanks!")
check("push thread: the acknowledged reply counts as the note being read",
      any(e["type"] == "outcome" and e["piece"] == F.thread(F.thread_key(DM, "1700000012.000260"))["piece"]
          for e in C.events(OWNER)))
check("not factory outside a bound thread is still left to Solo",
      chat.message(channel=DM, ts="1700000012.000250", thread_ts=None, user=OWNER, text="thanks!") is None)
push3 = "1700000012.000400"
F.bind(F.thread_key(DM, push3), "t_ccc33333", principal=OWNER, title="Ship or hold?", options=["Ship it", "Hold"])
n_answer = len(fac.did("task", "answer"))
got = chat.message(channel=DM, ts="1700000012.000500", thread_ts=push3, user=OWNER, text="thanks!")
check("open ask: chit-chat is not sent as its answer; the question is shown again",
      got and "Still waiting" in got[1].headline and "Hold" in got[1].headline and not F.took(got[1])
      and len(fac.did("task", "answer")) == n_answer, (got, fac.calls[-3:]))
jud.say["go ahead once the docs land"] = {"factory": True, "verb": "answer", "text": "go ahead once the docs land"}
say("go ahead once the docs land", ts="1700000012.000600", thread_ts=push3)
check("open ask: an answer in words reaches the task", fac.did("task", "answer", "t_ccc33333", "go ahead once the docs land"),
      fac.calls[-3:])
got = chat.message(channel=DM, ts="1700000012.000700", thread_ts=push3, user=OWNER, text="thanks!")
check("open ask: once answered in words, the question is closed and a thanks is only acknowledged",
      got and got[1] is F.ACKNOWLEDGED and len(fac.did("task", "answer")) == n_answer + 1, (got, fac.calls[-3:]))
jud.say["revert to last week's weights"] = "FAIL"
say("revert to last week's weights", ts="1700000012.000300", thread_ts=push2)
check("push thread: when the judge cannot say, the reply still reaches the task",
      fac.did("task", "instruct", "t_bbb22222", "revert to last week's weights"), fac.calls[-3:])

# 7. Somebody else: read-only, their own tasks only; their requests stay with staff_intake.
jud.say["my login copy"] = {"factory": True, "verb": "status", "task": "t_ddd44444"}
_, reply, _ = say("how is my login copy request?", user=STAFF, ts="1700000013.000100", channel="DSTAFF")
check("non-owner: reads their own task", reply and fac.did("task", "status", "t_ddd44444"))
check("non-owner: shown only their own tasks", "t_aaa11111" not in jud.prompts[-1] and "t_ddd44444" in jud.prompts[-1])
jud.say["ranking for me"] = {"factory": True, "verb": "status", "query": "ranking"}
_, reply, _ = say("what about the ranking for me?", user=STAFF, ts="1700000014.000100", channel="DSTAFF")
check("non-owner: nothing about someone else's task", not fac.did("task", "status", "t_bbb22222")
      and "t_bbb22222" not in repr(reply) and "Search results" not in repr(reply), reply)
jud.say["cancel my"] = {"factory": True, "verb": "cancel", "task": "t_ddd44444"}
check("non-owner: a write goes to staff_intake, not here",
      say("cancel my login copy request", user=STAFF, ts="1700000015.000100", channel="DSTAFF")[0] is None)
jud.say["new report"] = {"factory": True, "verb": "submit", "title": "x"}
check("non-owner: a new request too", say("new report: export is broken", user=STAFF, ts="1700000016.000100",
                                          channel="DSTAFF")[0] is None)

n_judge = len(jud.prompts)
check("non-owner with no tasks: no model call at all", say("how is my thing going?", user=OTHER, ts="1700000016.000200",
                                                          channel="COTHER")[0] is None and len(jud.prompts) == n_judge)

# 8. The learnt channel choice: choice, outcome and correction recorded; the profile changes the next choice.
ev = C.events(OWNER)
choices = [e for e in ev if e["type"] == "choice"]
outcomes = [e for e in ev if e["type"] == "outcome"]
check("channel: every reply's choice is recorded", len(choices) >= 8 and all(e["channel"] for e in choices), len(choices))
check("channel: a press or a reply records the outcome", outcomes and all(e["answered"] for e in outcomes)
      and any(isinstance(e["latency_s"], float) for e in outcomes), outcomes)
jud.say["text me"] = {"factory": True, "verb": "asks", "correction": "text me these on my phone, too long here"}
say("what asks are waiting? text me these on my phone, too long here", ts="1700000017.000100")
corr = [e for e in C.events(OWNER) if e["type"] == "correction"]
check("channel: the correction is recorded verbatim", corr and "text me these" in corr[-1]["said"], corr)


def model(prompt):
    # a stand-in chooser that weighs the profile it is given, as the real one is asked to
    if "correction:" in prompt and "text me" in prompt:
        return '{"channel": "sms", "modality": "short_text", "why": "asked for texts"}'
    return '{"channel": "slack", "modality": "structured_text", "why": "at the desk"}'


first = C.choose(OWNER, kind="update", content="2.4 shipped", urgency=2, model=model)
check("channel: before learning, the model picks Slack", first["channel"] == "slack", first)
C.learn()
prof = C.profile(OWNER)
check("channel: the nightly pass builds a profile in the store", prof.get("by_route") and prof.get("corrections")
      and (HOME / C.PROFILES).exists(), prof)
again = C.choose(OWNER, kind="update", content="2.4 shipped", urgency=2, model=model)
check("channel: the learnt profile changes the next choice", again["channel"] == "sms"
      and again["modality"] == "short_text", again)
seen = []
only = C.choose(OWNER, kind="update", content="x", available=("slack",),
                model=lambda p: seen.append(p) or '{"channel": "sms", "modality": "short_text"}')
check("channel: only what the caller can send on is offered or taken", only["channel"] == "slack"
      and "Channels: slack." in seen[0], (only, seen[0][:200]))
check("channel: a model that fails keeps the default", C.choose(OWNER, kind="ask", content="x",
                                                                model=lambda p: "nonsense")["channel"] == "slack")

# The chooser is held per person and kind for a while: one model call per conversation, not per reply.
calls = []
def counted(prompt):
    calls.append(prompt)
    return '{"channel": "slack", "modality": "short_text", "why": "quick"}'
clock = [1000.0]
C._clock = lambda: clock[0]
C.forget()
for n in range(3):
    C.choose(STAFF, kind="update", content=f"reply {n}", model=counted)
check("chooser: replies in a row pay one model call", len(calls) == 1, len(calls))
C.choose(STAFF, kind="ask", content="pick one", model=counted)
check("chooser: another kind of piece is its own choice", len(calls) == 2, len(calls))
clock[0] += C.CHOICE_TTL_S + 1
C.choose(STAFF, kind="update", content="later", model=counted)
check("chooser: the held choice runs out", len(calls) == 3, len(calls))
C.record_correction(STAFF, "too long, just a line")
C.choose(STAFF, kind="update", content="after the correction", model=counted)
check("chooser: a correction asks the model again at once", len(calls) == 4, len(calls))
check("chooser: and the model hears it before the nightly pass", "too long, just a line" in calls[-1], calls[-1][-400:])
C.learn(STAFF)
C.choose(STAFF, kind="update", content="after learning", model=counted)
check("chooser: a newly learnt profile asks the model again", len(calls) == 5, len(calls))
C.choose(STAFF, kind="update", content="held", model=counted)
store = json.loads((HOME / C.PROFILES).read_text())
store[STAFF]["learnt_at"] = "2099-01-01T00:00:00+00:00"   # the nightly pass, in its own process
(HOME / C.PROFILES).write_text(json.dumps(store))
C.choose(STAFF, kind="update", content="after another process learnt", model=counted)
check("chooser: a profile learnt by another process is read at once", len(calls) == 6, len(calls))
C.choose(STAFF, kind="update", content="other model", model=lambda p: counted(p))
check("chooser: another model is its own choice", len(calls) == 7, len(calls))
clock[0] += C.CHOICE_TTL_S + 1
C.choose(STAFF, kind="ask", content="late", model=counted)
check("chooser: run-out choices are dropped", all(clock[0] - at < C.CHOICE_TTL_S for at, _ in C._choices.values()),
      len(C._choices))
n = len(calls)
C.choose(STAFF, kind="confirm", content="x", model=lambda p: calls.append(p) or "nonsense")
C.choose(STAFF, kind="confirm", content="x", model=lambda p: calls.append(p) or "nonsense")
check("chooser: a fallback is not held", len(calls) == n + 2, len(calls))
C._clock = time.monotonic
C.forget()

# 9. The daemon wiring: a console message reaches the factory and the reply lands in its thread.
os.environ.setdefault("SLACK_USER_TOKEN", "xoxp-test")
(HOME / F.CONF_PATH).write_text(json.dumps({**conf, "factory_bin": "factory"}))
import slackctl  # noqa: E402

bot = FakeSlack()
stub = type("Stub", (), {})()
stub.__dict__.update(me=OWNER, bot=bot, bot_dm=DM, name_of=lambda u: u, _allowed=lambda *a: True,
                     state=slackctl.State())
stub._factory_answered = lambda *a, **k: D._factory_answered(stub, *a, **k)
D = slackctl.Daemon
stub.factory_chat = lambda: D.factory_chat(stub)
stub._factory_post = lambda ch: D._factory_post(stub, ch)
stub.__dict__["_factory_chat"] = F.Conversation(F.load_conf(), owner=OWNER, run=fac, judge_run=jud,
                                                chooser=lambda p: "")
ok = D.on_factory_chat(stub, {"user": OWNER, "channel": DM, "ts": "1700000020.000100",
                              "text": "how is the login timeout fix going?"})
check("daemon: the console message was answered in its thread", ok and bot.posts
      and bot.posts[0]["thread_ts"] == "1700000020.000100" and bot.posts[0]["channel"] == DM, bot.posts)
check("daemon: an owner message outside the console is not taken", not D.on_factory_chat(
    stub, {"user": OWNER, "channel": "CGENERAL", "ts": "1700000021.000100", "text": "how is the login timeout fix going?"}))
D.on_factory_press(stub, DM, push_ts, OWNER, {"action_id": "solo_answer_1", "value": "Hold"})
check("daemon: a press on a closed ask is answered, not re-sent", "no longer open" in bot.posts[-1]["text"], bot.posts[-1])

check("daemon: a staff DM to the owner is not answered by the bot, which is not in it", not D.on_factory_chat(
    stub, {"user": STAFF, "channel": "DSTAFF", "channel_type": "im", "ts": "1700000022.000100",
           "text": f"<@{OWNER}> how is my login copy request?"}))

# 10. A factory push through deliver_to_owner is an ask record too: the owner's press lands on the item.
os.environ["NOTE_SHAPER"] = "off"
console = type("Console", (), {})()
console.__dict__.update(console=DM, me=OWNER, _ping=lambda t: None, mention_id=lambda h: None,
                        console_post=lambda text, blocks=None, thread_ts=None: bot.chat_postMessage(DM, text, thread_ts, blocks)["ts"])
item = {"id": "say-push-1", "factory_task": "t_ccc33333", "origin": "agent:release",
        "note": {"status": "ask", "headline": "Ship 2.4 now or hold?", "options": ["Ship it", "Hold"]}}
slackctl.save_item(item)
ts, _ = slackctl.deliver_to_owner(console, item, "Ship 2.4 now or hold for the docs?")
asks = slackctl.State().read().get("asks") or {}
check("push: recorded as an ask and bound to its task", asks.get(ts, {}).get("item") == "say-push-1"
      and F.thread(F.thread_key(DM, ts)).get("bound") == "t_ccc33333", asks)
shapes, real_deliver = [], F.deliver
stub.__dict__["_factory_chat"].chooser = lambda p: '{"channel": "slack", "modality": "short_text"}'
F.deliver = lambda reply, key, post, modality="structured_text", **k: (shapes.append(modality),
                                                                         real_deliver(reply, key, post, modality, **k))[1]
D.on_factory_press(stub, DM, ts, OWNER, {"action_id": "solo_answer_1", "value": "Hold"})
F.deliver = real_deliver
check("press: the reply takes the chooser's modality", shapes == ["short_text"], shapes)
check("push: the press answered the task and is kept for `solo answer`",
      fac.did("task", "answer", "t_ccc33333", "Hold") and slackctl.load_item("say-push-1").get("answer", {}).get("choice") == "Hold",
      slackctl.load_item("say-push-1"))

item = {"id": "say-push-3", "factory_task": "t_bbb22222", "origin": "agent:release",
        "note": {"status": "ok", "headline": "Ranking fix is live."}}
slackctl.save_item(item)
ts3, _ = slackctl.deliver_to_owner(console, item, "Ranking fix is live.")
n_posts, n_instruct = len(bot.posts), len(fac.did("task", "instruct"))
jud.say["thanks, nice"] = {"factory": False}
ok = D.on_factory_chat(stub, {"user": OWNER, "channel": DM, "ts": "1700000025.000100", "thread_ts": ts3,
                              "text": "thanks, nice"})
check("console: a thanks under a factory note is taken, nothing posted, nothing forwarded, no answer recorded",
      ok is True and (DM, "1700000025.000100", "eyes") in getattr(bot, "reactions", []) and len(bot.posts) == n_posts and len(fac.did("task", "instruct")) == n_instruct
      and not slackctl.load_item("say-push-3").get("answer"), (ok, bot.posts[n_posts:], slackctl.load_item("say-push-3")))

# 11. Review round 2: the factory's own choices go back to it; every piece gets its outcome; chat off binds nothing.
import channel_choice  # noqa: E402

c = F.Conversation(conf, owner=OWNER, run=fac, judge_run=jud, chooser=lambda p: "")
said_to_factory = []
answers = [{"reply": {"status": "ask", "headline": "Which environment?", "options": ["staging", "prod"]}},
           {"reply": {"status": "ask", "headline": "Now or after the batch?", "options": ["Now", "Later"]}},
           {"reply": {"status": "ok", "headline": "Deploying to staging now."}}]
c.factory.converse = lambda payload: (said_to_factory.append(payload), answers[len(said_to_factory) - 1])[1]
k = F.thread_key(DM, "1700000030.000100")
r1 = c._converse(k, {"verb": "instruct", "task": "t_aaa11111", "text": "deploy it"}, user=OWNER, text="deploy it",
                 read_only=False)
c.route(k, OWNER, r1)
p1 = F.thread(k)["piece"]
n_answer = len(fac.did("task", "answer"))
r2 = c.press(key=k, value="staging", user=OWNER)
check("converse choice: the press goes back to the factory as the next turn, not as a task id",
      said_to_factory[-1]["text"] == "staging" and said_to_factory[-1]["intent"]["task"] == "t_aaa11111"
      and r2.headline == "Now or after the batch?" and len(fac.did("task", "answer")) == n_answer, said_to_factory)
c.route(k, OWNER, r2)
p2 = F.thread(k)["piece"]
r3 = c.press(key=k, value="Now", user=OWNER)
outs = {e["piece"] for e in channel_choice.events(OWNER) if e.get("type") == "outcome"}
check("channel: every piece in a thread gets its own outcome", p1 != p2 and {p1, p2} <= outs and r3.status == "ok",
      (p1, p2, outs))
check("took: a closed choice or a refusal is not an answer", not F.took(c.press(key=k, value="Now", user=OWNER))
      and not F.took(F.Reply("failed", "no")) and F.took(r3))

conf_file = HOME / F.CONF_PATH
saved_conf = conf_file.read_text()
conf_file.unlink()
item = {"id": "say-push-2", "factory_task": "t_bbb22222", "origin": "agent:release",
        "note": {"status": "ok", "headline": "Ranking fix is live."}}
slackctl.save_item(item)
ts2, _ = slackctl.deliver_to_owner(console, item, "Ranking fix is live.")
check("chat off: a factory push is not bound, and an informational one asks nothing",
      not F.thread(F.thread_key(DM, ts2)).get("bound") and ts2 not in (slackctl.State().read().get("asks") or {}))
conf_file.write_text(saved_conf)

stub.__dict__["_factory_chat"] = F.Conversation(F.load_conf(), owner=OWNER, run=fac, judge_run=jud,
                                                chooser=lambda p: "", own_tasks=lambda u: own.get(u, {}))
stub._factory_post = lambda ch: (lambda *a, **k: 1 / 0)
jud.say["login copy request"] = {"factory": True, "verb": "status", "task": "t_ddd44444"}
n_status = len(fac.did("task", "status", "t_ddd44444"))
check("non-owner: a reply that cannot be posted leaves the message to staff_intake", D.on_factory_chat(
    stub, {"user": STAFF, "channel": "CSTAFF", "ts": "1700000031.000100",
           "text": f"<@{OWNER}> how is my login copy request?"}) is False
      and len(fac.did("task", "status", "t_ddd44444")) > n_status)

print(f"\n{passed} passed")
