"""A whole Slack thread, read once: the root and every reply up to the newest.

conversations.replies pages from the oldest reply, so one call with limit=200 returns the start
of a long thread and misses its newest messages, the ones nearest the ask. `replies` follows the
cursor to the end, up to MAX_PAGES pages (10,000 replies): only a thread longer than that still
misses its newest replies.
"""
from __future__ import annotations

import logging

log = logging.getLogger("slack_thread")

PAGE = 200
MAX_PAGES = 50  # 10,000 replies: a bound on calls, not a thread length seen in practice


def replies(client, channel: str, thread_ts: str, *, page: int = PAGE, max_pages: int = MAX_PAGES) -> list[dict]:
    """The thread's messages oldest first, root first, each message once (by ts)."""
    pages: list[list[dict]] = []
    cursor = None
    for _ in range(max_pages):
        kw = {"cursor": cursor} if cursor else {}
        res = client.conversations_replies(channel=channel, ts=thread_ts, limit=page, **kw)
        pages.append(res.get("messages") or [])
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    else:
        log.warning("thread read stopped at %d pages: its newest replies are missing", max_pages)
    out: list[dict] = []
    seen: set[str] = set()
    for m in [m for p in pages for m in p]:
        ts = m.get("ts")
        if ts and ts in seen:  # Slack repeats the root at the top of later pages
            continue
        seen.add(ts)
        out.append(m)
    return out
