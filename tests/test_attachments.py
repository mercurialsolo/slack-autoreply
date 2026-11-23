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

if A.fit_image(b"\0" * (4 * 1024 * 1024), "image/png") is None:
    check("an image too large for image input without Pillow: not sent as input, the writer can Read it", True)
else:  # Pillow is installed: it is resized instead
    check("an image too large for image input is resized by Pillow", True)

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

# ------------------------------------------------------------------ prune
old = A.ROOT / "COLD-1_0"
old.mkdir()
os.utime(old, (time.time() - 30 * 86400,) * 2)
check("prune: folders past ATTACH_KEEP_DAYS go, fresh ones stay", A.prune() == 1 and not old.exists() and folder.exists())

import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{H.passed} checks passed")
