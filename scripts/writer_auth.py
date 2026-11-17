"""Which Claude account the writer's `claude -p` bills, leased from the factory pool.

On 17 Nov Solo stopped sending: every compose and every `solo ask` came back 429. The mini's
~/.claude/settings.json pointed `apiKeyHelper` at a pool helper that had picked an account at 100%
of its week. The helper was fixed and a plain `claude -p` worked again, but the writer still got a
429. Two things made the writer's auth something other than what the owner's shell saw:

- An env token beats `apiKeyHelper`. Claude Code takes ANTHROPIC_AUTH_TOKEN, then ANTHROPIC_API_KEY,
  then `apiKeyHelper`, then CLAUDE_CODE_OAUTH_TOKEN (headless). The daemon loads
  ~/.slack-autoreply/.env and the launchd env into os.environ, and `writer_env` kept every one of them.
  A `setup-token` left there pins the writer to one account, whatever the helper picks.
- The helper caches its token for five minutes. One full account is enough to make every run in
  that window fail, with nothing to move it on.

So the writer takes a lease the way factory workers do: `cluster pool lease solo-reply --reserved`.
The pool skips accounts that are week-full, 5h-full or cooling down, and a lease released as
`limit_hit` puts its account into cooldown. The lease's token reaches `claude` as an `apiKeyHelper`
in `--settings`, which ranks above the user's settings, with the env tokens that would beat it
removed. The config dir stays the owner's ~/.claude, where the plugin lives (the 11-06 incident),
so only the auth changes.

A run that comes back rate-limited releases its lease as `limit_hit` and tries the next account,
at most WRITER_POOL_TRIES times. No cluster CLI, WRITER_POOL=off, or an empty pool all fall back to
the auth the host already had, unchanged.

One job: choose the account and fail over. Running `claude` and reading its output stay with the
callers (slackctl's writer and `ask`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, TypeVar

log = logging.getLogger("autoreply")

HOLDER = "solo-reply"   # the pool's name for Solo's reply seats; `--reserved` lets it take them

# Every env var that outranks `apiKeyHelper`, plus the headless OAuth token that would win if the
# helper ever failed. ANTHROPIC_API_KEY is in this list too, but `writer_env` already owns that one
# (DISPATCH_USE_API_KEY keeps it on purpose).
OVERRIDING_TOKENS = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")

# What a refused request looks like, from the API and from Claude Code's own wording.
RATE_LIMIT_RE = re.compile(r"\b429\b|rate_limit_error|rate.limited|usage limit|hit your (?:\w+ )?limit|"
                           r"extra usage is required", re.I)

_local = threading.local()


@dataclass
class Lease:
    label: str
    lease_id: str
    helper: str          # the apiKeyHelper command that prints this account's token

    def settings_args(self) -> list[str]:
        return ["--settings", json.dumps({"apiKeyHelper": self.helper})]


def mode() -> str:
    """on | off | auto (the default: on when the cluster CLI is installed)."""
    m = (os.getenv("WRITER_POOL") or "auto").strip().lower()
    return m if m in ("on", "off", "auto") else "auto"


def cluster_bin() -> str | None:
    b = (os.getenv("CLUSTER_BIN") or "").strip()
    if b:
        return b if (os.path.exists(b) or shutil.which(b)) else None
    return shutil.which("cluster")


def enabled() -> bool:
    return mode() != "off" and cluster_bin() is not None


def tries() -> int:
    try:
        return max(1, int(os.getenv("WRITER_POOL_TRIES") or 3))
    except ValueError:
        return 3


def is_rate_limited(text: str) -> bool:
    return bool(RATE_LIMIT_RE.search(text or ""))


def _cluster(*args: str, timeout: int = 30) -> subprocess.CompletedProcess | None:
    b = cluster_bin()
    if not b:
        return None
    try:
        return subprocess.run([b, "pool", *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("writer pool: `cluster pool %s` failed: %s", args[0], e)
        return None


def lease(minutes: int) -> Lease | None:
    """One account from the pool, or None (no CLI, pool empty or exhausted, no token file)."""
    if not enabled():
        return None
    p = _cluster("lease", HOLDER, "--reserved", "--minutes", str(max(1, minutes)))
    if p is None:
        return None
    if p.returncode != 0:
        log.warning("writer pool: no lease (%s) — running on this host's own login",
                    (p.stderr or p.stdout or "").strip()[:200])
        return None
    try:
        r = json.loads((p.stdout or "").strip().splitlines()[-1])
        args = r.get("cli_args") or []
        helper = json.loads(args[args.index("--settings") + 1]).get("apiKeyHelper") if "--settings" in args else None
    except (ValueError, IndexError, AttributeError, TypeError):
        log.warning("writer pool: unreadable lease: %s", (p.stdout or "")[:200])
        return None
    if not r.get("lease_id"):
        return None
    if not helper:
        # Without a token file the account can only be reached through its own config dir, which
        # would lose the owner's plugin. Hand it back.
        log.warning("writer pool: %s has no token file — releasing it", r.get("label"))
        release(Lease(r.get("label") or "?", r["lease_id"], ""), "ok")
        return None
    return Lease(r.get("label") or "?", r["lease_id"], helper)


def release(ls: Lease | None, reason: str, text: str = "") -> None:
    if ls is None:
        return
    args = ["release", ls.lease_id, reason] + (["--text", text[:300]] if text else [])
    p = _cluster(*args)
    if p is not None and p.returncode != 0:
        log.warning("writer pool: release of %s failed: %s", ls.label, (p.stderr or "").strip()[:200])


def current() -> Lease | None:
    """The lease this thread's `claude` call runs under, if any."""
    return getattr(_local, "lease", None)


@contextmanager
def _holding(ls: Lease | None) -> Iterator[None]:
    prev = current()
    _local.lease = ls
    try:
        yield
    finally:
        _local.lease = prev


def apply(cmd: list[str], env: dict) -> tuple[list[str], dict]:
    """The command and env with this thread's lease in charge of auth. No lease: unchanged."""
    ls = current()
    if ls is None:
        return cmd, env
    env = dict(env)
    for k in OVERRIDING_TOKENS + ("ANTHROPIC_API_KEY",):
        env.pop(k, None)
    return list(cmd) + ls.settings_args(), env


T = TypeVar("T")


def run_leased(run: Callable[[], T], said: Callable[[T], str], minutes: int,
               may_retry: Callable[[T], bool] = lambda _r: True) -> T:
    """Call `run` under a pool lease, failing over to the next account while it is rate-limited.

    `said` turns a result into the text to look for a rate limit in. `may_retry` says whether a
    rate-limited result is safe to run again (a writer that already decided is not). With no lease
    to be had, `run` is called once, under the host's own auth."""
    out: T
    n = tries()
    for attempt in range(n):
        ls = lease(minutes)
        try:
            with _holding(ls):
                out = run()
        except BaseException:
            release(ls, "failed", "the run raised")
            raise
        text = said(out) or ""
        limited = is_rate_limited(text)
        release(ls, "limit_hit" if limited else "ok", text if limited else "")
        if ls is None or not limited or not may_retry(out):
            return out
        log.warning("writer pool: %s is rate-limited (%s) — %s", ls.label, text[:120],
                    "trying the next account" if attempt + 1 < n else f"giving up after {n} accounts")
    return out


def auth_source(env: dict | None = None) -> str:
    """Which credential a plain `claude -p` under `env` would use, for `solo doctor`. Never the value."""
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY (DISPATCH_USE_API_KEY: billed to the API, no pool lease)"
    if enabled():
        dropped = [k for k in OVERRIDING_TOKENS if env.get(k)]
        return (f"factory pool lease ({HOLDER}, reserved) via --settings apiKeyHelper"
                + (f"; {', '.join(dropped)} dropped for leased runs" if dropped else ""))
    if env.get("ANTHROPIC_AUTH_TOKEN"):
        return "ANTHROPIC_AUTH_TOKEN from the environment (one fixed account; beats apiKeyHelper)"
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "CLAUDE_CODE_OAUTH_TOKEN from the environment (one fixed account unless settings set apiKeyHelper)"
    return "Claude Code settings (apiKeyHelper if set, else the /login subscription)"
