"""attachments: which files a handled message carries, fetching them with the bot token, and the
model input they become — without a name or a byte of them reaching the log, and never forwarded."""
from __future__ import annotations

import io
import shutil
import json
import logging
import os
import stat
import sys
import struct
import time
import types
import zipfile
import zlib

import offline_harness as H  # noqa: E402  (sets the environment first)
from offline_harness import TMP, STATE, FakeClient, check  # noqa: E402
import attachments as A  # noqa: E402
import slack_thread  # noqa: E402
import slackctl as S  # noqa: E402
from slack_sdk.errors import SlackApiError  # noqa: E402

C, MATTHEW = "CPRODUCT", "UMATTHEW"


def png(w: int, h: int) -> bytes:
    """A real PNG: a white canvas with three bars, standing in for the chart in Matthew's thread."""
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            bar = any(x0 <= x < x0 + w // 8 and y >= h - (k + 1) * h // 4 for k, x0 in enumerate((w // 8, 3 * w // 8, 5 * w // 8)))
            row += b"\x33\x66\xcc" if bar else b"\xff\xff\xff"
        rows.append(b"\x00" + bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


CHART = png(240, 160)
CSV = b"week,signups,activations\n2026-11-02,120,48\n2026-11-09,134,61\n"
_dx = io.BytesIO()
with zipfile.ZipFile(_dx, "w") as z:
    z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Q4 plan &amp; goals</w:t></w:r></w:p>"
                                    "<w:p><w:r><w:t>Ship charts</w:t></w:r></w:p></w:body></w:document>")
DOCX = _dx.getvalue()

FILES = {  # id → (files.info object, bytes the download returns)
    "FCHART": ({"id": "FCHART", "name": "format-example.png", "mimetype": "image/png", "filetype": "png", "size": len(CHART)}, CHART),
    "FCSV": ({"id": "FCSV", "name": "signups.csv", "mimetype": "text/csv", "filetype": "csv", "size": len(CSV)}, CSV),
    "FDOC": ({"id": "FDOC", "name": "plan.docx", "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
              "filetype": "docx", "size": len(DOCX)}, DOCX),
    "FZIP": ({"id": "FZIP", "name": "bundle.zip", "mimetype": "application/zip", "filetype": "zip", "size": 2048}, b"PK" + b"\0" * 2046),
    "FPDF": ({"id": "FPDF", "name": "deck.pdf", "mimetype": "application/pdf", "filetype": "pdf", "size": 9}, b"%PDF-1.4\n"),
}
for _f, _ in FILES.values():
    _f["url_private_download"] = f"https://files.slack.test/{_f['id']}/download"


class FakeBot(FakeClient):
    token = "xoxb-test-bot"

    def __init__(self, error=None):
        super().__init__()
        self.error, self.infos = error, []

    def files_info(self, file):
        self.infos.append(file)
        if self.error:
            raise SlackApiError("refused", {"ok": False, "error": self.error})
        return {"file": dict(FILES[file][0])}


DOWNLOADS: list[tuple[str, str]] = []


def download(url, token, max_bytes, seconds_left=None):
    DOWNLOADS.append((url, token))
    fid = url.split("/")[-2]
    if fid == "FHTML":
        return 200, "text/html; charset=utf-8", b"<html>sign in</html>"
    data = FILES[fid][1]
    return (200, "application/octet-stream", None) if len(data) > max_bytes else (200, "application/octet-stream", data)


def clear_cache():
    """Forget every fetched file, so the next fetch downloads again."""
    shutil.rmtree(A.ROOT / A.FILES_DIR, ignore_errors=True)


def ev_files(*ids):
    return [{"id": i, "name": FILES[i][0]["name"], "mimetype": FILES[i][0]["mimetype"],
             "filetype": FILES[i][0]["filetype"], "size": FILES[i][0]["size"]} for i in ids]


# ------------------------------------------------------------------ which files
msgs = [{"user": MATTHEW, "files": ev_files("FCHART")},
        {"user": "UOTHER", "files": ev_files("FCSV", "FCHART")},
        {"user": "UOTHER", "files": [{"id": "FGONE", "mode": "tombstone"}]},
        {"user": "UOTHER", "files": ev_files("FDOC", "FZIP", "FPDF")}]
g = A.gather(msgs)
check("gather: the handled message's files first, each once, deleted ones skipped",
      [f["id"] for f in g] == ["FCHART", "FCSV", "FDOC", "FZIP", "FPDF"] and g[0]["_from"] == MATTHEW, str([f["id"] for f in g]))
check("gather: capped, keeping the files nearest the ask", [f["id"] for f in A.gather(msgs, limit=2)] == ["FCHART", "FCSV"])
check("kinds: image, text, docx, other, pdf", [A.kind_of(f) for f in g] == ["image", "text", "docx", "other", "pdf"])

# ------------------------------------------------------------------ fetching
nb = A.fetch(ev_files("FCHART"), "C1-1_0", [], download=download)
check("no bot token: described, not fetched, and the note says what to add",
      nb[0]["path"] is None and "SLACK_BOT_TOKEN" in nb[0]["note"] and "files:read" in nb[0]["note"] and not DOWNLOADS)

ns = A.fetch(ev_files("FCHART"), "C1-2_0", [(FakeBot("missing_scope"), "xoxb-test-bot")], download=download)
check("bot without files:read: the note names the scope and the reinstall", ns[0]["path"] is None and "files:read" in ns[0]["note"]
      and "reinstall" in ns[0]["note"] and not DOWNLOADS, ns[0]["note"])

na = A.fetch(ev_files("FCHART"), "C1-3_0", [(FakeBot("file_not_found"), "xoxb-test-bot")], download=download)
check("a file the bot cannot see: says so (invite the bot)", na[0]["path"] is None and "invite the bot" in na[0]["note"])

FILES["FHTML"] = ({"id": "FHTML", "name": "x.png", "mimetype": "image/png", "filetype": "png", "size": 10,
                   "url_private_download": "https://files.slack.test/FHTML/download"}, b"")
nh = A.fetch([{"id": "FHTML", "name": "x.png", "mimetype": "image/png", "filetype": "png", "size": 10}], "C1-4_0",
             [(FakeBot(), "xoxb-test-bot")], download=download)
check("Slack's sign-in page instead of the file: not saved as the image", nh[0]["path"] is None and "invite" in nh[0]["note"])

class FakeOwner(FakeBot):
    token = "xoxp-test-owner"


DOWNLOADS.clear()
dm = A.fetch(ev_files("FCSV"), "D1-1_0", [(FakeBot("file_not_found"), "xoxb-test-bot"), (FakeOwner(), "xoxp-test-owner")],
             download=download)
check("a DM file the bot cannot see: read with the owner's token when it can", dm[0]["path"] and DOWNLOADS[-1][1] == "xoxp-test-owner")
clear_cache()
dm2 = A.fetch(ev_files("FCSV"), "D1-2_0", [(FakeBot("file_not_found"), "xoxb-test-bot"), (FakeOwner("missing_scope"), "xoxp-test-owner")],
              download=download)
check("…and when neither can, the bot's reason is the one given", dm2[0]["path"] is None and dm2[0]["note"] == A.NO_ACCESS)

FILES["FPOST"] = ({"id": "FPOST", "name": "notes", "mimetype": "text/html", "filetype": "post", "size": 20,
                   "url_private_download": "https://files.slack.test/FPOST/download"}, b"<p>meeting notes</p>")
_dl = download


def download(url, token, max_bytes, seconds_left=None):  # noqa: F811 — a Slack Post really is served as text/html
    if "/FPOST/" in url:
        return 200, "text/html", FILES["FPOST"][1]
    return _dl(url, token, max_bytes)


post = A.fetch([dict(FILES["FPOST"][0])], "C1-7_0", [(FakeBot(), "xoxb-test-bot")], download=download)
check("a Slack Post (served as text/html) is read, not taken for the sign-in page", post[0]["path"] and post[0].get("text_path"))

big = dict(ev_files("FZIP")[0], size=50 * 1024 * 1024)
DOWNLOADS.clear()
nbig = A.fetch([big], "C1-5_0", [(FakeBot(), "xoxb-test-bot")], download=download)
check("over the size limit: described by size, never downloaded", nbig[0]["path"] is None and "limit" in nbig[0]["note"] and not DOWNLOADS)
nstream = A.fetch(ev_files("FCSV"), "C1-6_0", [(FakeBot(), "xoxb-test-bot")], download=download, max_bytes=10)
check("…and a file that grows past the limit while downloading is dropped", nstream[0]["path"] is None and "limit" in nstream[0]["note"])

logs = io.StringIO()
_h = logging.StreamHandler(logs)
A.log.addHandler(_h)
A.log.setLevel(logging.INFO)
bot = FakeBot()
clear_cache()
DOWNLOADS.clear()
recs = A.fetch(g, "CPRODUCT-1763869260_000100", [(bot, bot.token)], download=download)
A.log.removeHandler(_h)
by = {r["id"]: r for r in recs}
check("fetched with the bot token, every file", [t for _, t in DOWNLOADS] == ["xoxb-test-bot"] * 5 and bot.infos == list(by))
folder = A.file_dir("FCHART")
dirs = [str(A.file_dir(r["id"])) for r in recs]
check("stored in a private folder per Slack file id (0700, files 0600)",
      all(stat.S_IMODE(os.stat(x).st_mode) == 0o700 for x in dirs) and stat.S_IMODE(A.ROOT.stat().st_mode) == 0o700
      and stat.S_IMODE((A.ROOT / A.FILES_DIR).stat().st_mode) == 0o700
      and all(stat.S_IMODE(os.stat(r["path"]).st_mode) == 0o600 for r in recs) and folder.resolve().is_relative_to(STATE.resolve())
      and all(os.path.dirname(r["path"]) == x for r, x in zip(recs, dirs)))
check("the image as saved is the image sent", open(by["FCHART"]["path"], "rb").read() == CHART)
log_text = logs.getvalue()
check("the log carries counts and kinds only — no names, no content",
      "5 of 5 fetched" in log_text and "0 of them kept" in log_text and not any(s in log_text for s in ("format-example", "signups", "2026-11-02", "plan.docx", "Ship charts")), log_text)

# ------------------------------------------------------------------ model input
blocks = A.image_blocks(recs)
check("image input: one base64 PNG block for the chart",
      len(blocks) == 1 and blocks[0]["source"]["media_type"] == "image/png"
      and A.base64.b64decode(blocks[0]["source"]["data"]) == CHART)
check("CSV: extracted as text", "2026-11-09,134,61" in open(by["FCSV"]["text_path"]).read())
check("docx: extracted as text, entities decoded", open(by["FDOC"]["text_path"]).read().splitlines() == ["Q4 plan & goals", "Ship charts"])
check("other files: name, type and size only", by["FZIP"]["path"] and "text_path" not in by["FZIP"]
      and "bundle.zip (zip, 2 KB)" in A.triage_lines(recs)[3])
check("PDF without pypdf or pdftotext: the writer is told to Read it",
      ("text_path" in by["FPDF"]) or "Read the file" in (by["FPDF"]["note"] or ""))

tl = A.triage_lines(recs)
check("triage lines: the chart is shown as an image, the CSV's text is quoted",
      "format-example.png (image" in tl[0] and "shown as an image" in tl[0] and "signups,activations" in tl[1], str(tl))
sl = "\n".join(A.show_lines(recs))
check("show lines: the path to Read for the image, and a never-forward warning",
      f"Read this path to see the image: {by['FCHART']['path']}" in sl and "never forward" in sl and "Ship charts" in sl, sl)
check("read_dirs: the folder of each fetched file, only when something was fetched",
      A.read_dirs({"id": "CPRODUCT-1763869260_000100", "attachments": recs}) == dirs
      and A.read_dirs({"id": "C1-1_0", "attachments": nb}) == [] and A.read_dirs({"id": "x"}) == [])

# 4 MB is ~5.3 MB once base64-encoded, over the 5 MB image-input limit; these bytes are no image Pillow
# can open, so with or without Pillow they cannot be made to fit.
check("an image too large for image input: not sent as input, the writer can Read it",
      A.fit_image(b"\0" * (4 * 1024 * 1024), "image/png") is None)
check("an image within the limits is sent as it is", A.fit_image(CHART, "image/png") == (CHART, "image/png"))
try:
    from PIL import Image as _Image
except ImportError:
    _Image = None
if _Image is not None:  # Pillow is installed: a long edge past IMAGE_MAX_EDGE is scaled down to it
    _wide = A.fit_image(png(2000, 40), "image/png")
    check("an image past the edge limit is resized by Pillow",
          _wide is not None and max(_Image.open(io.BytesIO(_wide[0])).size) <= A.IMAGE_MAX_EDGE, str(_wide and _wide[1]))

# ------------------------------------------------------------------ never forwarded
try:
    S.attach_files([by["FCHART"]["path"]])
    forwarded = True
except SystemExit as e:
    forwarded, why = False, str(e)
check("say --file refuses a fetched attachment", not forwarded and "never forwards" in why, why if not forwarded else "")

# ------------------------------------------------------------------ the cache: a file is downloaded once
logs = io.StringIO()
_h = logging.StreamHandler(logs)
A.log.addHandler(_h)
bot2 = FakeBot()
DOWNLOADS.clear()
again = A.fetch(g, "CPRODUCT-1763869300_000100", [(bot2, bot2.token)], download=download)
A.log.removeHandler(_h)
# Every downloaded file is kept, one that could not be prepared too (with the tools it was tried with).
kept = [r["id"] for r in recs if r["path"]]
check("the next message in the thread: every fetched file from the cache, no download, no files.info",
      len(kept) >= 4 and not DOWNLOADS and not bot2.infos
      and [r["path"] for r in again] == [r["path"] for r in recs], str(DOWNLOADS))
check("…the cached record keeps the extracted text", again[1].get("text_path") == by["FCSV"]["text_path"])
check("…and the log says how many came from the cache, no names", f"{len(kept)} of them kept from before" in logs.getvalue()
      and "signups" not in logs.getvalue(), logs.getvalue())
check("a cached file is used even without a bot token", A.fetch(ev_files("FCSV"), "C1-9_0", [], download=download)[0]["path"]
      == by["FCSV"]["path"])
half = A.ROOT / A.FILES_DIR / "FHALF"  # a fetch cut short: the file is there, its record is not
half.mkdir()
check("a folder without its record is not a cache hit", A._cached("FHALF") is None)

clear_cache()
DOWNLOADS.clear()
late = A.fetch(ev_files("FCHART", "FCSV"), "C1-8_0", [(FakeBot(), "xoxb-test-bot")], download=download, budget_s=0)
check("out of time: nothing downloaded, and the note says Solo tries again",
      not DOWNLOADS and all(r["path"] is None and "out of time" in r["note"] for r in late), str(late))
now = [0.0]  # each download takes 100 s of a 10 s budget


def took_long(url, token, max_bytes, seconds_left=None):
    now[0] += 100.0
    return download(url, token, max_bytes)


DOWNLOADS.clear()
part = A.fetch(ev_files("FCHART", "FCSV"), "C1-8_1", [(FakeBot(), "xoxb-test-bot")], download=took_long, budget_s=10,
               clock=lambda: now[0])
check("…the budget covers all of a message's files: the first is fetched, the next is not once time is up",
      part[0]["path"] and part[1]["path"] is None and "out of time" in part[1]["note"] and len(DOWNLOADS) == 1, str(part))
check("an out-of-time file is not cached: it downloads next time", A._cached("FCSV") is None)

DOWNLOADS.clear()
nbig2 = A.fetch([big], "C1-8_2", [(FakeBot(), "xoxb-test-bot")], download=download, budget_s=0)
check("a file over the size limit says so even when time is up (it would never be fetched)",
      "limit" in nbig2[0]["note"] and "out of time" not in nbig2[0]["note"], nbig2[0]["note"])


def slow(url, token, max_bytes, seconds_left=None):
    DOWNLOADS.append((url, token))
    raise TimeoutError("cut off")


DOWNLOADS.clear()
cut = A.fetch(ev_files("FCSV"), "C1-8_3", [(FakeBot(), "xoxb-test-bot"), (FakeBot(), "xoxp-test-owner")], download=slow)
check("a download cut off by the time bound: the owner's token gets no fresh go, the note says out of time",
      len(DOWNLOADS) == 1 and "out of time" in cut[0]["note"], str(cut[0]["note"]))
now[0] = 0.0


def refused_slowly(url, token, max_bytes, seconds_left=None):
    DOWNLOADS.append((url, token))
    now[0] += 100.0
    return 403, "text/plain", b""


DOWNLOADS.clear()
spent = A.fetch(ev_files("FCSV"), "C1-8_5", [(FakeBot(), "xoxb-test-bot"), (FakeBot(), "xoxp-test-owner")],
                download=refused_slowly, budget_s=10, clock=lambda: now[0])
check("…a second token is tried only with time left: none once the first spent it",
      len(DOWNLOADS) == 1 and "out of time" in spent[0]["note"], str(spent[0]["note"]))
_left = []
A.fetch(ev_files("FCSV"), "C1-8_4", [(FakeBot(), "xoxb-test-bot")], budget_s=10, clock=lambda: 3.0,
        download=lambda u, t, m, seconds_left=None: (_left.append(seconds_left), download(u, t, m))[1])
check("…and each download is handed the time left of the message's budget", _left == [10.0], str(_left))


class SlowStream:
    """httpx.stream for a download that trickles: a chunk every 50 ms, for ever."""
    status_code, headers = 200, {"content-type": "text/csv"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        while True:
            time.sleep(0.05)
            yield b"x"


_httpx = sys.modules.get("httpx")
class _Timeout(Exception):
    pass


def _stalled(*a, **k):
    raise _Timeout("read timed out")


sys.modules["httpx"] = types.SimpleNamespace(stream=lambda *a, **k: SlowStream(), TimeoutException=_Timeout)
try:
    t0 = time.monotonic()
    try:
        A.http_download("https://files.slack.test/x", "xoxb-test-bot", 10**6, seconds_left=0.2)
        stopped = False
    except TimeoutError:
        stopped = True
    check("http_download stops a trickling download when the time left runs out", stopped and time.monotonic() - t0 < 2)
    sys.modules["httpx"].stream = _stalled
    try:
        A.http_download("https://files.slack.test/x", "xoxb-test-bot", 10**6, seconds_left=5)
        stalled = None
    except Exception as e:  # noqa: BLE001
        stalled = e
    check("…an httpx timeout (a stalled server) counts as out of time too", isinstance(stalled, TimeoutError), repr(stalled))
finally:
    if _httpx is None:
        sys.modules.pop("httpx", None)
    else:
        sys.modules["httpx"] = _httpx

clear_cache()
csv1 = A.fetch(ev_files("FCSV"), "C1-10_0", [(FakeBot(), "xoxb-test-bot")], download=download)
DOWNLOADS.clear()
edited = dict(ev_files("FCSV")[0], size=FILES["FCSV"][0]["size"] + 5)
A.fetch([edited], "C1-10_1", [(FakeBot(), "xoxb-test-bot")], download=download)
check("a file edited in Slack since (a new size or edit time) is downloaded again, not served stale",
      len(DOWNLOADS) == 1, str(DOWNLOADS))
DOWNLOADS.clear()
A.fetch([dict(ev_files("FCSV")[0], updated=1763869999)], "C1-10_2", [(FakeBot(), "xoxb-test-bot")], download=download)
check("…the edit time alone counts too", len(DOWNLOADS) == 1, str(DOWNLOADS))
DOWNLOADS.clear()
A.fetch([{"id": "FCSV", "name": "signups.csv"}], "C1-10_3", [(FakeBot(), "xoxb-test-bot")], download=download)
check("…a file object without edit time or size (a stub) is served from the cache", not DOWNLOADS, str(DOWNLOADS))
_rec = A.file_dir("FCSV") / "record.json"
_old = json.loads(_rec.read_text())
_old.pop("version", None)
_rec.write_text(json.dumps(_old))
check("…and so is a record kept before versions were stored", A._cached("FCSV", A.version(FILES["FCSV"][0])) is not None)

clear_cache()
_fit = A.fit_image
A.fit_image = lambda data, mt: None  # as without Pillow, for an image over the input limit
try:
    noimg = A.fetch(ev_files("FCHART"), "C1-11_0", [(FakeBot(), "xoxb-test-bot")], download=download)
finally:
    A.fit_image = _fit
check("an image that could not be prepared is kept, with the tools it was tried with",
      noimg[0]["path"] and "Pillow" in noimg[0]["note"] and (A._cached("FCHART") or {}).get("tools") == A.tools(),
      str(noimg[0]["note"]))
DOWNLOADS.clear()
same = A.fetch(ev_files("FCHART"), "C1-11_1", [(FakeBot(), "xoxb-test-bot")], download=download)
check("…the same tools: served as it was, no download, not prepared again", not DOWNLOADS and same[0]["note"] == noimg[0]["note"])
_tools = A.tools
A.tools = lambda: _tools() + ["a-new-tool"]
try:
    again_img = A.fetch(ev_files("FCHART"), "C1-11_2", [(FakeBot(), "xoxb-test-bot")], download=download)
finally:
    A.tools = _tools
check("…a tool installed since: prepared again from disk, no download",
      not DOWNLOADS and again_img[0]["note"] is None and again_img[0].get("model_type")
      and A._cached("FCHART").get("tools") is None, str(again_img[0]))


# ------------------------------------------------------------------ a long thread, read to its end
class PagedClient(FakeClient):
    """conversations.replies in pages of two, the root repeated at the top of each, as Slack does."""

    def __init__(self, msgs):
        super().__init__()
        self.msgs, self.calls = msgs, []

    def conversations_replies(self, channel, ts, limit=200, cursor=None, **kw):
        self.calls.append(cursor)
        start = int(cursor or 1)
        page = [self.msgs[0]] + self.msgs[start:start + 2]
        nxt = str(start + 2) if start + 2 < len(self.msgs) else ""
        return {"messages": page, "response_metadata": {"next_cursor": nxt}}


long_thread = [{"user": "U1", "ts": f"1763869000.{i:06d}", "text": f"m{i}"} for i in range(7)]
pc = PagedClient(long_thread)
got = slack_thread.replies(pc, C, long_thread[0]["ts"])
check("replies: every page read, the root once, oldest first", got == long_thread and pc.calls == [None, "3", "5"],
      f"{[m['ts'] for m in got]} {pc.calls}")
pc = PagedClient(long_thread)
_tlog = io.StringIO()
_th = logging.StreamHandler(_tlog)
slack_thread.log.addHandler(_th)
try:
    _short = slack_thread.replies(pc, C, long_thread[0]["ts"], max_pages=2)
finally:
    slack_thread.log.removeHandler(_th)
check("replies: stops at max_pages", len(_short) == 5 and len(pc.calls) == 2)
check("…and logs that the newest replies are missing", "stopped at 2 pages" in _tlog.getvalue(), _tlog.getvalue())

# ------------------------------------------------------------------ the daemon: Matthew's #product ask
d = S.Daemon("none")
d.bot = FakeBot()
S.attachments.fetch.__defaults__  # the module the daemon uses is this one
_real_dl = A.http_download
A.http_download = download
A.fetch.__kwdefaults__["download"] = download
ask = {"type": "message", "user": MATTHEW, "channel": C, "channel_type": "channel", "ts": "1763869260.000100",
       "text": "<@UME> create a chart with last month's signups in the format of the attached image",
       "files": ev_files("FCHART"), "subtype": "file_share"}
d.client.history[C] = [{k: ask[k] for k in ("user", "ts", "text", "files")}]
clear_cache()
DOWNLOADS.clear()
d.on_event(ask)
item = S.load_item("CPRODUCT-1763869260_000100")
att = item.get("attachments") or []
check("on_inbound: the attached chart is fetched with the bot token and kept on the item",
      len(att) == 1 and att[0]["kind"] == "image" and att[0]["path"] and DOWNLOADS == [(FILES["FCHART"][0]["url_private_download"], "xoxb-test-bot")],
      str(att))
check("…the item carries a record, not the image bytes", "data" not in str(item) and len(str(item)) < 20000)

clear_cache()  # a cached file needs no token; this one was never fetched
d2 = S.Daemon("none")  # no bot token
d2.on_event(dict(ask, ts="1763869261.000100"))
it2 = S.load_item("CPRODUCT-1763869261_000100")
check("on_inbound without a bot: the file is described with what to add", "SLACK_BOT_TOKEN" in (it2["attachments"][0]["note"] or ""))

# A reply far down a long thread: the file is on the newest page, and the thread is read once for
# the transcript and the files both.
root_ts = "1763869400.000100"
reply = {"type": "message", "user": MATTHEW, "channel": C, "channel_type": "channel", "ts": "1763869400.000900",
         "thread_ts": root_ts, "text": "<@UME> same format as the chart above, please"}
newest = {"user": "UOTHER", "ts": "1763869400.000800", "text": "here it is", "files": ev_files("FCSV")}
msgs_l = [{"user": MATTHEW, "ts": root_ts, "text": "<@UME> thread"}] + [
    {"user": "UOTHER", "ts": f"1763869400.00{i:02d}00", "text": f"m{i}"} for i in range(2, 8)] + [newest]
d3 = S.Daemon("none")
d3.bot = FakeBot()
d3.client = PagedClient(msgs_l)
d3.client.history[C] = []
clear_cache()
DOWNLOADS.clear()
d3.on_event(reply)
it3 = S.load_item("CPRODUCT-1763869400_000900")
check("on_inbound in a long thread: the file on the newest page is fetched",
      [a["id"] for a in it3.get("attachments") or []] == ["FCSV"] and len(DOWNLOADS) == 1, str(it3.get("attachments")))
check("…the whole thread is read twice: once for the context and the files, once after the delay for the transcript",
      d3.client.calls.count("7") == 2, str(d3.client.calls))


A.fetch.__kwdefaults__["download"] = _real_dl

# ------------------------------------------------------------------ use: triage sees the image
# the cache and daemon checks above cleared the file folders: fetch the same files again
clear_cache()
recs = A.fetch(g, "CPRODUCT-1763869260_000100", [(bot, bot.token)], download=download)
blocks = A.image_blocks(recs)
import triage as T  # noqa: E402


class FakeAnthropic:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **k):
        self.calls.append(k)
        return type("R", (), {"content": [type("B", (), {"text": '{"route": "full", "route_confidence": 0.9}'})()]})()


fa = FakeAnthropic()
T.claude({"latest_message": "do it in this format", "attachments": A.triage_lines(recs)}, "Barada", fa, "m", images=blocks)
content = fa.calls[0]["messages"][0]["content"]
check("triage (Claude): the chart goes in as an image block before the question, the file list in the state",
      isinstance(content, list) and content[0] == blocks[0] and content[-1]["type"] == "text"
      and "format-example.png (image" in content[-1]["text"], str(content)[:300])
T.claude({"latest_message": "hi"}, "Barada", fa, "m")
check("triage (Claude) without files: the prompt stays plain text", isinstance(fa.calls[1]["messages"][0]["content"], str))
tr = T.Triager("Barada", T.Policy(), fa, "m")
tr.jev_ok = False
tr.run({"latest_message": "x"}, images=blocks)
check("Triager.run hands the images to the Claude fallback", fa.calls[2]["messages"][0]["content"][0] == blocks[0])
built = []
tr.jev_ok = True
tr.api_key = "k"
_jev = T.jev
T.jev = lambda *a, **k: T.Triage("full", 0.9, 1.0, 1.0, 0.1, 0.0, "other")
try:
    tr.run({"latest_message": "x"}, images=lambda: built.append(1) or blocks)
finally:
    T.jev = _jev
check("Triager.run: images given as a callable are not built when Jev answers", built == [] and len(fa.calls) == 3)
tr.jev_ok = False


class FailOnImages(FakeAnthropic):
    def create(self, **k):
        if isinstance(k["messages"][0]["content"], list):
            self.calls.append(k)
            raise RuntimeError("image rejected")
        return super().create(**k)


fo = FailOnImages()
t_retry = T.Triager("Barada", T.Policy(), fo, "m").run({"latest_message": "x"}, images=lambda: blocks)
check("Triager.run: a failed call with images is retried on text, not dropped to hold",
      len(fo.calls) == 2 and isinstance(fo.calls[1]["messages"][0]["content"], str) and t_retry.source == "claude:m",
      str(t_retry.__dict__))
cap = A.image_blocks(recs, captioned=True)
check("captioned image input: each image follows a text block naming the file and who sent it",
      [b["type"] for b in cap] == ["text", "image"] and "format-example.png" in cap[0]["text"]
      and "sent to the owner" in cap[0]["text"], str(cap[0]))

# ------------------------------------------------------------------ provenance
own = A.fetch([{**ev_files("FCSV")[0], "_from": "UME"}, {**ev_files("FCHART")[0], "_from": MATTHEW}], "C1-9_0",
              [(FakeBot(), "xoxb-test-bot")], download=download, owner="UME")
osl = "\n".join(A.show_lines(own))
check("provenance: a file from the owner's own thread message is labelled theirs, not 'sent to the owner'",
      own[0]["by_owner"] and not own[1]["by_owner"]
      and f"signups.csv (text, {A.human(len(CSV))}) — the owner's own" in osl and "format-example.png (image, " in osl
      and osl.count("sent to the owner") == 1 and "from the owner" in A.triage_lines(own)[0]
      and "from the owner" not in A.triage_lines(own)[1], osl)
check("provenance: no owner given, nothing is marked theirs", not any(r["by_owner"] for r in A.fetch(ev_files("FCSV"), "C1-10_0",
      [(FakeBot(), "xoxb-test-bot")], download=download)))

# ------------------------------------------------------------------ replay: Matthew's #product thread (23 Nov 03:41Z)
# The owner had posted last month's numbers in the thread; Matthew replies with the chart whose format he wants.
d3 = S.Daemon("none")
d3.bot = FakeBot()
A.fetch.__kwdefaults__["download"] = download
root_ts, ask_ts = "1763869000.000100", "1763869262.000100"
root = {"type": "message", "user": "UME", "ts": root_ts, "text": "last month's signups attached", "files": ev_files("FCSV")}
reply = {"type": "message", "user": MATTHEW, "channel": C, "channel_type": "channel", "ts": ask_ts, "thread_ts": root_ts,
         "text": "<@UME> create a chart with last month's signups in the format of the attached image",
         "files": ev_files("FCHART"), "subtype": "file_share"}
d3.client.replies = {root_ts: [root, {k: reply[k] for k in ("user", "ts", "thread_ts", "text", "files")}]}
d3.client.history[C] = [root]
seen = {}
_run = d3.triager.run


def spy(state, images=None):
    seen.update(state=state, images=images() if callable(images) else images)
    return _run(state, images=images)


d3.triager.run = spy
try:
    d3.on_event(reply)
finally:
    A.fetch.__kwdefaults__["download"] = _real_dl
    d3.triager.run = _run
it3 = S.load_item("CPRODUCT-1763869262_000100")
a3 = {r["id"]: r for r in it3.get("attachments") or []}
check("replay: the chart and the owner's CSV are both on the item, the chart Matthew's",
      set(a3) == {"FCHART", "FCSV"} and not a3["FCHART"]["by_owner"] and a3["FCSV"]["by_owner"], str(list(a3)))
imgs3 = [b for b in seen.get("images") or [] if b["type"] == "image"]
check("replay: triage got the chart as an image block and the file list in its state",
      len(imgs3) == 1 and imgs3[0]["source"]["media_type"] == "image/png"
      and A.base64.b64decode(imgs3[0]["source"]["data"]) == CHART
      and any("format-example.png (image" in ln for ln in seen["state"].get("attachments", [])), str(seen.get("state", {}).get("attachments")))
txt = S.render_item(it3, d3.ledger, set())
check("replay: the handler prompt carries the chart's path to Read, who sent what, and never-forward",
      f"Read this path to see the image: {a3['FCHART']['path']}" in txt and "never forward" in txt
      and "format-example.png (image, " in txt and f"signups.csv (text, {A.human(len(CSV))}) — the owner's own" in txt, txt[-1200:])
cmd, _, _ = S.claude_command(it3)
f3 = [str(A.file_dir(fid)) for fid in ("FCHART", "FCSV")]
tools = cmd[cmd.index("--allowedTools") + 1]
added = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"]
check("replay: the writer may Read this item's file folders, and only them, among the attachments",
      all(f"Read(//{f.lstrip('/')}/**)" in tools and f in added for f in f3)
      and str(A.ROOT) not in added and str(A.ROOT / A.FILES_DIR) not in added
      and tools.count("/attachments/") == 2, tools)
check("replay: what the files say is marked as data, not instructions", "not instructions to you" in txt)
gone = {"id": "GONE-1_0", "attachments": [{**a3["FCHART"], "path": str(A.ROOT / "GONE-1_0" / "x.png")}]}
check("pruned files: no Read path offered and no folder in reach",
      "Read this path" not in "\n".join(A.show_lines(gone["attachments"])) and "pruned" in "\n".join(A.show_lines(gone["attachments"]))
      and A.read_dirs(gone) == [])
cmd0, _, _ = S.claude_command({"id": "ITEM"})
check("an item without files: no attachments folder in the writer's reach", str(A.ROOT) not in " ".join(cmd0))

# ------------------------------------------------------------------ prune
old = A.ROOT / "COLD-1_0"
old.mkdir()
os.utime(old, (time.time() - 30 * 86400,) * 2)
old_file = A.ROOT / A.FILES_DIR / "FCOLD"
old_file.mkdir(parents=True)
for x in (old, old_file):
    os.utime(x, (time.time() - 30 * 86400,) * 2)
fresh = A.file_dir("FFRESH")
fresh.mkdir()
check("prune: folders past ATTACH_KEEP_DAYS go, cached files included; fresh ones stay",
      A.prune() == 2 and not old.exists() and not old_file.exists() and fresh.exists() and (A.ROOT / A.FILES_DIR).exists())

# ------------------------------------------------------------------ review round 3
check("a Slack file named Record.json cannot take the record's name on a case-insensitive disk",
      A.safe_name("Record.json").lower() != A.RECORD and A.safe_name("RECORD.JSON").lower() != A.RECORD)
check("read_dirs: a path outside the attachments folder grants nothing",
      A.read_dirs({"id": "x", "attachments": [{"path": "/etc/hosts"},
                                               {"path": str(A.ROOT / "stray")}]}) == [])

clear_cache()
A.fetch(ev_files("FCSV"), "C1-12_0", [(FakeBot(), "xoxb-test-bot")], download=download)
(A.file_dir("FCSV") / A.RECORD).write_text("[1, 2]")
DOWNLOADS.clear()
odd = A.fetch(ev_files("FCSV"), "C1-12_1", [(FakeBot(), "xoxb-test-bot")], download=download)
check("a record that is not an object is a miss: the file downloads again", len(DOWNLOADS) == 1 and odd[0]["path"])

_rec = A.file_dir("FCSV") / A.RECORD
_r = json.loads(_rec.read_text())
_rec.write_text(json.dumps(dict(_r, note="could not read", tools=["none-of-these"])))
_pa = A._prepare_again


def _gone(rec):
    raise FileNotFoundError("pruned")


A._prepare_again = _gone
DOWNLOADS.clear()
try:
    raced = A.fetch(ev_files("FCSV"), "C1-12_2", [(FakeBot(), "xoxb-test-bot")], download=download)
finally:
    A._prepare_again = _pa
check("a cached file gone before it is prepared again: downloaded again, not lost",
      len(DOWNLOADS) == 1 and raced[0]["path"] and raced[0]["note"] is None, str(raced[0]))

_r = json.loads(_rec.read_text())
_rec.write_text(json.dumps(dict(_r, note="could not read", tools=["none-of-these"])))
DOWNLOADS.clear()
nolate = A.fetch(ev_files("FCSV"), "C1-12_3", [(FakeBot(), "xoxb-test-bot")], download=download, budget_s=0)
check("out of time: a kept file is served as it was, not prepared again", not DOWNLOADS
      and nolate[0]["note"] == "could not read", str(nolate[0]["note"]))

clear_cache()
first = A.fetch(ev_files("FCSV"), "C1-12_4", [(FakeBot(), "xoxb-test-bot")], download=download)
renamed = dict(ev_files("FCSV")[0], name="signups-v2.csv", size=FILES["FCSV"][0]["size"] + 1)
second = A.fetch([renamed], "C1-12_5", [(FakeBot(), "xoxb-test-bot")], download=download)
check("a file edited and renamed in Slack: the old name and its text are gone from its folder",
      not os.path.exists(first[0]["path"]) and not os.path.exists(first[0]["text_path"]) and os.path.isfile(second[0]["path"]),
      str(sorted(p.name for p in A.file_dir("FCSV").iterdir())))


class FlakyClient(PagedClient):
    """The first `ok` pages answer; the next one fails, as a rate limit past its retries does."""

    def __init__(self, msgs, ok):
        super().__init__(msgs)
        self.ok = ok

    def conversations_replies(self, channel, ts, limit=200, cursor=None, **kw):
        if len(self.calls) >= self.ok:
            self.calls.append(cursor)
            raise RuntimeError("ratelimited")
        return super().conversations_replies(channel, ts, limit, cursor, **kw)


_tlog = io.StringIO()
_th = logging.StreamHandler(_tlog)
slack_thread.log.addHandler(_th)
try:
    part_thread = slack_thread.replies(FlakyClient(long_thread, 2), C, long_thread[0]["ts"])
finally:
    slack_thread.log.removeHandler(_th)
check("replies: a later page failing keeps the pages read, and says so",
      part_thread == long_thread[:5] and "stopped after 2 pages" in _tlog.getvalue(), _tlog.getvalue())
try:
    slack_thread.replies(FlakyClient(long_thread, 0), C, long_thread[0]["ts"])
    first_failed = False
except RuntimeError:
    first_failed = True
check("replies: the first page failing still raises, as one read did", first_failed)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{H.passed} checks passed")
