"""The library: files you upload (on the apps page), read once in the background, so the
agent can answer questions about them with citations that are checked before he speaks.

Each file becomes text with locations a citation can point to: `page 12`, `slide 4`,
`sheet Sales, rows 1-500`, `§ 14. Termination`, `lines 1-80`. PDF text comes straight from
the file (pypdfium2), a page at a time; scanned pages and images are transcribed by Claude
vision (marked scanned), after a yes for a large scan and within a daily page limit;
spreadsheets are read row by row; Word, PowerPoint, EPUB and HTML go through MarkItDown,
with the pictures in Word and PowerPoint files read by vision. Old Office formats are
converted by LibreOffice first. A zip is unpacked into its files.

The text is cut into chunks labelled with their document and location, and found two ways
(hybrid search): by keyword (SQLite FTS5, with stemming: "terminate" finds "termination")
and by meaning (OpenAI embeddings of each chunk with its label, in sqlite-vec). The two
rankings are merged; without embeddings (offline), keyword search carries on alone.
Pages are searchable as soon as they are done; small files are read before large ones.

Everything lives in ~/.alfred/library: the files, and library.db.
"""

import asyncio
import base64
import codecs
import csv
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path

from loguru import logger

from alfred.memory import words_in

PDF = {".pdf"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MARKDOWN = {".docx", ".pptx", ".xls", ".html", ".htm", ".epub", ".msg"}
ROWS = {".xlsx", ".xlsm", ".csv"}  # read row by row: any size
SPREADSHEETS = ROWS | {".xls"}
CONVERT = {".doc": "docx", ".rtf": "docx", ".odt": "docx", ".ppt": "pptx", ".odp": "pptx",
           ".ods": "xlsx"}  # fmt: skip
TEXT = {".txt", ".md", ".markdown", ".json", ".xml", ".yaml", ".yml", ".log", ".tsv", ".ini"}
DESCRIBE_AT_ONCE = 10  # files per Claude call: starting Claude takes longer than the writing
OPENING = 2000  # characters of a file its description is written from
PICTURE_BYTES_AT_LEAST = 8000  # smaller ones are icons and logos
ZIP_FILES_AT_MOST = 50_000
WHOLE_FILE_MB = 2048  # read whole, into memory: Word, PowerPoint, EPUB, HTML, .xls (a 2 GB
# deck needs about 3 GB of RAM: fine on an 8 GB Pi 5)
KEEP_FREE_GB = 10  # the card always keeps this much free
PAGES_AT_ONCE = 8  # PDF pages read per step; each step is searchable when done
ROWS_PER_PART = 500
CLAUDE_EDGE, CLAUDE_PIXELS = 1568, 1_150_000  # the size Claude reads pictures at; bigger
# ones are shrunk on arrival, so sending more only slows the call
SENT_AS_IS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}  # Claude's kinds
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
BLANK_SPREAD = 3.0  # brightness varying less than this: a blank picture, not worth reading
SEEN_AT_MOST = 20_000  # pictures remembered, so one repeated across files is read once
TOKENS_PER_SCAN = 2000  # roughly, for a page image and its text
SCANNED_BELOW = 30  # characters: a page with less text than this is read as an image
DRAWN_AT_LEAST = 8  # drawn shapes and lines on a page before it may be a diagram
DRAWN_TEXT_BELOW = 2500  # characters: a page with more is text with rules and tables
CHUNK, OVERLAP = 1500, 200  # characters
PART_AT_MOST = 6000  # characters: longer sections are split into parts
LINES_PER_PART = 80
READ_AT_MOST = 24_000  # characters read_document returns at once
LOOSE_MATCH = 0.9  # how much of a quote must line up on a scanned page
DIMENSIONS = 1024  # numbers per embedding
EMBED_AT_ONCE = 64  # chunks per embeddings call
FOUND_EACH_WAY = 40  # candidates from each search before the rankings are merged
FUSION_K = 60  # reciprocal rank fusion: score = sum of 1 / (60 + rank)
LISTED_IN_PROMPTS = 40  # more files than this: a summary instead of every name
RECHECK_SECONDS = 600  # paused files (daily scan limit) are looked at again this often

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,           -- the start of the file's SHA-256
    name        TEXT NOT NULL,              -- as uploaded
    path        TEXT NOT NULL,
    status      TEXT NOT NULL,              -- waiting processing asking paused ready failed
    done        INTEGER NOT NULL DEFAULT 0, -- pages (or parts) done
    total       INTEGER NOT NULL DEFAULT 0,
    note        TEXT,                       -- what to know, or what went wrong
    created_at  REAL NOT NULL,
    bytes       INTEGER NOT NULL DEFAULT 0,
    approved    INTEGER NOT NULL DEFAULT 0, -- yes to reading a large scan
    later       INTEGER NOT NULL DEFAULT 0, -- its pictures are read after every file's text
    about       TEXT                        -- one line on what it is; NULL: not written yet
);
CREATE TABLE IF NOT EXISTS parts (            -- the text at each location, in order
    doc_id      TEXT NOT NULL,
    ord         INTEGER NOT NULL,
    location    TEXT NOT NULL,              -- page 12 | slide 4 | sheet Sales, rows 1-500 | ...
    text        TEXT NOT NULL,
    scanned     INTEGER NOT NULL DEFAULT 0, -- read from an image by Claude vision
    PRIMARY KEY (doc_id, ord)
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    label, text, doc_id UNINDEXED, ord UNINDEXED, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS answers (          -- every checked answer, for the record
    at          REAL NOT NULL,
    project     TEXT NOT NULL,
    answer      TEXT NOT NULL,
    citations   TEXT NOT NULL               -- JSON
);
CREATE TABLE IF NOT EXISTS vision_days (      -- scanned pages read per day, for the limit
    day         TEXT PRIMARY KEY,
    pages       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Vision: page images in, their text out (one string per image, in order).
Vision = Callable[[list[Path]], Awaitable[list[str]]]
# Describer: (name, opening text) per file in, one line on what each file is out.
Describer = Callable[[list[tuple[str, str]]], Awaitable[list[str]]]
# Embedder: texts in, one embedding (DIMENSIONS numbers) per text out.
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class Refused(ValueError):
    """An upload the library won't take: too large, or no room. The message says why."""


class Unreadable(Exception):
    """A file Alfred can't read. The message says why, for the library page."""


@dataclass
class Limits:
    max_mb: float = 4096  # per file
    max_gb: float = 64  # the whole library
    ask_pages: int = 10_000  # scanned pages in one file before asking first
    daily_pages: int = 20_000  # scanned pages and pictures read by vision per day
    vision_per_call: int = 8  # pictures in one Claude call
    vision_at_once: int = 4  # Claude calls at once (your plan's rate limit decides)


@dataclass
class Document:
    id: str
    name: str
    path: str
    status: str
    done: int
    total: int
    note: str | None
    created_at: float
    bytes: int = 0
    approved: int = 0
    later: int = 0
    about: str | None = None

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()

    @property
    def converted(self) -> Path | None:
        """Where LibreOffice's modern copy of an old Office file goes."""
        to = CONVERT.get(self.suffix)
        return Path(self.path).with_name(f"{self.id}.converted.{to}") if to else None

    @property
    def readable(self) -> str:
        """The file to open: the converted copy of an old Office file, else the file."""
        copy = self.converted
        return str(copy) if copy and copy.exists() else self.path

    @property
    def media(self) -> Path:
        """Where the pictures read from a Word or PowerPoint file go, as <part>.png."""
        return Path(self.path).with_name(f"{self.id}.media")

    def describe(self, with_file: bool = True) -> dict:
        """with_file: for the agent, to calculate from or to look at a page or picture."""
        progress = f"{self.done} of {self.total}" if self.status in ("processing", "paused") else ""
        return (
            {"document": self.name, "id": self.id, "status": self.status}
            | ({"progress": progress} if progress else {})
            | ({"about": self.about} if self.about else {})
            | {"added": ago(self.created_at)}
            | ({"pictures": "still being read"} if self.later else {})
            | ({"note": self.note} if self.note else {})
            | ({"file": self.readable} if with_file else {})
        )


@dataclass
class Part:
    location: str
    text: str
    scanned: bool = False


class Library:
    def __init__(
        self,
        root: Path,
        vision: Vision | None,
        limits: Limits | None = None,
        embedder: Embedder | None = None,
        embedding_model: str = "",
        describer: Describer | None = None,
    ):
        self._describer = describer
        self._seen: dict[str, str] = {}  # a picture's fingerprint: what Claude read in it
        self._describing: asyncio.Task | None = None
        self._office = asyncio.Lock()  # one LibreOffice at a time: two clash over its profile
        self._scans = asyncio.Lock()  # pictures: checking and counting scans, as one step
        self.root, self.files, self.incoming = root, root / "files", root / "incoming"
        self.files.mkdir(parents=True, exist_ok=True)
        self.incoming.mkdir(exist_ok=True)
        self.limits = limits or Limits()
        self._vision, self._embedder = vision, embedder
        self._conn = sqlite3.connect(root / "library.db", check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")  # searches never wait for writes
        self._conn.executescript(SCHEMA)
        self._add_missing_columns()
        self.vectors = self._open_vectors(embedding_model) if embedder else False
        self._lock = threading.Lock()  # the upload page calls in from its own thread
        self._loop: asyncio.AbstractEventLoop | None = None
        # (pass, size, order, id): every file's text (pass 0) before any pictures (pass 1)
        self._queue: asyncio.PriorityQueue[tuple[int, int, int, str]] = asyncio.PriorityQueue()
        self._queued: set[tuple[str, int]] = set()
        self._order = 0
        self._workers: list[asyncio.Task] = []
        self._embedding: set[asyncio.Task] = set()  # meaning search catches up behind reading
        self._embed_slots = asyncio.Semaphore(2)
        self.on_ready: list[Callable[[Document], None]] = []
        self.on_asking: list[Callable[[Document], None]] = []  # a large scan needs a yes
        self.on_change: list[Callable[[], None]] = []  # files added or removed

    @property
    def max_bytes(self) -> int:
        return int(self.limits.max_mb * 1024 * 1024)

    def _add_missing_columns(self) -> None:
        """An older library.db lacks the newer columns."""
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(documents)")}
        number = "INTEGER NOT NULL DEFAULT 0"
        for column, kind in (
            ("bytes", number),
            ("approved", number),
            ("later", number),
            ("about", "TEXT"),
        ):
            if column not in have:
                self._conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {kind}")
        self._conn.commit()

    def _open_vectors(self, model: str) -> bool:
        """The meaning index (sqlite-vec). False if it can't load: keyword search only."""
        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
        except Exception as error:  # Python built without extension loading, say
            logger.warning(f"Library: meaning search is off ({error}); keyword search only")
            return False
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(embedding float[{DIMENSIONS}])"
        )
        row = self._conn.execute("SELECT value FROM settings WHERE key = 'model'").fetchone()
        if row and row[0] != model:  # a new model: every chunk is embedded again
            logger.info(f"Library: embeddings model changed to {model}: re-embedding")
            self._conn.execute("DELETE FROM vectors")
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('model', ?)", (model,)
        )
        self._conn.commit()
        return True

    # --- the database, from any thread ---

    def _sql(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
            self._conn.commit()
            return rows

    def documents(self, newest_first: bool = False) -> list[Document]:
        order = "created_at DESC" if newest_first else "name"
        return [Document(**dict(r)) for r in self._sql(f"SELECT * FROM documents ORDER BY {order}")]

    def get(self, doc_id: str) -> Document | None:
        rows = self._sql("SELECT * FROM documents WHERE id = ?", (doc_id,))
        return Document(**dict(rows[0])) if rows else None

    def find(self, ref: str) -> Document | None:
        """A document by its id or its name, a word of its name, or words of what it's
        about ("the lease"). Several match: None, never a guess."""
        ref = ref.strip().lower()
        docs, wanted = self.documents(), set(words_in(ref))
        for test in (
            lambda d: d.id == ref or d.name.lower() == ref,
            lambda d: Path(d.name).name.lower() == ref or Path(d.name).stem.lower() == ref,
            lambda d: ref and ref in d.name.lower(),
            lambda d: set(words_in(ref)) & set(words_in(Path(d.name).stem)),
            lambda d: wanted and wanted <= set(words_in(d.about or "")),  # "the lease"
        ):
            if found := [d for d in docs if test(d)]:
                return found[0] if len(found) == 1 else None  # several: never guess
        return None

    def contents_text(self, with_about: bool = True) -> str:
        """For every prompt: which files the library holds, so a question they may answer
        goes to them first, not to the web. Past LISTED_IN_PROMPTS files, a summary.
        with_about: what each file is; GPT-Live leaves it out (a longer prompt is slower)."""
        docs = self.documents()
        if not docs:
            return ""
        intro = (
            "\n\n## The user's files (the library)\nQuestions these may answer, even when "
            "the user doesn't mention a file, are answered from them first."
            + (f" {waiting}." if (waiting := self.waiting_text()) else "")
            + "\n"
        )
        if len(docs) <= LISTED_IN_PROMPTS:
            return intro + "\n".join(
                f"- {doc.name}{brief(doc)}{about(doc) if with_about else ''}" for doc in docs
            )
        kinds: dict[str, int] = {}
        for doc in docs:
            kind = doc.suffix.lstrip(".").upper() or "other"
            kinds[kind] = kinds.get(kind, 0) + 1
        counts = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items(), key=lambda k: -k[1]))
        newest = sorted(docs, key=lambda d: d.created_at, reverse=True)[:10]
        return (
            intro
            + f"{len(docs)} files ({counts}). Newest:\n"
            + "\n".join(
                f"- {doc.name}{brief(doc)}{about(doc) if with_about else ''}" for doc in newest
            )
            + "\nFind the rest with list_documents and search_documents."
        )

    def waiting_text(self) -> str:
        """Files not ready yet, by what they wait for: "2 still being read, 1 waiting for
        the user's yes on the library page"."""
        why = {"waiting": "still being read", "processing": "still being read",
               "asking": "waiting for the user's yes on the library page",
               "paused": "waiting for tomorrow's scan limit"}  # fmt: skip
        counts: dict[str, int] = {}
        for doc in self.documents():
            if doc.status in why:
                counts[why[doc.status]] = counts.get(why[doc.status], 0) + 1
        return ", ".join(f"{n} {what}" for what, n in counts.items())

    def space(self) -> dict:
        """What the library holds, what it may hold, and what the card has free."""
        used = sum(d.bytes for d in self.documents())
        free = shutil.disk_usage(self.root).free
        return {"used": used, "limit": int(self.limits.max_gb * 1024**3), "free": free}

    # --- adding and removing (the upload page) ---

    def check_upload(self, name: str, size: int) -> None:
        """Raises Refused, with the reason, for a file the library can't take."""
        suffix = Path(name).suffix.lower()
        cap = WHOLE_FILE_MB if suffix in MARKDOWN else self.limits.max_mb
        if size > cap * 1024 * 1024 and suffix != ".zip":  # a zip's files are checked one by one
            why = (
                " (Word, PowerPoint, EPUB and web pages are read whole)"
                if cap == WHOLE_FILE_MB
                else ""
            )
            raise Refused(f"{name} is {size / 1048576:.0f} MB: the limit is {cap:.0f} MB{why}.")
        room = self.space()
        if room["used"] + size > room["limit"]:
            raise Refused(
                f"The library is full ({self.limits.max_gb:.0f} GB): delete some files first."
            )
        if room["free"] - size < KEEP_FREE_GB * 1024**3:
            raise Refused(f"Not enough space: the card keeps {KEEP_FREE_GB} GB free.")

    def receive(self, upload_id: str, offset: int, chunk: bytes, size: int, name: str) -> int:
        """One piece of an upload, from the upload page. Returns how many bytes are in
        now: if `offset` isn't that (a piece lost or sent twice), nothing is written and
        the page carries on from the returned count."""
        if not re.fullmatch(r"[a-z0-9-]{8,64}", upload_id):
            raise Refused("That upload has no proper id.")
        part = self.incoming / f"{upload_id}.part"
        have = part.stat().st_size if part.exists() else 0
        if offset == 0 and have == 0:
            self.check_upload(name, size)
        if offset != have:
            return have
        if have + len(chunk) > size:
            raise Refused("The upload is larger than it said it would be.")
        with part.open("ab") as out:
            out.write(chunk)
        return have + len(chunk)

    def finish(self, upload_id: str, name: str, replace: bool = False) -> tuple[Document, str]:
        """An upload's last piece arrived: keep it as a document."""
        return self.add_file(name, self.incoming / f"{upload_id}.part", replace)

    def add(self, name: str, data: bytes, replace: bool = True) -> tuple[Document, str]:
        """Keep a file given as bytes (tests, small files)."""
        self.check_upload(name, len(data))
        handle, temp = tempfile.mkstemp(dir=self.incoming, suffix=".part")
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        return self.add_file(name, Path(temp), replace)

    def add_file(
        self, name: str, path: Path, replace: bool = False, notify: bool = True
    ) -> tuple[Document | None, str]:
        """Keep a received file and queue it. Returns the document and what happened:
        added, already there (same contents), replaced, or exists (same name, different
        contents, and `replace` not given: nothing is done). A zip is unpacked instead."""
        name = "/".join(p for p in re.split(r"[\\/]+", name.strip()) if p not in ("", ".", ".."))
        name = name or "untitled"
        if name.lower().endswith(".zip"):
            return None, self._unpack(name, path, replace)
        digest = hashlib.sha256()
        with path.open("rb") as data:
            while block := data.read(1 << 20):
                digest.update(block)
        doc_id = digest.hexdigest()[:16]
        if existing := self.get(doc_id):
            path.unlink(missing_ok=True)
            return existing, "already there"
        same_name = [
            Document(**dict(r))
            for r in self._sql("SELECT * FROM documents WHERE lower(name) = lower(?)", (name,))
        ]
        if same_name and not replace:
            path.unlink(missing_ok=True)
            return same_name[0], "exists"
        for old in same_name:
            self.delete(old.id)
        final = self.files / f"{doc_id}{Path(name).suffix.lower()}"
        path.replace(final)
        self._sql(
            "INSERT INTO documents (id, name, path, status, created_at, bytes) "
            "VALUES (?, ?, ?, 'waiting', ?, ?)",
            (doc_id, name, str(final), time.time(), final.stat().st_size),
        )
        self._enqueue(doc_id)
        if notify:
            self._changed()
        logger.info(f"Library: {name} {'replaced' if same_name else 'added'}")
        return self.get(doc_id), "replaced" if same_name else "added"

    def _unpack(self, name: str, path: Path, replace: bool) -> str:
        """Each file in a zip, added on its own and named by its path inside it (a zip in
        it too, one level down). Sizes are checked before anything is written, and again
        while unpacking, in case the zip lies. Returns what happened, for the page."""
        counts: dict[str, int] = {}
        try:
            self._unpack_into(name, path, replace, counts, prefix="", depth=0)
        finally:
            self._changed()
        return f"{name}: " + (", ".join(f"{n} {what}" for what, n in counts.items()) or "empty")

    def _unpack_into(self, name, path: Path, replace, counts, prefix: str, depth: int) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                members = [m for m in archive.infolist() if not m.is_dir() and not junk(m.filename)]
                total, room = sum(m.file_size for m in members), self.space()
                if len(members) > ZIP_FILES_AT_MOST:
                    raise Refused(f"{name} holds more than {ZIP_FILES_AT_MOST} files.")
                if (
                    room["used"] + total > room["limit"]
                    or room["free"] - total < KEEP_FREE_GB << 30
                ):
                    raise Refused(f"{name} unpacks to {total >> 20} MB: there isn't room.")
                tops = {m.filename.split("/")[0] for m in members}
                top = (
                    f"{tops.pop()}/"
                    if len(tops) == 1 and all("/" in m.filename for m in members)
                    else ""
                )
                for member in members:
                    inner = prefix + zip_name(member).removeprefix(zip_name_of(top))
                    what = self._unpack_one(archive, member, inner, replace, counts, depth)
                    counts[what] = counts.get(what, 0) + 1
        except zipfile.BadZipFile as error:
            raise Refused(f"{name} can't be opened as a zip ({error}).") from error
        finally:
            path.unlink(missing_ok=True)

    def _unpack_one(self, archive, member, inner: str, replace, counts, depth: int) -> str:
        suffix = Path(inner).suffix.lower()
        cap = (WHOLE_FILE_MB if suffix in MARKDOWN else self.limits.max_mb) * 1024 * 1024
        if ".." in member.filename.replace("\\", "/").split("/"):
            return "skipped (a path outside the zip)"
        if member.flag_bits & 1:
            return "skipped (password-protected)"
        if member.file_size > cap:
            return "skipped (too large)"
        if suffix == ".zip" and depth:
            return "skipped (a zip inside a zip inside a zip)"
        handle, temp = tempfile.mkstemp(dir=self.incoming, suffix=".part")
        try:
            with os.fdopen(handle, "wb") as out, archive.open(member) as data:
                written = 0
                while (block := data.read(1 << 20)) and written <= member.file_size:
                    written += len(block)
                    out.write(block)
        except (zipfile.BadZipFile, OSError):
            Path(temp).unlink(missing_ok=True)
            return "skipped (damaged in the zip)"
        if written > member.file_size:  # it lied about its size: a zip bomb
            Path(temp).unlink()
            return "skipped (larger than the zip said)"
        if suffix == ".zip":
            try:
                self._unpack_into(inner, Path(temp), replace, counts, inner[:-4] + "/", depth + 1)
            except Refused:
                return "skipped (a zip inside that can't be opened)"
            return "zip(s) inside unpacked"
        what = self.add_file(inner, Path(temp), replace, notify=False)[1]
        return "not added (a file of that name exists)" if what == "exists" else what

    def clear(self) -> int:
        """Start afresh: every file and every recorded answer goes."""
        docs = self.documents()
        for doc in docs:
            self.delete(doc.id)
        self._sql("DELETE FROM answers")
        return len(docs)

    def _changed(self) -> None:
        """Files were added or removed: tell the listeners on the event loop (the upload
        page calls from its own thread)."""
        if self._loop and not self._loop.is_closed():
            for listener in list(self.on_change):
                self._loop.call_soon_threadsafe(listener)

    def delete(self, doc_id: str) -> bool:
        doc = self.get(doc_id)
        if not doc:
            return False
        if self.vectors:
            self._sql(
                "DELETE FROM vectors WHERE rowid IN (SELECT rowid FROM chunks WHERE doc_id = ?)",
                (doc_id,),
            )
        for table in ("parts", "chunks"):
            self._sql(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))
        self._sql("DELETE FROM documents WHERE id = ?", (doc_id,))
        Path(doc.path).unlink(missing_ok=True)
        if doc.converted:
            doc.converted.unlink(missing_ok=True)
        shutil.rmtree(doc.media, ignore_errors=True)
        self._changed()
        logger.info(f"Library: {doc.name} deleted")
        return True

    def approve(self, document: str) -> Document | None:
        """Yes to reading a large scan: it goes back in the queue."""
        doc = self.find(document)
        if not doc or doc.status != "asking":
            return None
        self._set(doc.id, approved=1, status="waiting", note=None)
        self._enqueue(doc.id)
        return self.get(doc.id)

    # --- the background worker ---

    def start(self) -> None:
        """Start processing, and carry on with files left unfinished by a restart."""
        self._loop = asyncio.get_running_loop()
        self._workers = [asyncio.create_task(self._work(), name=f"library-{n}") for n in (1, 2)]
        for doc in self.documents():
            if doc.status in ("waiting", "processing", "paused"):
                self._enqueue(doc.id)
            elif doc.status == "ready" and doc.later:
                self._enqueue(doc.id, later=True)
        self._describe_soon()
        for stale in self.incoming.glob("*.part"):  # uploads abandoned over a day ago
            if time.time() - stale.stat().st_mtime > 86400:
                stale.unlink(missing_ok=True)

    async def stop(self) -> None:
        running = [t for t in (*self._workers, self._describing, *self._embedding) if t]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def embedded(self) -> None:
        """Wait for the meaning index to catch up with what has been read."""
        while self._embedding:
            await asyncio.gather(*self._embedding, return_exceptions=True)

    def _enqueue(self, doc_id: str, later: bool = False) -> None:
        if not self._loop or (doc_id, later) in self._queued:
            return
        doc = self.get(doc_id)
        self._queued.add((doc_id, later))
        self._order += 1
        item = (int(later), doc.bytes if doc else 0, self._order, doc_id)  # smallest first
        self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    async def _work(self) -> None:
        while True:
            try:
                later, _, _, doc_id = await asyncio.wait_for(self._queue.get(), RECHECK_SECONDS)
            except TimeoutError:
                await self._idle()
                continue
            self._queued.discard((doc_id, bool(later)))
            doc = self.get(doc_id)
            if later:
                if doc and doc.later and doc.status == "ready":
                    try:
                        await self._read_later(doc)
                    except Exception:  # its text stays searchable; only the pictures are lost
                        logger.exception(f"Library: couldn't read the pictures in {doc.name}")
                        self._set(doc.id, later=0, note="Its pictures couldn't be read.")
                continue
            if not doc or doc.status in ("ready", "asking", "failed"):
                continue
            started = time.monotonic()
            try:
                finished = await self._process(doc)
            except Unreadable as error:
                self._set(doc.id, status="failed", note=str(error))
                continue
            except Exception as error:
                if not self.get(doc.id):  # deleted while it was read
                    self._clear(doc.id)
                    continue
                logger.exception(f"Library: couldn't read {doc.name}")
                self._set(doc.id, status="failed", note=f"Couldn't read it: {error}")
                continue
            if not self.get(doc.id):  # deleted while it was read: no leftovers
                self._clear(doc.id)
                continue
            if not finished:
                continue  # waiting for a yes, or for tomorrow's scans
            self._set(doc.id, status="ready")
            logger.info(f"Library: {doc.name} ready in {time.monotonic() - started:.0f} s")
            for listener in list(self.on_ready):
                listener(self.get(doc.id))
            if (self.get(doc.id) or doc).later:
                self._enqueue(doc.id, later=True)
            self._describe_soon()
            if self._queue.empty():
                await self._idle()

    def _describe_soon(self) -> None:
        if (
            self._describer
            and self._loop
            and not (self._describing and not self._describing.done())
        ):
            self._describing = asyncio.create_task(self._describe_all(), name="describe")

    async def _describe_all(self) -> None:
        """One line on what each ready file is, DESCRIBE_AT_ONCE files per call, behind
        the reading. A file Claude says nothing about gets "" (not asked again)."""
        while todo := [d for d in self.documents() if d.status == "ready" and d.about is None]:
            batch = todo[:DESCRIBE_AT_ONCE]
            openings = [(d.name, self._opening(d)) for d in batch]
            try:
                lines = await self._describer(openings)
            except Exception as error:  # tried again when the next file is ready
                logger.warning(f"Library: couldn't describe files: {error}")
                return
            for doc, line in zip_longest(batch, lines[: len(batch)], fillvalue=""):
                self._set(doc.id, about=" ".join(str(line).split())[:200])

    def _opening(self, doc: Document) -> str:
        parts = self._parts(doc.id)
        places = ", ".join(p["location"] for p in parts[:15])
        return f"Parts: {places}\n" + "\n".join(p["text"] for p in parts)[:OPENING]

    async def _idle(self) -> None:
        """Between files: embed chunks that missed out (offline earlier, or a new model),
        and give files paused by the daily scan limit another go."""
        await self.embed_missing()
        for doc in self.documents():
            if doc.status == "paused" and self.scans_left() > 0:
                self._enqueue(doc.id)

    def _set(self, doc_id: str, **values) -> None:
        names = ", ".join(f"{key} = ?" for key in values)
        self._sql(f"UPDATE documents SET {names} WHERE id = ?", (*values.values(), doc_id))

    async def _process(self, doc: Document) -> bool:
        """Read a document. False if it stopped to wait (a yes, or tomorrow's scans)."""
        path, suffix = Path(doc.path), doc.suffix
        if suffix in CONVERT:
            async with self._office:
                await asyncio.to_thread(convert, path, doc.converted, self.root)
            path, suffix = doc.converted, doc.converted.suffix
        elif suffix not in PDF | IMAGES | MARKDOWN | ROWS | TEXT:
            await asyncio.to_thread(must_be_text, path, suffix)
        if suffix in PDF:
            return await self._read_pdf(doc, path)
        self._clear(doc.id)
        self._set(doc.id, status="processing", done=0, note=None)
        if suffix in IMAGES:
            if self.scans_left() < 1:
                self._set(doc.id, status="paused", note=paused_note())
                return False
            self._count_scans(1)
            with tempfile.TemporaryDirectory() as folder:
                image = await asyncio.to_thread(save_picture, path.read_bytes(), Path(folder) / "1")
                if not image:
                    raise Unreadable("Alfred can't open this image.")
                parts = [Part("page 1", (await self._see([image]))[0], scanned=True)]
        elif suffix in MARKDOWN:
            markdown = await asyncio.to_thread(to_markdown, path)
            text = sections(markdown, suffix) + (word_charts(path) if suffix == ".docx" else [])
            if suffix == ".pptx":
                text += (await asyncio.to_thread(slide_diagrams, path))[0]  # exact, no Claude
            await self._store(doc, 0, text)
            later = int(bool(self._vision) and suffix in (".docx", ".pptx"))
            self._set(doc.id, total=len(text), done=len(text), later=later)
            parts = []
        elif suffix in (".md", ".markdown") and path.stat().st_size <= WHOLE_FILE_MB << 20:
            markdown = await asyncio.to_thread(path.read_text, text_encoding(path), "replace")
            parts = sections(markdown, suffix)  # by its headings: "§ Installation"
        else:  # spreadsheets and text: read a batch at a time, whatever the size
            batches = row_batches(path) if suffix in ROWS else line_batches(path)
            done = 0
            while (batch := await asyncio.to_thread(next, batches, None)) is not None:
                await self._store(doc, done, batch)
                done += len(batch)
                self._set(doc.id, done=done, total=done)
            parts = []
        if parts:
            self._set(doc.id, total=len(parts))
            await self._store(doc, 0, parts)
            self._set(doc.id, done=len(parts))
        if suffix in SPREADSHEETS:
            self._set(doc.id, note="Figures are worked out from the file itself.")
        return True

    async def _read_later(self, doc: Document) -> None:
        """The second pass, once every file's text is in: a Word or PowerPoint file's
        pictures, and a deck's slides with loose drawings, as Claude sees them."""
        path = Path(doc.readable)
        parts = await self._pictures(doc, path, path.suffix.lower(), first=doc.total)
        if path.suffix.lower() == ".pptx":
            parts += await self._drawings(doc, path, first=doc.total + len(parts))
        if parts and self.get(doc.id):
            await self._store(doc, doc.total, parts)
            self._set(doc.id, total=doc.total + len(parts), done=doc.total + len(parts))
        self._set(doc.id, later=0)

    async def _drawings(self, doc: Document, path: Path, first: int) -> list[Part]:
        """Slides whose drawings record no joins, drawn by LibreOffice (as a PDF, one page a
        slide) and read by Claude, within the daily scan limit."""
        _, loose, slides = await asyncio.to_thread(slide_diagrams, path)
        if not loose or not libreoffice():
            return []
        with tempfile.TemporaryDirectory() as folder:
            pdf = Path(folder) / f"{doc.id}.pdf"
            try:
                async with self._office:
                    await asyncio.to_thread(convert, path, pdf, self.root, SLIDES_AS_PDF)
            except Unreadable as error:  # the deck's pictures and text stay as they are
                logger.warning(f"Library: couldn't draw {doc.name}'s slides: {error}")
                return []
            if await asyncio.to_thread(page_count, pdf) != slides:
                logger.warning(f"Library: {doc.name}'s slides didn't come out one a page")
                return []
            async with self._scans:
                loose = loose[: max(self.scans_left(), 0)]
                self._count_scans(len(loose))
            shown = await asyncio.to_thread(slide_pictures, pdf, loose, doc.media, first)
        texts = await self._see([image for _, image in shown])
        return [
            Part(f"slide {n}, drawing", text, True)
            for (n, _), text in zip(shown, texts, strict=True)
        ]

    async def _pictures(self, doc: Document, path: Path, suffix: str, first: int) -> list[Part]:
        """Charts, diagrams and photos in a Word or PowerPoint file, read by vision (within
        the daily scan limit), each saved as <its part's number>.png for the agent to see."""
        found = pictures_in(path, suffix) if self._vision and suffix in (".docx", ".pptx") else []
        kept: list[tuple[str, Path]] = []
        async with self._scans:
            if len(found) > (left := max(self.scans_left(), 0)):
                self._set(
                    doc.id, note=f"{len(found) - left} picture(s) not read: today's scan limit"
                )
            for at, picture in found[:left]:  # skipping drawings Pillow can't open
                saved = await asyncio.to_thread(
                    save_picture, picture, doc.media / str(first + len(kept))
                )
                if saved:
                    kept.append((at, saved))
            self._count_scans(len(kept))
        if not kept:
            return []
        texts = await self._see([png for _, png in kept])
        return [Part(at, text, scanned=True) for (at, _), text in zip(kept, texts, strict=True)]

    def _clear(self, doc_id: str) -> None:
        if self.vectors:
            self._sql(
                "DELETE FROM vectors WHERE rowid IN (SELECT rowid FROM chunks WHERE doc_id = ?)",
                (doc_id,),
            )
        for table in ("parts", "chunks"):
            self._sql(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))

    async def _read_pdf(self, doc: Document, path: Path) -> bool:
        """Page by page, carrying on where it stopped (a restart, or the daily limit)."""
        if doc.done == 0:
            self._clear(doc.id)
            total, scans = await asyncio.to_thread(pdf_survey, path)
            self._set(doc.id, total=total)
            if scans > self.limits.ask_pages and not doc.approved:
                tokens = scans * TOKENS_PER_SCAN
                self._set(
                    doc.id,
                    status="asking",
                    note=f"{scans} scanned pages, about {amount(tokens)} tokens of your Claude "
                    "plan to read. Read it?",
                )
                for listener in list(self.on_asking):
                    listener(self.get(doc.id))
                return False
        self._set(doc.id, status="processing", note=None)
        total = (self.get(doc.id) or doc).total
        with tempfile.TemporaryDirectory(prefix="alfred-pages-") as folder:
            for start in range((self.get(doc.id) or doc).done, total, PAGES_AT_ONCE):
                pages = await asyncio.to_thread(pdf_pages, path, start, PAGES_AT_ONCE, Path(folder))
                pages, out_of_scans = within_budget(pages, self.scans_left())
                images = [image for _, image in pages if image]
                self._count_scans(len(images))
                if not pages:
                    self._set(doc.id, status="paused", note=paused_note())
                    return False
                seen = iter(await self._see(images)) if images else iter(())
                parts, pictures = [], []
                for i, (text, image) in enumerate(pages):
                    at = f"page {start + i + 1}"
                    parts.append(Part(at, text) if text else Part(at, next(seen), True))
                    if text and image:  # a chart or figure on a page of text
                        pictures.append(Part(f"{at}, picture", next(seen), True))
                await self._store(doc, start, parts)
                if pictures:  # kept after the pages, so pages stay numbered in order
                    before = self._sql(
                        "SELECT count(*) FROM parts WHERE doc_id = ? AND ord >= ?", (doc.id, total)
                    )[0][0]
                    await self._store(doc, total + before, pictures)
                self._set(doc.id, done=start + len(pages))
                if out_of_scans:
                    self._set(doc.id, status="paused", note=paused_note())
                    return False
        scanned = self._sql(
            "SELECT count(*) FROM parts WHERE doc_id = ? AND scanned = 1", (doc.id,)
        )[0][0]
        if scanned:
            self._set(doc.id, note=f"{scanned} scanned page(s) read by Claude vision")
        return True

    def scans_left(self) -> int:
        today = time.strftime("%Y-%m-%d")
        rows = self._sql("SELECT pages FROM vision_days WHERE day = ?", (today,))
        return self.limits.daily_pages - (rows[0][0] if rows else 0)

    def _count_scans(self, pages: int) -> None:
        if pages:
            self._sql(
                "INSERT INTO vision_days (day, pages) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET pages = pages + excluded.pages",
                (time.strftime("%Y-%m-%d"), pages),
            )

    async def _see(self, images: list[Path]) -> list[str]:
        """Pictures read by Claude, a few per call, a few calls at once. A blank one isn't
        sent, nor one read before (a template or a chart repeated across files)."""
        if not self._vision:
            return ["[unclear: scanned page, no vision configured]"] * len(images)
        prints = [hashlib.sha256(image.read_bytes()).hexdigest() for image in images]
        texts = [self._seen.get(p) for p in prints]
        for i, image in enumerate(images):
            if texts[i] is None and await asyncio.to_thread(blank, image):
                texts[i] = "[blank]"
        todo = [i for i, text in enumerate(texts) if text is None]
        slots, size = asyncio.Semaphore(self.limits.vision_at_once), self.limits.vision_per_call

        async def batch(group: list[int]) -> list[str]:
            async with slots:
                read = await self._vision([images[i] for i in group])
            return (read + ["[unclear]"] * len(group))[: len(group)]

        groups = [todo[i : i + size] for i in range(0, len(todo), size)]
        for group, read in zip(groups, await asyncio.gather(*map(batch, groups)), strict=True):
            for i, text in zip(group, read, strict=True):
                texts[i] = text
                if text != "[unclear]":
                    self._seen[prints[i]] = text
        while len(self._seen) > SEEN_AT_MOST:
            self._seen.pop(next(iter(self._seen)))
        return texts

    async def _store(self, doc: Document, first: int, parts: list[Part]) -> None:
        """Keep the parts and their chunks (searchable by keyword at once), and embed the
        chunks for meaning search in the background. Nothing for a file deleted meanwhile."""
        new: list[tuple[int, str]] = []
        with self._lock:
            if not self._conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc.id,)).fetchone():
                return
            for ord_, part in enumerate(parts, start=first):
                self._conn.execute(
                    "INSERT OR REPLACE INTO parts (doc_id, ord, location, text, scanned) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc.id, ord_, part.location, part.text, int(part.scanned)),
                )
                label = f"{doc.name} › {part.location}"
                for chunk in chunks(part.text):
                    cursor = self._conn.execute(
                        "INSERT INTO chunks (label, text, doc_id, ord) VALUES (?, ?, ?, ?)",
                        (label, chunk, doc.id, ord_),
                    )
                    new.append((cursor.lastrowid, f"{label}\n{chunk}"))
            self._conn.commit()
        if new and self.vectors:
            task = asyncio.create_task(self._embed_slowly(new))
            self._embedding.add(task)
            task.add_done_callback(self._embedding.discard)

    async def _embed_slowly(self, rows: list[tuple[int, str]]) -> None:
        async with self._embed_slots:
            await self._embed(rows)

    async def _embed(self, rows: list[tuple[int, str]]) -> bool:
        """Embed (rowid, text) chunks into the meaning index. False if it couldn't (they're
        tried again later: embed_missing)."""
        if not self.vectors or not rows:
            return False
        for i in range(0, len(rows), EMBED_AT_ONCE):
            batch = rows[i : i + EMBED_AT_ONCE]
            try:
                vectors = await self._embedder([text for _, text in batch])
            except Exception as error:
                logger.warning(f"Library: couldn't embed ({error}); keyword search meanwhile")
                return False
            with self._lock:  # delete first: vec0 ignores OR REPLACE, and two embeddings of
                # the same chunks can race (a file's own, and the idle catch-up)
                self._conn.executemany(
                    "DELETE FROM vectors WHERE rowid = ?", [(r,) for r, _ in batch]
                )
                self._conn.executemany(
                    "INSERT INTO vectors (rowid, embedding) VALUES (?, ?)",
                    [(rowid, packed(v)) for (rowid, _), v in zip(batch, vectors, strict=True)],
                )
                self._conn.commit()
        return True

    async def embed_missing(self, at_most: int = 2000) -> int:
        """Embed chunks that have no embedding yet. Returns how many were done."""
        if not self.vectors:
            return 0
        rows = self._sql(
            "SELECT rowid, label, text FROM chunks WHERE rowid NOT IN (SELECT rowid FROM vectors) "
            "LIMIT ?",
            (at_most,),
        )
        missing = [(r["rowid"], f"{r['label']}\n{r['text']}") for r in rows]
        return len(missing) if missing and await self._embed(missing) else 0

    # --- what the agent uses ---

    async def search(self, query: str, document: str = "", limit: int = 8) -> list[dict]:
        """The best passages for `query`, best first: keyword and meaning searches, their
        rankings merged (reciprocal rank fusion). Keyword only when embeddings are off."""
        doc = self.find(document) if document else None
        ranked = [self._by_keyword(query, doc)]
        if self.vectors and self._embedder:
            try:
                ranked.append(await self._by_meaning(query, doc))
            except Exception as error:
                logger.warning(f"Library: meaning search failed ({error}); keyword only")
        scores: dict[int, float] = {}
        for ranking in ranked:
            for rank, rowid in enumerate(ranking):
                scores[rowid] = scores.get(rowid, 0) + 1 / (FUSION_K + rank + 1)
        found, seen = [], set()
        for rowid in sorted(scores, key=scores.get, reverse=True):
            row = self._sql("SELECT doc_id, ord, label, text FROM chunks WHERE rowid = ?", (rowid,))
            if not row or (row[0]["doc_id"], row[0]["ord"]) in seen:
                continue  # one passage per location: the best one
            seen.add((row[0]["doc_id"], row[0]["ord"]))
            document_name, location = row[0]["label"].split(" › ", 1)
            found.append(
                {"document": document_name, "location": location, "passage": row[0]["text"][:900]}
            )
            if len(found) == limit:
                break
        return found

    def _by_keyword(self, query: str, doc: Document | None) -> list[int]:
        words = words_in(query)
        if not words:
            return []
        sql = (
            "SELECT rowid FROM chunks WHERE chunks MATCH ?"
            + (" AND doc_id = ?" if doc else "")
            + " ORDER BY bm25(chunks, 2.0, 1.0) LIMIT ?"
        )
        args = [" OR ".join(f'"{w}"' for w in words), *([doc.id] if doc else []), FOUND_EACH_WAY]
        return [row[0] for row in self._sql(sql, args)]

    async def _by_meaning(self, query: str, doc: Document | None) -> list[int]:
        (vector,) = await self._embedder([query])
        many = FOUND_EACH_WAY * (5 if doc else 1)  # one file: look wider, then keep its own
        rows = self._sql(
            "SELECT rowid FROM vectors WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (packed(vector), many),
        )
        rowids = [row[0] for row in rows]
        if doc and rowids:
            marks = ",".join("?" * len(rowids))
            sql = f"SELECT rowid FROM chunks WHERE doc_id = ? AND rowid IN ({marks})"
            mine = {r[0] for r in self._sql(sql, (doc.id, *rowids))}
            rowids = [r for r in rowids if r in mine]
        return rowids[:FOUND_EACH_WAY]

    def read(self, document: str, location: str) -> dict:
        """The full text at a location: "page 12", "12-14", "slide 4", "sheet Sales",
        a section's heading, or "lines 81-160"."""
        doc = self.find(document)
        if not doc:
            return {"error": f"no document called {document!r}: see list_documents"}
        parts = self._parts(doc.id)
        chosen = pick(parts, location)
        if not chosen:
            names = ", ".join(p["location"] for p in parts[:40])
            return {"error": f"no {location!r} in {doc.name}. Its locations: {names}"}
        text, shown, pictures = "", [], []
        for part in chosen:
            piece = (
                f"[{part['location']}]{' (scanned)' if part['scanned'] else ''}\n{part['text']}\n\n"
            )
            if text and len(text) + len(piece) > READ_AT_MOST:
                break
            text += piece[:READ_AT_MOST]
            shown.append(part["location"])
            if picture := next(doc.media.glob(f"{part['ord']}.*"), None):
                pictures.append(str(picture))
        more = chosen[len(shown) :]
        return (
            {"document": doc.name, "text": text, "file": doc.readable}
            | ({"pictures": pictures} if pictures else {})
            | ({"more": f"continues at {more[0]['location']}"} if more else {})
            | (
                {"still_processing": f"{doc.done} of {doc.total} done"}
                if doc.status != "ready"
                else {}
            )
        )

    def check(self, citations: list[dict]) -> list[str | None]:
        """For each citation (document, location, quote): None if the quote is at that
        location (or the next or previous one, for a quote across a page break), else why
        not."""
        results, texts = [], {}  # each document's text loaded once
        for citation in citations:
            doc = self.find(str(citation.get("document", "")))
            quote = normal(str(citation.get("quote", "")))
            if not doc:
                results.append("that document isn't in the library")
                continue
            if len(quote) < 8:
                results.append("the quote is too short to check: quote a full phrase")
                continue
            if doc.id not in texts:
                texts[doc.id] = self._parts(doc.id)
            parts = texts[doc.id]
            here = pick(parts, str(citation.get("location", "")))
            if not here:
                results.append(f"{doc.name} has no {citation.get('location')!r}")
                continue
            if self._quoted(quote, here, parts):
                results.append(None)
            else:
                results.append(f"the quote isn't at {citation.get('location')} of {doc.name}")
        return results

    @staticmethod
    def _quoted(quote: str, here: list[dict], parts: list[dict]) -> bool:
        """The quote is at the cited location, or runs across its edge into the page
        before or after (a page break) without lying wholly on that other page."""

        def found(part_list: list[dict]) -> bool:
            text = normal(" ".join(p["text"] for p in part_list))
            loose = any(p["scanned"] for p in part_list)
            return quote in text or (loose and loosely_in(quote, text))

        if found(here):
            return True
        by_ord = {p["ord"]: p for p in parts}
        first, last = here[0]["ord"], here[-1]["ord"]
        for neighbour, joined in (
            (by_ord.get(first - 1), [by_ord.get(first - 1), *here]),
            (by_ord.get(last + 1), [*here, by_ord.get(last + 1)]),
        ):
            if neighbour and found(joined) and not found([neighbour]):
                return True
        return False

    def record(self, project: str, answer: str, citations: list[dict]) -> None:
        self._sql(
            "INSERT INTO answers (at, project, answer, citations) VALUES (?, ?, ?, ?)",
            (time.time(), project, answer, json.dumps(citations)),
        )

    def scanned_at(self, document: str, location: str) -> bool:
        doc = self.find(document)
        return bool(doc and any(p["scanned"] for p in pick(self._parts(doc.id), location)))

    def _parts(self, doc_id: str) -> list[dict]:
        rows = self._sql("SELECT * FROM parts WHERE doc_id = ? ORDER BY ord", (doc_id,))
        return [dict(r) for r in rows]


# --- reading files (pure functions: tests use them directly) ---


def brief(doc: Document) -> str:
    """ " (12 pages)", " (processing)": for the list of files in prompts."""
    unit = "pages" if doc.suffix in PDF else "parts"
    details = [f"{doc.total} {unit}"] if doc.total and doc.status == "ready" else []
    details += [] if doc.status == "ready" else [doc.status]
    return f" ({', '.join(details)})" if details else ""


def about(doc: Document) -> str:
    return f": {doc.about}" if doc.about else ""


def ago(when: float) -> str:
    """ "just now", "5 min ago", "3 h ago", "20 Nov": when a file was added."""
    seconds = time.time() - when
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60:.0f} min ago"
    return (
        f"{seconds // 3600:.0f} h ago"
        if seconds < 86400
        else time.strftime("%d %b", time.localtime(when))
    )


def amount(tokens: int) -> str:
    return f"{tokens / 1e6:.1f} million" if tokens >= 1e6 else f"{tokens:,}"


def paused_note() -> str:
    return "Today's limit for reading scanned pages is used up: it carries on tomorrow."


def within_budget(pages: list, scans_left: int) -> tuple[list, bool]:
    """The pages that fit today's scan budget, in order, and whether it ran out."""
    kept, scans = [], 0
    for text, image in pages:
        if image and scans == scans_left:
            if not text:
                return kept, True
            image = None  # its text now; the picture on it isn't read (today's limit)
        scans += bool(image)
        kept.append((text, image))
    return kept, False


def packed(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def openai_embedder(api_key: str, model: str) -> Embedder:
    """OpenAI embeddings, shortened to DIMENSIONS numbers."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, max_retries=3)

    async def embed(texts: list[str]) -> list[list[float]]:
        reply = await client.embeddings.create(
            model=model, input=[t[:20_000] for t in texts], dimensions=DIMENSIONS
        )
        return [item.embedding for item in reply.data]

    return embed


def pdf_survey(path: Path) -> tuple[int, int]:
    """(pages, how many of them are scans), without rendering anything."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        scans = 0
        for i in range(len(pdf)):
            page = pdf[i]
            textpage = page.get_textpage()
            scans += len(textpage.get_text_bounded().strip()) < SCANNED_BELOW
            textpage.close()
            page.close()
        return len(pdf), scans
    finally:
        pdf.close()


def row_batches(path: Path, parts_at_once: int = 20):
    """A spreadsheet, a few parts at a time: ROWS_PER_PART rows each, as a Markdown table
    with the sheet's header row repeated, so any part reads on its own."""
    batch: list[Part] = []
    for part in row_parts(path):
        batch.append(part)
        if len(batch) == parts_at_once:
            yield batch
            batch = []
    if batch:
        yield batch


def row_parts(path: Path):
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding=text_encoding(path), errors="replace") as file:
            yield from table_parts("", csv.reader(file))
        return
    from openpyxl import load_workbook

    # Saved results, and the formulas: a file made by a script has no saved results, so a
    # total would read as blank; its formula is shown instead, to work out.
    book = load_workbook(path, read_only=True, data_only=True)
    formulas = load_workbook(path, read_only=True)
    try:
        for sheet, written in zip(book.worksheets, formulas.worksheets, strict=True):
            rows = zip_longest(
                sheet.iter_rows(values_only=True), written.iter_rows(values_only=True)
            )
            merged = (
                tuple(v if v is not None else f for v, f in zip_longest(a or (), b or ()))
                for a, b in rows
            )
            yield from table_parts(f"sheet {sheet.title}", merged)
    finally:
        book.close()
        formulas.close()


def table_parts(sheet: str, rows):
    def line(values) -> str:
        cells = (
            "" if v is None else str(v).replace("|", "/").replace("\n", " ")[:200] for v in values
        )
        return "| " + " | ".join(cells) + " |"

    header, body, first = None, [], 2
    for number, values in enumerate(rows, start=1):
        if header is None:
            header = line(values)
            continue
        if not any(v not in (None, "") for v in values):
            continue
        body.append(line(values))
        if len(body) == ROWS_PER_PART:
            yield table_part(sheet, header, body, first, number)
            body, first = [], number + 1
    if header is not None and (body or first == 2):
        yield table_part(sheet, header, body, first, first + len(body) - 1)


def table_part(sheet: str, header: str, body: list[str], first: int, last: int) -> Part:
    where = f"rows {first}-{max(first, last)}"
    rule = "|" + " --- |" * max(header.count("|") - 1, 1)
    return Part(f"{sheet}, {where}" if sheet else where, "\n".join([header, rule, *body]))


def line_batches(path: Path, parts_at_once: int = 20):
    """A text file, LINES_PER_PART lines a part, a few parts at a time."""
    batch: list[Part] = []
    with path.open(encoding=text_encoding(path), errors="replace") as file:
        lines, first = [], 1
        for number, text in enumerate(file, start=1):
            lines.append(text.rstrip("\n"))
            if len(lines) == LINES_PER_PART:
                batch.append(Part(f"lines {first}-{number}", "\n".join(lines)))
                lines, first = [], number + 1
            if len(batch) == parts_at_once:
                yield batch
                batch = []
        if any(t.strip() for t in lines):
            batch.append(Part(f"lines {first}-{first + len(lines) - 1}", "\n".join(lines)))
    if batch:
        yield batch


def text_encoding(path: Path) -> str:
    """UTF-8 (a byte-order mark dropped), UTF-16, or else Windows' cp1252, as Excel on
    Windows saves a CSV: so "Peñafrancia" stays "Peñafrancia"."""
    with path.open("rb") as file:
        start = file.read(1 << 24)
    if start[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(start, final=len(start) < 1 << 24)
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8-sig"


def must_be_text(path: Path, suffix: str) -> None:
    """A file of a kind not known here is read as text, unless it's binary."""
    with path.open("rb") as file:
        if b"\0" in file.read(8192):
            raise Unreadable(f"Alfred can't read {suffix or 'these'} files.")


def libreoffice() -> str | None:
    mac = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return shutil.which("soffice") or (mac if Path(mac).exists() else None)


SLIDES_AS_PDF = 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'


def slide_pictures(
    pdf: Path, numbers: list[int], media: Path, first: int
) -> list[tuple[int, Path]]:
    """Slides (pages of the deck's PDF) drawn at the size Claude reads them, kept with the
    file's pictures for the agent to look at again."""
    import pypdfium2 as pdfium

    document, shown = pdfium.PdfDocument(pdf), []
    try:
        for number in numbers:
            page = document[number - 1]
            width, height = page.get_size()
            scale = min(200 / 72, (CLAUDE_PIXELS / (width * height)) ** 0.5)
            saved = save_picture(page.render(scale=scale).to_pil(), media / str(first + len(shown)))
            page.close()
            if saved:
                shown.append((number, saved))
    finally:
        document.close()
    return shown


def convert(path: Path, copy: Path, root: Path, to: str = "") -> None:
    """An Office file as another kind (`copy`'s suffix, or `to`: a LibreOffice filter), by
    LibreOffice."""
    if not (office := libreoffice()):
        install = "brew install --cask libreoffice" if sys.platform == "darwin" else "./setup.sh"
        raise Unreadable(f"To read {path.suffix} files, install LibreOffice: {install}")
    profile = f"-env:UserInstallation={(root / 'office').resolve().as_uri()}"  # its own
    with tempfile.TemporaryDirectory() as folder:
        try:
            subprocess.run(
                [office, "--headless", profile, "--convert-to", to or copy.suffix[1:], "--outdir",
                 folder, path],
                capture_output=True, timeout=120,
            )  # fmt: skip
        except subprocess.TimeoutExpired as error:
            raise Unreadable("LibreOffice took too long to convert it.") from error
        made = Path(folder) / f"{path.stem}{copy.suffix}"
        if not made.exists():
            raise Unreadable("LibreOffice couldn't convert it.")
        shutil.move(made, copy)


def pictures_in(path: Path, suffix: str) -> list[tuple[str, bytes]]:
    """The pictures in a PowerPoint file, slide by slide ("slide 4, picture 2"), or in a
    Word file ("picture 3"; small ones there are icons and bullets, left out)."""
    if suffix == ".pptx":
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        def walk(shapes):  # into groups; placeholders holding a picture have an image too
            for shape in shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    yield from walk(shape.shapes)
                elif image := getattr(shape, "image", None):
                    yield image.blob

        slides = enumerate(Presentation(path).slides, start=1)
        found = unique_blobs((n, blob) for n, slide in slides for blob in walk(slide.shapes))
        return [(f"slide {n}, picture {i}", blob) for i, (n, blob) in enumerate(found, start=1)]
    with zipfile.ZipFile(path) as word:
        media = [i for i in word.infolist() if i.filename.startswith("word/media/")]
        blobs = [word.read(i) for i in media if i.file_size >= PICTURE_BYTES_AT_LEAST]
    return [(f"picture {i}", blob) for i, blob in enumerate(blobs, start=1)]


def unique_blobs(items):
    """Each image once, where it first appears (a logo or a chart repeated on slides)."""
    seen: set[str] = set()
    for at, blob in items:
        if (digest := hashlib.sha256(blob).hexdigest()) not in seen:
            seen.add(digest)
            yield at, blob


OOXML = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
}
DRAWN_SHAPES = ("arrow", "Arrow", "flowChart", "chevron", "curved", "line")  # shapes, no joins


def slide_diagrams(path: Path) -> tuple[list[Part], list[int], int]:
    """A deck's diagrams, read from the file itself: SmartArt as its outline, and boxes
    joined by arrows as "A → B" (PowerPoint records which shapes an arrow joins). Also
    the slides whose drawings record no joins (loose arrows, block arrows, freehand
    shapes), for Claude to look at instead, and how many slides there are. A slide whose
    diagram can't be read is left to its text."""
    from pptx import Presentation

    slides = Presentation(path).slides
    parts, loose = [], []
    for number, slide in enumerate(slides, start=1):
        try:
            found, unclear = diagrams_on(slide)
        except Exception:  # a diagram this doesn't understand: the slide's text still counts
            logger.debug(f"Library: couldn't read the diagrams on slide {number} of {path.name}")
            continue
        parts += [
            Part(f"slide {number}, diagram" + (f" {n}" if len(found) > 1 else ""), text)
            for n, text in enumerate(found, start=1)
        ]
        if unclear:
            loose.append(number)
    return parts, loose, len(slides)


def diagrams_on(slide) -> tuple[list[str], bool]:
    """A slide's diagrams as text, and whether it has drawings that record no joins."""
    root = slide.element
    names = {
        int(c.get("id")): words(c.getparent().getparent()) or c.get("name", "")
        for c in root.iterfind(".//p:cNvPr", OOXML)
    }
    joins, unjoined = [], 0
    for join in root.iterfind(".//p:cNvCxnSpPr", OOXML):
        start, end = join.find("a:stCxn", OOXML), join.find("a:endCxn", OOXML)
        if start is None or end is None:
            unjoined += 1
        else:
            joins.append(
                f"{names.get(int(start.get('id')), '?')} → {names.get(int(end.get('id')), '?')}"
            )
    smartart = [
        smartart_outline(slide.part.related_part(rid).blob)
        for ids in root.iterfind(".//dgm:relIds", OOXML)
        if (rid := ids.get(f"{{{OOXML['r']}}}dm"))
    ]
    found = [f"Diagram:\n{o}" for o in smartart if o]
    if joins:
        found.append("Diagram (arrows between shapes):\n" + "\n".join(f"- {j}" for j in joins))
    drawn = 0
    for shape in root.iterfind(".//p:sp/p:spPr", OOXML):
        preset = shape.find("a:prstGeom", OOXML)
        kind = preset.get("prst", "") if preset is not None else ""
        drawn += shape.find("a:custGeom", OOXML) is not None or any(k in kind for k in DRAWN_SHAPES)
    return found, bool(unjoined or (drawn >= 2 and not found))


def smartart_outline(data: bytes) -> str:
    """A SmartArt diagram's boxes as a nested list, from its data model: every box's text,
    and which box belongs under which."""
    from lxml import etree

    root = etree.fromstring(data)
    kinds, text = {}, {}
    for point in root.iterfind(".//dgm:pt", OOXML):
        kinds[point.get("modelId")] = point.get("type", "node")
        text[point.get("modelId")] = words(point)
    under: dict[str, list[tuple[int, str]]] = {}
    for link in root.iterfind(".//dgm:cxn", OOXML):
        if link.get("type", "parOf") == "parOf" and link.get("destId") in kinds:
            under.setdefault(link.get("srcId"), []).append(
                (int(link.get("srcOrd", 0)), link.get("destId"))
            )

    def lines(model: str, depth: int):
        for _, child in sorted(under.get(model, [])):
            if depth < 12 and kinds.get(child) in ("node", "asst"):
                if text[child]:
                    yield "  " * depth + "- " + text[child]
                yield from lines(child, depth + 1)

    top = next((m for m, kind in kinds.items() if kind == "doc"), None)
    return "\n".join(lines(top, 0)) if top else ""


def words(element) -> str:
    """An XML element's text, its runs joined."""
    return " ".join(" ".join(t.text or "" for t in element.iterfind(".//a:t", OOXML)).split())


def word_charts(path: Path) -> list[Part]:
    """A Word file's charts, as the data Word keeps inside each one (Word's reader skips
    charts): "chart 1, sheet Sheet1, rows 2-5"."""
    found = []
    with zipfile.ZipFile(path) as word, tempfile.TemporaryDirectory() as folder:
        books = sorted(n for n in word.namelist() if re.match(r"word/embeddings/.+\.xlsx$", n))
        for number, name in enumerate(books, start=1):
            book = Path(folder) / f"{number}.xlsx"
            book.write_bytes(word.read(name))
            found += [Part(f"chart {number}, {p.location}", p.text) for p in row_parts(book)]
    return found


def save_picture(picture, stem: Path) -> Path | None:
    """A picture (bytes, or a rendered page) saved for Claude, at the size Claude reads it:
    as it came if it's already a JPEG, PNG, WebP or GIF of that size; else a PNG for a
    drawing (few colours) or a JPEG for a photo or scan. None for one Pillow can't open
    (an EMF drawing)."""
    from io import BytesIO

    from PIL import Image, UnidentifiedImageError

    try:
        image = picture if isinstance(picture, Image.Image) else Image.open(BytesIO(picture))
        width, height = image.size
        scale = min(
            1.0, CLAUDE_EDGE / max(width, height), (CLAUDE_PIXELS / (width * height)) ** 0.5
        )
        stem.parent.mkdir(parents=True, exist_ok=True)
        kind = SENT_AS_IS.get(image.format or "")
        if (
            isinstance(picture, bytes)
            and kind
            and scale == 1
            and not getattr(image, "is_animated", 0)
        ):
            (saved := stem.with_suffix(kind)).write_bytes(picture)
            return saved
        image = image.convert("RGB")
        if scale < 1:
            size = (max(1, int(width * scale + 1e-6)), max(1, int(height * scale + 1e-6)))
            image = image.resize(size)  # rounded down: within Claude's size, never over
        drawing = image.getcolors(maxcolors=256) is not None
        saved = stem.with_suffix(".png" if drawing else ".jpg")
        image.save(saved, **({} if drawing else {"quality": 90}))
        return saved
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def blank(image: Path) -> bool:
    """Nearly one colour all over: a blank page or a background, nothing to read."""
    from PIL import Image, ImageStat

    with Image.open(image) as opened:
        return ImageStat.Stat(opened.convert("L")).stddev[0] < BLANK_SPREAD


def zip_name(member: zipfile.ZipInfo) -> str:
    """A zip member's name. One not marked UTF-8 is read as UTF-8 if it is (a Mac writes
    them so), else as the old DOS encoding the format says."""
    return zip_name_of(member.filename, member.flag_bits & 0x800)


def zip_name_of(name: str, utf8: bool = False) -> str:
    if utf8:
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def junk(name: str) -> bool:
    """What zips carry along: Mac and Windows clutter, hidden and lock files."""
    parts = name.lower().replace("\\", "/").split("/")
    return any(
        p in ("__macosx", "thumbs.db", "desktop.ini") or (p.startswith((".", "~$")) and p != "..")
        for p in parts
    )


def page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def pdf_pages(path: Path, start: int, count: int, folder: Path) -> list[tuple[str, Path | None]]:
    """(text, None) for each page with text; ("", image) for a page that needs reading
    as an image (a scan); (text, image) for a page of text with a large picture or drawing
    on it, a quarter of the page or more (a chart or a diagram, not a logo or a rule).
    Pages are drawn at the size Claude reads them."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    pages = []
    try:
        for i in range(start, min(start + count, len(pdf))):
            page = pdf[i]
            textpage = page.get_textpage()
            text = textpage.get_text_bounded().replace("\r\n", "\n").strip()
            textpage.close()
            width, height = page.get_size()
            pictured = any(
                (right - left) * (top - bottom) >= width * height / 4
                for left, bottom, right, top in (
                    o.get_bounds()
                    for o in page.get_objects(filter=[3])  # 3: images
                )
            )
            text = text if len(text) >= SCANNED_BELOW else ""
            image = None
            if (
                pictured
                or not text
                or (len(text) < DRAWN_TEXT_BELOW and drawn(page, width, height))
            ):
                scale = min(200 / 72, (CLAUDE_PIXELS / (width * height)) ** 0.5)
                image = save_picture(page.render(scale=scale).to_pil(), folder / f"page-{i + 1}")
            pages.append((text, image))
            page.close()
    finally:
        pdf.close()
    return pages


def drawn(page, width: float, height: float) -> bool:
    """A diagram drawn with lines and shapes (no picture in it): enough drawn pieces,
    spread over a quarter of the page or more."""
    pieces = [
        bounds
        for bounds in (o.get_bounds() for o in page.get_objects(filter=[2]))  # 2: drawn
        if (bounds[2] - bounds[0]) * (bounds[3] - bounds[1]) >= width * height / 1000
        or bounds[2] - bounds[0] >= width / 10  # a line or an arrow: thin but long
        or bounds[3] - bounds[1] >= height / 10
    ]
    if len(pieces) < DRAWN_AT_LEAST:
        return False
    left, bottom = min(b[0] for b in pieces), min(b[1] for b in pieces)
    right, top = max(b[2] for b in pieces), max(b[3] for b in pieces)
    return (right - left) * (top - bottom) >= width * height / 4


def to_markdown(path: Path) -> str:
    from markitdown import MarkItDown

    return MarkItDown(enable_plugins=False).convert(str(path)).text_content


SLIDE = re.compile(r"^<!-- Slide number: (\d+) -->")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def sections(markdown: str, suffix: str) -> list[Part]:
    """Markdown cut at its slides, sheets or headings."""
    found: list[Part] = []
    location, lines = "start", []

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            found.extend(split_long(location, text))

    for line in markdown.splitlines():
        slide = SLIDE.match(line)
        heading = HEADING.match(line) if suffix != ".pptx" else None
        if slide or heading:
            flush()
            if slide:
                location = f"slide {slide.group(1)}"
            elif suffix in SPREADSHEETS:
                location = f"sheet {heading.group(1)}"
            else:
                location = f"§ {heading.group(1)}"
            lines = [] if slide else [line]
        else:
            lines.append(line)
    flush()
    return unique(found)


def split_long(location: str, text: str) -> list[Part]:
    if len(text) <= PART_AT_MOST:
        return [Part(location, text)]
    pieces = [p.strip() for p in windows(text, PART_AT_MOST, 0)]
    return [Part(f"{location} (part {i + 1})", piece) for i, piece in enumerate(pieces)]


def unique(parts: list[Part]) -> list[Part]:
    """Two sections with the same heading get "(2)", so every location names one place."""
    seen: dict[str, int] = {}
    for part in parts:
        seen[part.location] = seen.get(part.location, 0) + 1
        if seen[part.location] > 1:
            part.location = f"{part.location} ({seen[part.location]})"
    return parts


def chunks(text: str) -> list[str]:
    return windows(text, CHUNK, OVERLAP)


def windows(text: str, size: int, overlap: int) -> list[str]:
    """Pieces of about `size` characters, cut at a paragraph or a space."""
    pieces, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(" ", start, end))
            end = cut if cut > start + size // 2 else end
        pieces.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [p for p in pieces if p.strip()]


def pick(parts: list[dict], location: str) -> list[dict]:
    """The parts a location names: "page 12", "12", "pages 12-14", or a location's name."""
    wanted = location.strip().lower()
    numbers = re.fullmatch(r"(?:pages?\s*)?(\d+)(?:\s*(?:-|–|to)\s*(\d+))?", wanted)
    if numbers:
        low = int(numbers.group(1))
        high = int(numbers.group(2) or low)
        paged = [p for p in parts if re.fullmatch(r"page \d+", p["location"])]
        return [p for p in paged if low <= int(p["location"].split()[1]) <= high]
    exact = [p for p in parts if p["location"].lower() == wanted]
    if exact:
        return exact
    plain = wanted.lstrip("§ ").strip()
    return [p for p in parts if plain and plain in p["location"].lower()][:20]


def normal(text: str) -> str:
    """Lower case, curly quotes and dashes made plain, everything but letters and digits
    a single space: so a quote matches however the file spaced or styled it."""
    text = text.lower().translate(str.maketrans("‘’“”–—", "''\"\"--"))
    return re.sub(r"[^\w]+", " ", text).strip()


def loosely_in(quote: str, text: str) -> bool:
    """For scanned pages: most of the quote lines up somewhere in the text."""
    if not quote:
        return False
    step = max(len(quote) // 4, 1)
    for start in range(0, max(len(text) - len(quote), 0) + 1, step):
        window = text[start : start + len(quote) + step]
        matched = sum(
            b.size
            for b in difflib.SequenceMatcher(
                None, quote, window, autojunk=False
            ).get_matching_blocks()
        )
        if matched / len(quote) >= LOOSE_MATCH:
            return True
    return False


def answer_page(answer: str, citations: list[dict], library: Library) -> str:
    """The full answer for the apps page: each quote, linked to the file at its page."""
    from html import escape

    items = []
    for c in sorted(citations, key=lambda c: bool(c.get("url"))):  # the user's files first
        if url := c.get("url"):
            items.append(
                f"<li><blockquote>{escape(str(c.get('quote', '')))}</blockquote><a "
                f"href='{escape(url)}'>{escape(short(url, 80))}</a> (from the web)</li>"
            )
            continue
        doc = library.find(str(c.get("document", "")))
        location = str(c.get("location", ""))
        page = re.search(r"\d+", location) if doc and doc.suffix in PDF else None
        href = f"/library/files/{doc.id}" + (f"#page={page.group()}" if page else "") if doc else ""
        scanned = " (scanned copy)" if library.scanned_at(c.get("document", ""), location) else ""
        source = (
            f"<a href='{escape(href)}'>{escape(doc.name if doc else '?')}, {escape(location)}</a>"
        )
        items.append(
            f"<li><blockquote>{escape(str(c.get('quote', '')))}</blockquote>{source}{scanned}</li>"
        )
    when = time.strftime("%d %b %Y, %H:%M")
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{escape(short(answer))}</title><link rel=stylesheet href='/static/alfred.css'>"
        "<link rel=icon type='image/svg+xml' href='/static/alfred-logo.svg'>"
        "<header class=bar><a class=mark href='/'><img src='/static/alfred-logo.svg' alt=''>"
        "Alfred</a><nav><a href='/'>What he built</a><a href='/library'>Library</a></nav>"
        "</header><main class=answer>"
        f"<h1>Answer</h1><p class=lead>From your files, {when}. Every quote below was "
        "checked against the file before Alfred said it.</p>"
        f"<div class='sheet said'>{escape(answer).replace(chr(10), '<br>')}</div>"
        f"<h2>Sources</h2><ol class=sources>{''.join(items)}</ol>"
        "<p class=signature>At your service.</p></main>"
    )


def short(text: str, at_most: int = 70) -> str:
    """The start of a text, cut at a word, for a title."""
    text = " ".join(text.split())
    return text if len(text) <= at_most else text[:at_most].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def claude_describer(model: str) -> Describer:
    """Claude writes one line on what each file is (through the Agent SDK, on your plan)."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    instructions = (
        "For each file, write one line of at most 20 words: what it is, who or what it "
        'concerns, and its date if shown ("Lease for Unit 4B, Makati, signed March 2024"). '
        'Reply with only a JSON object: {"1": "...", "2": "..."}, one per file, in order.'
    )

    async def describe(files: list[tuple[str, str]]) -> list[str]:
        listing = "\n\n".join(
            f"=== {n} === {name}\n{opening}" for n, (name, opening) in enumerate(files, 1)
        )
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=instructions,
            tools=[],
            max_turns=1,
            setting_sources=[],
            cwd=tempfile.gettempdir(),
        )
        answer = ""
        async for message in query(prompt=listing, options=options):
            if isinstance(message, ResultMessage):
                answer = message.result or ""
        lines = json.loads(answer[answer.find("{") : answer.rfind("}") + 1] or "{}")
        return [str(lines.get(str(n), "")) for n in range(1, len(files) + 1)]

    return describe


def claude_vision(model: str) -> Vision:
    """Claude reads pictures (through the Agent SDK, so on your Claude plan). They go in
    the message itself, so Claude reads them all in one turn."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    instructions = (
        "You transcribe document page images exactly. Each image follows a line "
        "'=== <number> ==='. For each, in order, write that line and then the page's text "
        "word for word: no corrections, no summaries, no commentary. Keep tables as Markdown "
        "tables. Describe a chart or photo fully in [brackets]: its title, every label, value, "
        "axis and legend you can read. For a diagram (a flowchart, org chart or process), give "
        "every box and how they connect, as a nested list or as arrows (A → B). Write "
        "[unclear] for anything you cannot read."
    )

    async def see(images: list[Path]) -> list[str]:
        content: list[dict] = [{"type": "text", "text": "Transcribe these pages."}]
        for number, image in enumerate(images, start=1):
            data = base64.b64encode(image.read_bytes()).decode()
            source = {"type": "base64", "media_type": MEDIA_TYPES[image.suffix], "data": data}
            content += [
                {"type": "text", "text": f"=== {number} ==="},
                {"type": "image", "source": source},
            ]

        async def message():
            yield {"type": "user", "message": {"role": "user", "content": content}}

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=instructions,
            tools=[],
            max_turns=1,
            setting_sources=[],
            cwd=tempfile.gettempdir(),
        )
        answer = ""
        async for result in query(prompt=message(), options=options):
            if isinstance(result, ResultMessage):
                answer = result.result or ""
        texts = {}
        for block in re.split(r"^=== ", answer, flags=re.MULTILINE)[1:]:
            number, _, text = block.partition("\n")
            texts[number.strip(" =")] = text.strip()
        return [texts.get(str(n), "[unclear]") for n in range(1, len(images) + 1)]

    return see
