"""attachments: which files a handled message carries, fetching them with the bot token, and the
model input they become — without a name or a byte of them reaching the log, and never forwarded."""
from __future__ import annotations

import io
import logging
import os
import stat
import struct
import time
import zipfile
import zlib

import offline_harness as H  # noqa: E402  (sets the environment first)
from offline_harness import TMP, STATE, FakeClient, check  # noqa: E402
import attachments as A  # noqa: E402
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


def download(url, token, max_bytes):
    DOWNLOADS.append((url, token))
    fid = url.split("/")[-2]
    if fid == "FHTML":
        return 200, "text/html; charset=utf-8", b"<html>sign in</html>"
    data = FILES[fid][1]
    return (200, "application/octet-stream", None) if len(data) > max_bytes else (200, "application/octet-stream", data)


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
dm2 = A.fetch(ev_files("FCSV"), "D1-2_0", [(FakeBot("file_not_found"), "xoxb-test-bot"), (FakeOwner("missing_scope"), "xoxp-test-owner")],
              download=download)
check("…and when neither can, the bot's reason is the one given", dm2[0]["path"] is None and dm2[0]["note"] == A.NO_ACCESS)

FILES["FPOST"] = ({"id": "FPOST", "name": "notes", "mimetype": "text/html", "filetype": "post", "size": 20,
                   "url_private_download": "https://files.slack.test/FPOST/download"}, b"<p>meeting notes</p>")
_dl = download


def download(url, token, max_bytes):  # noqa: F811 — a Slack Post really is served as text/html
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
DOWNLOADS.clear()
recs = A.fetch(g, "CPRODUCT-1763869260_000100", [(bot, bot.token)], download=download)
A.log.removeHandler(_h)
by = {r["id"]: r for r in recs}
check("fetched with the bot token, every file", [t for _, t in DOWNLOADS] == ["xoxb-test-bot"] * 5 and bot.infos == list(by))
folder = A.item_dir("CPRODUCT-1763869260_000100")
check("stored in a private per-item folder (0700, files 0600)",
      stat.S_IMODE(folder.stat().st_mode) == 0o700 and stat.S_IMODE(A.ROOT.stat().st_mode) == 0o700
      and all(stat.S_IMODE(os.stat(r["path"]).st_mode) == 0o600 for r in recs) and folder.is_relative_to(STATE))
check("the image as saved is the image sent", open(by["FCHART"]["path"], "rb").read() == CHART)
log_text = logs.getvalue()
check("the log carries counts and kinds only — no names, no content",
      "5 of 5 fetched" in log_text and not any(s in log_text for s in ("format-example", "signups", "2026-11-02", "plan.docx", "Ship charts")), log_text)

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
check("read_dirs: the item's folder only when something was fetched",
      A.read_dirs({"id": "CPRODUCT-1763869260_000100", "attachments": recs}) == [str(folder)]
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
DOWNLOADS.clear()
d.on_event(ask)
item = S.load_item("CPRODUCT-1763869260_000100")
att = item.get("attachments") or []
check("on_inbound: the attached chart is fetched with the bot token and kept on the item",
      len(att) == 1 and att[0]["kind"] == "image" and att[0]["path"] and DOWNLOADS == [(FILES["FCHART"][0]["url_private_download"], "xoxb-test-bot")],
      str(att))
check("…the item carries a record, not the image bytes", "data" not in str(item) and len(str(item)) < 20000)

d2 = S.Daemon("none")  # no bot token
d2.on_event(dict(ask, ts="1763869261.000100"))
it2 = S.load_item("CPRODUCT-1763869261_000100")
check("on_inbound without a bot: the file is described with what to add", "SLACK_BOT_TOKEN" in (it2["attachments"][0]["note"] or ""))
A.fetch.__kwdefaults__["download"] = _real_dl

# ------------------------------------------------------------------ use: triage sees the image
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
    seen.update(state=state, images=images)
    return _run(state, images=images)


d3.triager.run = spy
d3.on_event(reply)
A.fetch.__kwdefaults__["download"] = _real_dl
it3 = S.load_item("CPRODUCT-1763869262_000100")
a3 = {r["id"]: r for r in it3.get("attachments") or []}
check("replay: the chart and the owner's CSV are both on the item, the chart Matthew's",
      set(a3) == {"FCHART", "FCSV"} and not a3["FCHART"]["by_owner"] and a3["FCSV"]["by_owner"], str(list(a3)))
check("replay: triage got the chart as an image block and the file list in its state",
      seen.get("images") and seen["images"][0]["source"]["media_type"] == "image/png"
      and A.base64.b64decode(seen["images"][0]["source"]["data"]) == CHART
      and any("format-example.png (image" in ln for ln in seen["state"].get("attachments", [])), str(seen.get("state", {}).get("attachments")))
txt = S.render_item(it3, d3.ledger, set())
check("replay: the handler prompt carries the chart's path to Read, who sent what, and never-forward",
      f"Read this path to see the image: {a3['FCHART']['path']}" in txt and "never forward" in txt
      and "format-example.png (image, " in txt and f"signups.csv (text, {A.human(len(CSV))}) — the owner's own" in txt, txt[-1200:])
cmd, _, _ = S.claude_command(it3)
f3 = str(A.item_dir(it3["id"]))
tools = cmd[cmd.index("--allowedTools") + 1]
check("replay: the writer may Read this item's folder, and only it, among the attachments",
      f"Read(//{f3.lstrip('/')}/**)" in tools and f3 in [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"]
      and str(A.ROOT) not in [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"], tools)
cmd0, _, _ = S.claude_command({"id": "ITEM"})
check("an item without files: no attachments folder in the writer's reach", str(A.ROOT) not in " ".join(cmd0))

# ------------------------------------------------------------------ prune
old = A.ROOT / "COLD-1_0"
old.mkdir()
os.utime(old, (time.time() - 30 * 86400,) * 2)
check("prune: folders past ATTACH_KEEP_DAYS go, fresh ones stay", A.prune() == 1 and not old.exists() and folder.exists())

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{H.passed} checks passed")
