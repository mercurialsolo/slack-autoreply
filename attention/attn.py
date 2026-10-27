#!/usr/bin/env python3
"""attn — get the owner's attention on their desktop, with a priority the sender decides.

Slack and email are channels: they wait to be read. A one-time code, or a decision that blocks
everything behind it, cannot wait to be read — it has to interrupt, stay on screen until it is
dealt with, and show how long it has. That is the whole job of this file. The sender says what
and how urgent; this decides how it looks.

    attn ask    "<title>" [--body …] --priority p0|p1|p2 [--deadline 90s|5m] [--reply text|choice|file --choices A,B] [--image f.png] [--from solo] [--wait]
    attn fetch  <id> --out <path>                            the file the owner answered with
    attn notify "<title>" [--body …] [--priority p2]        fire and forget
    attn status [<id>]                                       what is open, or one ask's state
    attn cancel <id>                                         the sender no longer needs it
    attn serve                                               the daemon, on the owner's desktop
    attn doctor                                              is this machine set up to send / receive

Priorities are rendering policy, owned here, not by the sender:
    p0  blocks something now: a modal on top of everything, the screen blurred, a countdown to the
        deadline, a sound until answered, re-shown if closed. Always has a "Can't right now" so the
        owner is never trapped — that answer is recorded as `declined`, which is still an answer.
    p1  needs a decision soon: on top, with a countdown, one sound, shown again every few minutes
        until answered or the deadline passes.
    p2  worth knowing: a normal notification, nothing persistent.

The sender lives anywhere on the tailnet; the daemon runs where the owner sits. HTTP with a bearer
token, because tailscale already says who is on the network and the token says who may interrupt.

Answers can be secrets (a TOTP comes back this way): asks are 0600 files, the log never carries an
answer, and `--wait` prints the answer to stdout only.

Config (0600), ~/.attention/env, plain KEY=value; the environment wins over the file:
    ATTN_TOKEN           shared bearer token, both ends (required)
    ATTN_URL             sender side: where the daemon is, e.g. http://100.85.22.1:9911
    ATTN_BIND            daemon side: address to bind (default: this machine's tailscale IPv4, else 127.0.0.1)
    ATTN_PORT            daemon side: port (default 9911)
    ATTN_FALLBACK_CMD    sender side: run with the summary when the daemon is unreachable (e.g. `node-notify blocked`)
    ATTN_ESCALATE_CMD    daemon side: run with the summary when a p0 is half way to its deadline unanswered
    ATTN_RENDERER        daemon side: command that shows one ask (default: this file's `render`); tests use a fake
    ATTN_NAG_MINUTES     daemon side: how often a p1 is shown again (default 5)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(os.environ.get("ATTN_HOME") or Path.home() / ".attention")
ASKS = HOME / "asks"
LOG = HOME / "attn.log"
ENV_FILE = HOME / "env"
PRIORITIES = ("p0", "p1", "p2")
DEFAULT_DEADLINE = {"p0": 300, "p1": 1800, "p2": 0}
TERMINAL = ("answered", "declined", "expired", "cancelled", "shown")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".pdf"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024
EXIT = {"answered": 0, "shown": 0, "declined": 3, "expired": 4, "cancelled": 5, "unreachable": 6}
SOUND = "/System/Library/Sounds/Basso.aiff"


# ------------------------------------------------------------------ config


def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def cfg(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    """One line per event. Never an answer, never a body — titles only."""
    HOME.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{now_iso()} {msg}\n")


def parse_duration(s: str | None, default: int) -> int:
    if not s:
        return default
    m = re.fullmatch(r"(\d+)\s*([smh]?)", s.strip())
    if not m:
        sys.exit(f"--deadline {s!r}: use 90s, 5m or 1h")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600}[unit]


# ------------------------------------------------------------------ asks on disk


def ask_path(ask_id: str) -> Path:
    if not re.fullmatch(r"ask-[0-9a-f]{12}", ask_id):
        raise ValueError(f"not an ask id: {ask_id}")
    return ASKS / f"{ask_id}.json"


def save_ask(ask: dict) -> None:
    ASKS.mkdir(parents=True, exist_ok=True)
    p = ask_path(ask["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ask, indent=1), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)


def load_ask(ask_id: str) -> dict | None:
    p = ask_path(ask_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def open_asks() -> list[dict]:
    out = []
    for p in sorted(ASKS.glob("ask-*.json")) if ASKS.exists() else []:
        a = json.loads(p.read_text(encoding="utf-8"))
        if a.get("state") == "open":
            out.append(a)
    return out


def new_ask(title: str, body: str, priority: str, frm: str, deadline_s: int, reply: str, choices: list[str]) -> dict:
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}")
    if reply not in ("none", "text", "choice", "file"):
        raise ValueError("reply must be none, text, choice or file")
    if reply == "choice" and len(choices) < 2:
        raise ValueError("a choice needs at least two --choices")
    created = datetime.now().replace(microsecond=0)
    return {"id": f"ask-{uuid.uuid4().hex[:12]}", "title": title.strip()[:120], "body": (body or "").strip()[:2000],
            "priority": priority, "from": (frm or "unknown")[:40], "reply": reply, "choices": choices,
            "created": created.isoformat(),
            "deadline": (created + timedelta(seconds=deadline_s)).isoformat() if deadline_s else None,
            "state": "open", "answer": None, "answered_at": None, "shown": 0, "images": [], "answer_file": None}


def attach_images(ask: dict, images: list[dict]) -> None:
    """Images the owner should see with the ask (a CAPTCHA, a screenshot): written beside it, 0600."""
    total = 0
    for n, img in enumerate(images[:6]):
        ext = Path(img.get("name") or "image.png").suffix.lower() or ".png"
        if ext not in IMAGE_EXT:
            raise ValueError(f"image {img.get('name')!r}: not an image type ({', '.join(sorted(IMAGE_EXT))})")
        data = base64.b64decode(img.get("b64") or "")
        total += len(data)
        if total > MAX_IMAGE_BYTES:
            raise ValueError(f"images exceed {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
        ASKS.mkdir(parents=True, exist_ok=True)
        path = ASKS / f"{ask['id']}-img{n}{ext}"
        path.write_bytes(data)
        path.chmod(0o600)
        ask["images"].append(str(path))


def keep_answer_file(ask: dict, chosen: str) -> str | None:
    """The owner answered with a file: copy it beside the ask so the sender can fetch it later."""
    src = Path(chosen).expanduser()
    if not src.is_file():
        return None
    dest = ASKS / f"{ask['id']}-answer{src.suffix.lower()[:16]}"
    shutil.copyfile(src, dest)
    dest.chmod(0o600)
    return str(dest)


def remaining(ask: dict) -> int | None:
    if not ask.get("deadline"):
        return None
    return max(0, int((datetime.fromisoformat(ask["deadline"]) - datetime.now()).total_seconds()))


def finish(ask: dict, state: str, answer: str | None = None) -> dict:
    """Terminal states are final: the first one written wins."""
    current = load_ask(ask["id"]) or ask
    if current["state"] != "open":
        return current
    current["state"], current["answer"], current["answered_at"] = state, answer, now_iso()
    save_ask(current)
    log(f"{current['id']} {state} ({current['priority']}, from {current['from']}): {current['title']}")
    return current


# ------------------------------------------------------------------ daemon


class Daemon:
    """Owns the open asks: one renderer per ask, re-shown while it is open, closed on cancel."""

    def __init__(self) -> None:
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def start(self, ask: dict) -> None:
        save_ask(ask)
        log(f"{ask['id']} open ({ask['priority']}, from {ask['from']}): {ask['title']}")
        threading.Thread(target=self._run, args=(ask["id"],), daemon=True).start()

    def cancel(self, ask_id: str) -> dict | None:
        ask = load_ask(ask_id)
        if not ask:
            return None
        ask = finish(ask, "cancelled")
        self._kill(ask_id)
        return ask

    def _kill(self, ask_id: str) -> None:
        with self.lock:
            p = self.procs.pop(ask_id, None)
        if p and p.poll() is None:
            p.terminate()

    def _run(self, ask_id: str) -> None:
        """Show the ask until something terminal happens.

        p0 is shown again the moment it closes without an answer, because a modal the owner can
        quit is not a modal. p1 waits ATTN_NAG_MINUTES between showings. p2 is shown once.
        """
        escalated = False
        while True:
            ask = load_ask(ask_id)
            if not ask or ask["state"] != "open":
                return
            left = remaining(ask)
            if left == 0:
                finish(ask, "expired")
                return
            if ask["priority"] == "p0" and not escalated and ask.get("deadline"):
                total = (datetime.fromisoformat(ask["deadline"]) - datetime.fromisoformat(ask["created"])).total_seconds()
                if left is not None and left <= total / 2 and ask["shown"] > 0:
                    escalated = True
                    self._escalate(ask)
            result = self._show(ask)
            if result is None:  # cancelled while on screen
                return
            state, answer = result
            if state in ("answered", "declined"):
                if state == "answered" and ask["reply"] == "file":
                    kept = keep_answer_file(ask, answer or "")
                    if not kept:
                        log(f"{ask['id']} answered with a file that does not exist: shown again")
                        continue
                    current = load_ask(ask_id) or ask
                    current["answer_file"] = kept
                    save_ask(current)
                    answer = Path(kept).name
                finish(ask, state, answer)
                return
            if ask["priority"] == "p2":
                finish(ask, "shown")
                return
            # closed, timed out, or the renderer died: show it again
            wait = 1 if ask["priority"] == "p0" else 60 * float(cfg("ATTN_NAG_MINUTES", "5"))
            left = remaining(ask)
            if left is not None and left <= wait:
                finish(ask, "expired")
                return
            time.sleep(wait)

    def _show(self, ask: dict) -> tuple[str, str | None] | None:
        ask["shown"] += 1
        save_ask(ask)
        renderer = shlex.split(cfg("ATTN_RENDERER") or f"{shlex.quote(sys.executable)} {shlex.quote(__file__)} render")
        cmd = renderer + ["--ask", str(ask_path(ask["id"]))]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            log(f"{ask['id']} renderer failed to start: {e}")
            return ("closed", None)
        with self.lock:
            self.procs[ask["id"]] = p
        out, err = p.communicate()
        with self.lock:
            self.procs.pop(ask["id"], None)
        if (load_ask(ask["id"]) or {}).get("state") == "cancelled":
            return None
        try:
            r = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except (json.JSONDecodeError, IndexError):
            r = {}
        if r.get("state") in ("answered", "declined"):
            return r["state"], r.get("answer")
        if err.strip():
            log(f"{ask['id']} renderer: {err.strip()[-200:]}")
        return ("closed", None)

    def _escalate(self, ask: dict) -> None:
        cmd = cfg("ATTN_ESCALATE_CMD")
        if not cmd:
            return
        log(f"{ask['id']} escalating: half way to the deadline unanswered")
        try:
            subprocess.run(shlex.split(cmd) + [f"[{ask['priority']}] {ask['title']} — {remaining(ask)}s left"],
                           timeout=30, capture_output=True, check=False)
        except Exception as e:  # noqa: BLE001 - escalation is best effort
            log(f"{ask['id']} ATTN_ESCALATE_CMD failed: {e}")


def make_handler(daemon: Daemon, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet; attn.log is the record
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if self.headers.get("Authorization", "") == f"Bearer {token}":
                return True
            self._send(401, {"error": "bad token"})
            return False

        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                return self._send(200, {"ok": True, "open": len(open_asks())})
            if not self._authed():
                return
            m = re.fullmatch(r"/ask/(ask-[0-9a-f]{12})", self.path)
            if m:
                ask = load_ask(m.group(1))
                return self._send(200, ask) if ask else self._send(404, {"error": "no such ask"})
            m = re.fullmatch(r"/ask/(ask-[0-9a-f]{12})/file", self.path)
            if m:
                ask = load_ask(m.group(1))
                if not ask or not ask.get("answer_file") or not Path(ask["answer_file"]).is_file():
                    return self._send(404, {"error": "no file on this ask"})
                data = Path(ask["answer_file"]).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Attn-Filename", Path(ask["answer_file"]).name)
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/asks":
                return self._send(200, {"open": open_asks()})
            self._send(404, {"error": "no such route"})

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "body is not JSON"})
            if self.path == "/ask":
                try:
                    ask = new_ask(data.get("title") or "", data.get("body") or "", data.get("priority") or "p1",
                                  data.get("from") or "", int(data.get("deadline_s") or 0), data.get("reply") or "none",
                                  list(data.get("choices") or []))
                except (ValueError, TypeError) as e:
                    return self._send(400, {"error": str(e)})
                if not ask["title"]:
                    return self._send(400, {"error": "a title is required"})
                try:
                    attach_images(ask, list(data.get("images") or []))
                except (ValueError, TypeError, OSError) as e:
                    return self._send(400, {"error": f"images: {e}"})
                daemon.start(ask)
                return self._send(201, ask)
            m = re.fullmatch(r"/ask/(ask-[0-9a-f]{12})/cancel", self.path)
            if m:
                ask = daemon.cancel(m.group(1))
                return self._send(200, ask) if ask else self._send(404, {"error": "no such ask"})
            self._send(404, {"error": "no such route"})

    return Handler


def tailscale_ip() -> str:
    for binary in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        try:
            p = subprocess.run([binary, "ip", "-4"], capture_output=True, text=True, timeout=5)
            ip = (p.stdout or "").strip().splitlines()
            if p.returncode == 0 and ip:
                return ip[0]
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


def cmd_serve(a) -> None:
    token = cfg("ATTN_TOKEN")
    if not token:
        sys.exit(f"ATTN_TOKEN is not set — put it in {ENV_FILE} (0600). Make one: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
    bind = a.bind or cfg("ATTN_BIND") or tailscale_ip() or "127.0.0.1"
    port = int(a.port or cfg("ATTN_PORT") or 9911)
    daemon = Daemon()
    # Asks left open by a previous daemon are shown again, not forgotten.
    for ask in open_asks():
        daemon.start(ask)
    srv = ThreadingHTTPServer((bind, port), make_handler(daemon, token))
    log(f"serving on {bind}:{port}")
    print(f"attn: serving on http://{bind}:{port}  (asks in {ASKS})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------ renderers (the daemon's side)


def dialog_bin() -> str:
    return shutil.which("dialog") or ("/usr/local/bin/dialog" if os.path.exists("/usr/local/bin/dialog") else "")


def cmd_render(a) -> None:
    """Show one ask and print its result as JSON: {"state": answered|declined|closed, "answer": …}.

    swiftDialog when installed (on top, blur, countdown, text field); AppleScript's display dialog
    otherwise, which is uglier and has no live countdown but needs nothing installed. p2 is a
    Notification Center banner.
    """
    ask = json.loads(Path(a.ask).read_text(encoding="utf-8"))
    if ask["priority"] == "p2":
        _notify_banner(ask)
        print(json.dumps({"state": "closed"}))
        return
    stop = threading.Event()
    threading.Thread(target=_sound_loop, args=(ask, stop), daemon=True).start()
    try:
        result = _render_swiftdialog(ask) if dialog_bin() else _render_applescript(ask)
    finally:
        stop.set()
    print(json.dumps(result))


def _sound_loop(ask: dict, stop: threading.Event) -> None:
    every = 30 if ask["priority"] == "p0" else None
    while not stop.is_set():
        if os.path.exists(SOUND):
            subprocess.run(["afplay", SOUND], capture_output=True, check=False)
        if every is None or stop.wait(every):
            return


def _deadline_line(ask: dict) -> str:
    left = remaining(ask)
    if left is None:
        return ""
    mins, secs = divmod(left, 60)
    return f"\n\nNeeded within {mins}m {secs:02d}s" if mins else f"\n\nNeeded within {secs}s"


def _render_swiftdialog(ask: dict) -> dict:
    left = remaining(ask)
    cmd = [dialog_bin(), "--title", f"[{ask['priority'].upper()}] {ask['title']}",
           "--message", (ask["body"] or " ") + f"\n\nfrom {ask['from']}",
           "--ontop", "--moveable", "--json", "--button1text", "Send" if ask["reply"] != "none" else "Done",
           "--button2text", "Can't right now", "--icon", "warning" if ask["priority"] == "p0" else "caution"]
    if left:
        cmd += ["--timer", str(left)]  # the bar is the countdown
    if ask["priority"] == "p0":
        cmd += ["--blurscreen", "--big"]
    if ask["reply"] == "text":
        cmd += ["--textfield", "Answer,required"]
    elif ask["reply"] == "choice":
        cmd += ["--selecttitle", "Choice,required", "--selectvalues", ",".join(ask["choices"])]
    elif ask["reply"] == "file":
        cmd += ["--textfield", "File,fileselect,required"]
    for img in ask.get("images") or []:
        cmd += ["--image", img]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 2:
        return {"state": "declined"}
    if p.returncode != 0:
        return {"state": "closed"}
    try:
        out = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        out = {}
    if ask["reply"] == "text":
        return {"state": "answered", "answer": str(out.get("Answer", "")).strip()}
    if ask["reply"] == "file":
        return {"state": "answered", "answer": str(out.get("File", "")).strip()}
    if ask["reply"] == "choice":
        sel = out.get("Choice") or out.get("SelectedOption") or {}
        return {"state": "answered", "answer": sel.get("selectedValue") if isinstance(sel, dict) else str(sel)}
    return {"state": "answered"}


def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_applescript(ask: dict) -> dict:
    """display dialog cannot show a picture, so the images open in Preview first, beside it."""
    left = remaining(ask)
    if ask.get("images"):
        subprocess.run(["open", "-a", "Preview", *ask["images"]], capture_output=True, check=False)
    title = _applescript_str(f"[{ask['priority'].upper()}] {ask['title']}")
    body = _applescript_str((ask["body"] or "") + f"\n\nfrom {ask['from']}" + _deadline_line(ask))
    give_up = f" giving up after {left}" if left else ""
    icon = "stop" if ask["priority"] == "p0" else "caution"
    if ask["reply"] == "file":
        script = f"POSIX path of (choose file with prompt {body})"
    elif ask["reply"] == "choice" and len(ask["choices"]) <= 3:
        buttons = ", ".join(_applescript_str(c) for c in ask["choices"])
        script = f"display dialog {body} with title {title} buttons {{{buttons}}} with icon {icon}{give_up}"
    elif ask["reply"] == "choice":
        items = ", ".join(_applescript_str(c) for c in ask["choices"])
        script = f"choose from list {{{items}}} with title {title} with prompt {body} OK button name \"Send\" cancel button name \"Can't right now\""
    else:
        answer = ' default answer ""' if ask["reply"] == "text" else ""
        ok = "Send" if ask["reply"] == "text" else "Done"
        script = (f"display dialog {body} with title {title}{answer} buttons {{\"Can't right now\", \"{ok}\"}} "
                  f"default button \"{ok}\" cancel button \"Can't right now\" with icon {icon}{give_up}")
    full = f'tell application "System Events" to activate\n{script}'
    p = subprocess.run(["osascript", "-e", full], capture_output=True, text=True)
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        # -128 is the cancel button: that is the owner saying not now, not a failure.
        return {"state": "declined"} if "-128" in (p.stderr or "") else {"state": "closed"}
    if ask["reply"] == "file":
        return {"state": "answered", "answer": out}
    if ask["reply"] == "choice" and len(ask["choices"]) > 3:
        return {"state": "declined"} if out == "false" else {"state": "answered", "answer": out}
    if "gave up:true" in out:
        return {"state": "closed"}
    m = re.search(r"button returned:([^,]*)", out)
    button = (m.group(1) if m else "").strip()
    if button == "Can't right now":
        return {"state": "declined"}
    if ask["reply"] == "choice":
        return {"state": "answered", "answer": button}
    if ask["reply"] == "text":
        m = re.search(r"text returned:(.*)$", out)
        return {"state": "answered", "answer": (m.group(1) if m else "").strip()}
    return {"state": "answered"}


def _notify_banner(ask: dict) -> None:
    title = f"[{ask['priority'].upper()}] {ask['title']}"
    msg = (ask["body"] or f"from {ask['from']}")[:200]
    tn = shutil.which("terminal-notifier")
    if tn:
        subprocess.run([tn, "-title", title, "-message", msg, "-sound", "default"], capture_output=True, check=False)
        return
    subprocess.run(["osascript", "-e", f"display notification {_applescript_str(msg)} with title {_applescript_str(title)}"],
                   capture_output=True, check=False)


# ------------------------------------------------------------------ sender side


def _request(method: str, path: str, data: dict | None = None, timeout: float = 10) -> dict:
    url, token = cfg("ATTN_URL").rstrip("/"), cfg("ATTN_TOKEN")
    if not url or not token:
        sys.exit(f"ATTN_URL and ATTN_TOKEN must be set — in {ENV_FILE} or the environment")
    req = urllib.request.Request(url + path, method=method, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or e.reason
        except json.JSONDecodeError:
            why = e.reason
        sys.exit(f"attn: the daemon refused it ({e.code}): {why}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Unreachable(f"{url}: {getattr(e, 'reason', e)}") from e


class Unreachable(Exception):
    pass


def _fallback(summary: str, why: str) -> None:
    cmd = cfg("ATTN_FALLBACK_CMD")
    if cmd:
        try:
            subprocess.run(shlex.split(cmd) + [summary], timeout=30, capture_output=True, check=False)
            print(f"attn: desktop unreachable ({why}) — sent through ATTN_FALLBACK_CMD instead", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            print(f"attn: desktop unreachable ({why}) and ATTN_FALLBACK_CMD failed: {e}", file=sys.stderr)
    else:
        print(f"attn: desktop unreachable ({why}) and no ATTN_FALLBACK_CMD — nobody was told", file=sys.stderr)


def cmd_ask(a) -> None:
    priority = a.priority or ("p2" if a.cmd == "notify" else "p1")
    deadline_s = parse_duration(a.deadline, DEFAULT_DEADLINE[priority]) if priority in DEFAULT_DEADLINE else 0
    reply = "none" if a.cmd == "notify" else (a.reply or ("choice" if a.choices else "none"))
    choices = [c.strip() for c in (a.choices or "").split(",") if c.strip()]
    images = []
    for path in a.image or []:
        f = Path(path).expanduser()
        if not f.is_file():
            sys.exit(f"--image {path}: no such file")
        if f.suffix.lower() not in IMAGE_EXT:
            sys.exit(f"--image {path}: not an image ({', '.join(sorted(IMAGE_EXT))})")
        images.append({"name": f.name, "b64": base64.b64encode(f.read_bytes()).decode()})
    payload = {"title": a.title, "body": a.body or "", "priority": priority, "from": a.from_ or "",
               "deadline_s": deadline_s, "reply": reply, "choices": choices, "images": images}
    summary = f"[{priority}] {a.title}" + (f" — {a.body[:120]}" if a.body else "")
    try:
        ask = _request("POST", "/ask", payload)
    except Unreachable as e:
        _fallback(summary, str(e))
        sys.exit(EXIT["unreachable"])
    if not a.wait:
        print(json.dumps({"id": ask["id"], "state": ask["state"], "deadline": ask["deadline"]}))
        return
    while ask["state"] == "open":
        time.sleep(1.0)
        try:
            ask = _request("GET", f"/ask/{ask['id']}")
        except Unreachable as e:
            print(f"attn: lost the daemon while waiting ({e})", file=sys.stderr)
            sys.exit(EXIT["unreachable"])
    if ask["state"] == "answered" and ask["reply"] == "file" and a.out:
        _fetch(ask["id"], a.out)
    print(json.dumps({"id": ask["id"], "state": ask["state"], "answer": ask.get("answer"),
                      **({"file": a.out} if ask["state"] == "answered" and ask["reply"] == "file" and a.out else {})}))
    sys.exit(EXIT.get(ask["state"], 1))


def _fetch(ask_id: str, out: str) -> None:
    url, token = cfg("ATTN_URL").rstrip("/"), cfg("ATTN_TOKEN")
    req = urllib.request.Request(f"{url}/ask/{ask_id}/file", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        sys.exit(f"attn: no file to fetch for {ask_id} ({e.code})")
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"attn: {url}: {getattr(e, 'reason', e)}")
    dest = Path(out).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    dest.chmod(0o600)


def cmd_fetch(a) -> None:
    _fetch(a.id, a.out)
    print(json.dumps({"id": a.id, "file": a.out}))


def cmd_cancel(a) -> None:
    try:
        ask = _request("POST", f"/ask/{a.id}/cancel")
    except Unreachable as e:
        sys.exit(f"attn: {e}")
    print(f"{ask['id']} {ask['state']}")


def cmd_status(a) -> None:
    try:
        if a.id:
            ask = _request("GET", f"/ask/{a.id}")
            print(json.dumps({k: ask.get(k) for k in ("id", "state", "priority", "from", "title", "deadline", "answered_at")}))
            return
        asks = _request("GET", "/asks")["open"]
    except Unreachable as e:
        sys.exit(f"attn: {e}")
    if not asks:
        print("nothing waiting on the owner")
    for ask in asks:
        left = remaining(ask)
        print(f"{ask['id']}  {ask['priority']}  {ask['from']:<12} {ask['title'][:60]}" + (f"  ({left}s left)" if left is not None else ""))


def cmd_doctor(a) -> None:
    rows = []
    rows.append(("ATTN_TOKEN", "set" if cfg("ATTN_TOKEN") else f"MISSING — put it in {ENV_FILE}"))
    if ENV_FILE.exists():
        mode = ENV_FILE.stat().st_mode & 0o777
        rows.append(("env file mode", f"{oct(mode)}" + ("" if mode == 0o600 else " — should be 0600: chmod 600 " + str(ENV_FILE))))
    rows.append(("renderer", "swiftDialog " + dialog_bin() if dialog_bin() else "AppleScript display dialog (no countdown bar) — brew install --cask swiftdialog for the real thing"))
    rows.append(("banners", "terminal-notifier" if shutil.which("terminal-notifier") else "osascript display notification"))
    rows.append(("tailscale ip", tailscale_ip() or "none found — the daemon will bind 127.0.0.1 unless ATTN_BIND is set"))
    url = cfg("ATTN_URL")
    if url:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
                h = json.loads(r.read() or b"{}")
            rows.append(("daemon", f"reachable at {url} ({h.get('open', 0)} open)"))
        except Exception as e:  # noqa: BLE001
            rows.append(("daemon", f"UNREACHABLE at {url}: {getattr(e, 'reason', e)}"))
    else:
        rows.append(("ATTN_URL", "not set — this machine can serve but not send"))
    rows.append(("fallback", cfg("ATTN_FALLBACK_CMD") or "none — an unreachable desktop tells nobody"))
    for k, v in rows:
        print(f"{k:<14} {v}")


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> None:
    load_env()
    ap = argparse.ArgumentParser(prog="attn", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ask", "notify"):
        p = sub.add_parser(name)
        p.add_argument("title")
        p.add_argument("--body", default="")
        p.add_argument("--priority", choices=PRIORITIES)
        p.add_argument("--deadline", help="90s, 5m, 1h (default: p0 5m, p1 30m)")
        p.add_argument("--from", dest="from_", default=os.environ.get("ATTN_FROM", ""))
        p.add_argument("--reply", choices=("none", "text", "choice", "file"))
        p.add_argument("--choices", help="comma-separated, with --reply choice")
        p.add_argument("--image", action="append", help="show this image with the ask (png/jpg/gif/webp/heic/pdf, repeatable)")
        p.add_argument("--out", help="with --reply file and --wait: save the owner's file here")
        p.add_argument("--wait", action="store_true", help="block until answered, declined, expired or cancelled")
        p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("cancel"); p.add_argument("id"); p.set_defaults(fn=cmd_cancel)
    p = sub.add_parser("fetch"); p.add_argument("id"); p.add_argument("--out", required=True); p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("status"); p.add_argument("id", nargs="?"); p.set_defaults(fn=cmd_status)
    p = sub.add_parser("serve"); p.add_argument("--bind"); p.add_argument("--port"); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("render"); p.add_argument("--ask", required=True); p.set_defaults(fn=cmd_render)
    p = sub.add_parser("doctor"); p.set_defaults(fn=cmd_doctor)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
