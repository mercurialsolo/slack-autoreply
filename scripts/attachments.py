"""Files on the messages Solo handles: fetch them, and turn them into what a model can use.

Someone writes "do it in the format of the attached image" and attaches a chart. Without this
module the writer saw "[shared a file]" and could only say it could not see the image. Now:

  gather(messages)          the file objects on the handled message and its thread, newest ask first
  fetch(files, item_id, …)  download each one with the bot token (files:read) into a private
                            per-item folder; a missing scope or a file the bot cannot see is a
                            note on the record, never an exception
  image_blocks(records)     images as Anthropic image input, within the model's size limits
  triage_lines(records)     what a text-only classifier is told: name, kind, size, text excerpt
  show_lines(records)       what the writer is told in `show`: the same, plus the local path its
                            Read tool opens (Claude Code reads images and PDFs natively)

Rules this keeps:
- Content never reaches a log line. Logs carry counts and kinds, not names, text or bytes.
- A fetched file is never sent anywhere: it lives under STATE_DIR/attachments/<item>/ (0700/0600),
  `say --file` refuses that folder, and `prune` removes folders older than ATTACH_KEEP_DAYS.
- Limits: ATTACH_MAX_FILES per item, ATTACH_MAX_MB per file, TEXT_CHARS of extracted text.
"""
from __future__ import annotations

import base64
import io
import logging
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import config

log = logging.getLogger("autoreply.attachments")

ROOT = config.STATE_DIR / "attachments"
MAX_FILES = config.env_int("ATTACH_MAX_FILES", 5)
MAX_BYTES = int(config.env_float("ATTACH_MAX_MB", 20) * 1024 * 1024)
KEEP_DAYS = config.env_float("ATTACH_KEEP_DAYS", 7)
TEXT_CHARS = 20_000
TRIAGE_TEXT_CHARS = 1_500
SHOW_TEXT_CHARS = 6_000

# Anthropic image input: these four media types, at most 5 MB each once base64-encoded, and a
# long edge past ~1568 px is scaled down by the API anyway, so we send that size.
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMAGE_MAX_B64 = 5 * 1024 * 1024
IMAGE_MAX_EDGE = 1568
TEXT_FILETYPES = {"text", "csv", "tsv", "markdown", "md", "json", "yaml", "yml", "xml", "html", "python",
                  "javascript", "typescript", "sql", "shell", "log", "plain", "post", "go", "rust"}
DOCX_TYPES = {"docx"}
PDF_TYPES = {"pdf"}

NO_BOT = "not fetched: no bot token (SLACK_BOT_TOKEN) — add one with files:read to let Solo read attachments"
NO_SCOPE = ("not fetched: the bot token lacks the files:read scope — add it under OAuth & Permissions "
            "(manifest.yaml has it) and reinstall the app")
NO_ACCESS = "not fetched: Slack would not give this file to the bot (invite the bot to the channel)"


# ------------------------------------------------------------ which files

def gather(messages: list[dict], limit: int = MAX_FILES) -> list[dict]:
    """The Slack file objects on these messages, first message's files first, each file once.

    Pass the handled message first and then its thread newest-first: the files nearest the ask
    are the ones it most likely means, and they are the ones kept when there are too many."""
    out: list[dict] = []
    seen: set[str] = set()
    for m in messages:
        for f in m.get("files") or []:
            fid = f.get("id")
            if not fid or fid in seen or f.get("mode") == "tombstone":
                continue
            seen.add(fid)
            out.append({**f, "_from": m.get("user") or m.get("bot_id")})
    return out[:limit]


def kind_of(f: dict) -> str:
    mt = (f.get("mimetype") or "").lower()
    ft = (f.get("filetype") or "").lower()
    if mt in IMAGE_TYPES:
        return "image"
    if ft in PDF_TYPES or mt == "application/pdf":
        return "pdf"
    if ft in DOCX_TYPES or mt.endswith("wordprocessingml.document"):
        return "docx"
    if mt.startswith("text/") or ft in TEXT_FILETYPES or mt in ("application/json", "application/csv"):
        return "text"
    return "other"


# ------------------------------------------------------------ fetching

def item_dir(item_id: str) -> Path:
    return ROOT / re.sub(r"[^\w.-]", "_", item_id)


def safe_name(i: int, name: str) -> str:
    base = re.sub(r"[^\w.-]", "_", Path(name or "file").name).lstrip(".") or "file"
    return f"{i}-{base[:80]}"


def http_download(url: str, token: str, max_bytes: int) -> tuple[int, str, bytes | None]:
    """GET a Slack private file → (status, content-type, bytes, or None when it is over max_bytes)."""
    import httpx

    with httpx.stream("GET", url, headers={"Authorization": f"Bearer {token}"}, follow_redirects=True,
                      timeout=30) as r:
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200:
            return r.status_code, ctype, b""
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                return r.status_code, ctype, None
        return r.status_code, ctype, bytes(buf)


def _slack_error(e: Exception) -> str:
    resp = getattr(e, "response", None)
    try:
        return str((resp or {}).get("error") or e)
    except Exception:  # noqa: BLE001 — a SlackResponse that cannot be indexed
        return str(e)


def _info(client, fid: str) -> tuple[dict | None, str | None]:
    """files.info through one client → (file, None) or (None, note)."""
    try:
        return client.files_info(file=fid)["file"], None
    except Exception as e:  # noqa: BLE001 — every Slack refusal becomes a note, not a crash
        err = _slack_error(e)
        if "missing_scope" in err:
            return None, NO_SCOPE
        if err in ("file_not_found", "file_deleted", "access_denied", "not_in_channel"):
            return None, NO_ACCESS
        return None, f"not fetched: Slack said {err[:80]}"


def fetch(files: list[dict], item_id: str, clients: list[tuple[object, str]], *,
          download=http_download, max_bytes: int = MAX_BYTES) -> list[dict]:
    """Download each file and return one record per file.

    `clients` is [(WebClient, token), …] in the order to try: the bot first (files:read), then the
    owner's user token, which works only if the owner granted it files:read too. A record always
    has name/kind/size; `path` only when the bytes are on disk; `note` says why not."""
    records: list[dict] = []
    if not files:
        return records
    folder = item_dir(item_id)
    for i, f in enumerate(files):
        rec = {"id": f.get("id"), "name": f.get("name") or f.get("title") or "file", "title": f.get("title") or "",
               "mimetype": f.get("mimetype") or "", "filetype": f.get("filetype") or "", "size": int(f.get("size") or 0),
               "kind": kind_of(f), "from": f.get("_from"), "path": None, "note": None}
        records.append(rec)
        if not clients:
            rec["note"] = NO_BOT
            continue
        if rec["size"] > max_bytes:
            rec["note"] = f"not fetched: {rec['size'] // (1024 * 1024)} MB is over the {max_bytes // (1024 * 1024)} MB limit"
            continue
        note = None
        for client, token in clients:
            info, note = _info(client, rec["id"]) if rec["id"] else (f, None)
            if info is None:
                continue
            url = info.get("url_private_download") or info.get("url_private")
            if not url:
                note = "not fetched: Slack gave no download link (an external or deleted file)"
                continue
            try:
                status, ctype, data = download(url, token, max_bytes)
            except Exception as e:  # noqa: BLE001 — a network fault is a note
                note = f"not fetched: download failed ({type(e).__name__})"
                continue
            if data is None:
                note = f"not fetched: over the {max_bytes // (1024 * 1024)} MB limit"
                break
            if status != 200 or ("text/html" in ctype and rec["filetype"] != "html"):
                # Slack answers a token that may not read the file with its sign-in page, not a 403.
                note = NO_ACCESS
                continue
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            ROOT.chmod(0o700)
            path = folder / safe_name(i, rec["name"])
            path.write_bytes(data)
            path.chmod(0o600)
            rec["path"], rec["size"], note = str(path), len(data), None
            _extract(rec, data)
            break
        rec["note"] = note if rec["path"] is None else rec.get("note")
    got = sum(1 for r in records if r["path"])
    log.info("attachments for %s: %d of %d fetched (%s)", item_id, got, len(records),
             ", ".join(sorted({r["kind"] for r in records})))
    return records


# ------------------------------------------------------------ turning bytes into model input

def _extract(rec: dict, data: bytes) -> None:
    """Text for documents, a model-sized copy for images. Writes sidecar files next to the original."""
    text = None
    if rec["kind"] == "text":
        text = data.decode("utf-8", errors="replace")
    elif rec["kind"] == "pdf":
        text = pdf_text(Path(rec["path"]), data)
        if text is None:
            rec["note"] = "PDF text could not be extracted here; Read the file to see it"
    elif rec["kind"] == "docx":
        text = docx_text(data)
    elif rec["kind"] == "image":
        small = fit_image(data, rec["mimetype"])
        if small is None:
            rec["note"] = "too large to pass as image input without Pillow; Read the file to see it"
        else:
            body, mt = small
            if body is not data:
                p = Path(rec["path"] + ".model" + {"image/png": ".png", "image/jpeg": ".jpg"}.get(mt, ""))
                p.write_bytes(body)
                p.chmod(0o600)
                rec["model_path"] = str(p)
            rec["model_type"] = mt
    if text is not None:
        text = text.strip()
        rec["text_chars"] = len(text)
        p = Path(rec["path"] + ".txt")
        p.write_text(text[:TEXT_CHARS])
        p.chmod(0o600)
        rec["text_path"] = str(p)


def pdf_text(path: Path, data: bytes) -> str | None:
    try:
        import pypdf  # optional

        reader = pypdf.PdfReader(io.BytesIO(data))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages[:30])
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 — a broken PDF is described, not fatal
        return None
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-l", "30", str(path), "-"], capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else None


def docx_text(data: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    return re.sub(r"&amp;", "&", re.sub(r"&lt;", "<", re.sub(r"&gt;", ">", text)))


def fit_image(data: bytes, mimetype: str) -> tuple[bytes, str] | None:
    """(bytes, media type) within the image-input limits, or None when that needs Pillow and it is absent."""
    try:
        from PIL import Image  # optional
    except ImportError:
        return (data, mimetype) if len(data) * 4 / 3 <= IMAGE_MAX_B64 else None
    try:
        im = Image.open(io.BytesIO(data))
        if max(im.size) <= IMAGE_MAX_EDGE and len(data) * 4 / 3 <= IMAGE_MAX_B64:
            return data, mimetype
        im.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
        out = io.BytesIO()
        if im.mode in ("RGBA", "LA", "P"):
            im.save(out, "PNG", optimize=True)
            mt = "image/png"
        else:
            im.convert("RGB").save(out, "JPEG", quality=85)
            mt = "image/jpeg"
        body = out.getvalue()
        return (body, mt) if len(body) * 4 / 3 <= IMAGE_MAX_B64 else None
    except Exception:  # noqa: BLE001 — an image Pillow cannot open is described, not fatal
        return (data, mimetype) if len(data) * 4 / 3 <= IMAGE_MAX_B64 else None


def _text(rec: dict, n: int) -> str:
    p = rec.get("text_path")
    if not p or not Path(p).is_file():
        return ""
    t = Path(p).read_text(errors="replace")
    return t[:n] + (f"\n… ({rec.get('text_chars', len(t)) - n} more characters)" if len(t) > n else "")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def label(rec: dict) -> str:
    kind = {"image": "image", "pdf": "PDF", "docx": "Word document", "text": "text"}.get(rec["kind"], rec["filetype"] or "file")
    return f"{rec['name']} ({kind}, {human(rec['size'])})"


def image_blocks(records: list[dict]) -> list[dict]:
    """Anthropic content blocks for the images that fit, in record order."""
    out = []
    for r in records:
        if r["kind"] != "image" or not r.get("path") or "model_type" not in r:
            continue
        p = Path(r.get("model_path") or r["path"])
        if not p.is_file():
            continue
        out.append({"type": "image", "source": {"type": "base64", "media_type": r["model_type"],
                                                "data": base64.b64encode(p.read_bytes()).decode()}})
    return out


def triage_lines(records: list[dict]) -> list[str]:
    """One line per file for a classifier: what it is, and the start of its text."""
    out = []
    for r in records:
        line = f"{label(r)}"
        if r["kind"] == "image" and "model_type" in r:
            line += " — shown as an image"
        if (t := _text(r, TRIAGE_TEXT_CHARS)):
            line += f" — text: {t}"
        if r.get("note") and not r.get("path"):
            line += f" — {r['note']}"
        out.append(line)
    return out


def show_lines(records: list[dict]) -> list[str]:
    """The `show` section for the writer: each file, where to Read it, and its extracted text."""
    if not records:
        return []
    out = ["--- files on this message or its thread (sent to the owner, not by them; never forward them) ---"]
    for r in records:
        out.append(f"- {label(r)}")
        if r.get("path"):
            how = {"image": "Read this path to see the image", "pdf": "Read this path to see the PDF"}.get(
                r["kind"], "Read this path for the whole file")
            out.append(f"  {how}: {r['path']}")
        if r.get("note"):
            out.append(f"  {r['note']}")
        if (t := _text(r, SHOW_TEXT_CHARS)):
            out.append("  text:\n" + "\n".join("    " + ln for ln in t.splitlines()))
    out.append("")
    return out


def read_dirs(item: dict) -> list[str]:
    """The folder the writer may Read for this item, when it has fetched files."""
    if any(r.get("path") for r in item.get("attachments") or []):
        return [str(item_dir(item["id"]))]
    return []


def prune(days: float = KEEP_DAYS, now: float | None = None) -> int:
    """Remove per-item folders older than `days`. → how many went."""
    if not ROOT.is_dir():
        return 0
    cutoff = (now or time.time()) - days * 86400
    gone = 0
    for d in ROOT.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            gone += 1
    return gone
