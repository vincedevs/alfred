"""The library: files you upload (on the apps page), read once in the background, so the
agent can answer questions about them with citations that are checked before he speaks.

Each file becomes text with locations a citation can point to: `page 12`, `slide 4`,
`sheet Sales, rows 1-500`, `§ 14. Termination`, `lines 1-80`. PDF text comes straight from
the file (pypdfium2), a page at a time; scanned pages and images are transcribed by Claude
vision (marked scanned), after a yes for a large scan and within a daily page limit;
spreadsheets are read row by row; Word, PowerPoint, EPUB and HTML go through MarkItDown.

The text is cut into chunks labelled with their document and location, and found two ways
(hybrid search): by keyword (SQLite FTS5, with stemming: "terminate" finds "termination")
and by meaning (OpenAI embeddings of each chunk with its label, in sqlite-vec). The two
rankings are merged; without embeddings (offline), keyword search carries on alone.
Pages are searchable as soon as they are done; small files are read before large ones.

Everything lives in ~/.alfred/library: the files, and library.db.
"""

import asyncio
import csv
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from alfred.memory import words_in

PDF = {".pdf"}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MARKDOWN = {".docx", ".pptx", ".xls", ".html", ".htm", ".epub", ".msg"}
ROWS = {".xlsx", ".xlsm", ".csv"}  # read row by row: any size
SPREADSHEETS = ROWS | {".xls"}
WHOLE_FILE_MB = 100  # MarkItDown reads a file whole: Word, PowerPoint, EPUB, HTML, .xls
KEEP_FREE_GB = 10  # the card always keeps this much free
PAGES_AT_ONCE = 8  # PDF pages read per step; each step is searchable when done
ROWS_PER_PART = 500
VISION_PAGES = 4  # page images per Claude call
VISION_AT_ONCE = 3  # Claude calls in flight
TOKENS_PER_SCAN = 2000  # roughly, for a page image and its text
SCANNED_BELOW = 30  # characters: a page with less text than this is read as an image
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
    approved    INTEGER NOT NULL DEFAULT 0  -- yes to reading a large scan
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
# Embedder: texts in, one embedding (DIMENSIONS numbers) per text out.
Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class Refused(ValueError):
    """An upload the library won't take: too large, or no room. The message says why."""


@dataclass
class Limits:
    max_mb: float = 500  # per file
    max_gb: float = 64  # the whole library
    ask_pages: int = 200  # scanned pages in one file before asking first
    daily_pages: int = 1000  # scanned pages read by vision per day


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

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix.lower()

    def describe(self) -> dict:
        progress = f"{self.done} of {self.total}" if self.status in ("processing", "paused") else ""
        return (
            {"document": self.name, "id": self.id, "status": self.status}
            | ({"progress": progress} if progress else {})
            | ({"note": self.note} if self.note else {})
            | ({"file": self.path} if self.suffix in SPREADSHEETS else {})  # to calculate from
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
    ):
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
        self._queue: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue()
        self._queued: set[str] = set()
        self._order = 0
        self._worker: asyncio.Task | None = None
        self.on_ready: list[Callable[[Document], None]] = []
        self.on_asking: list[Callable[[Document], None]] = []  # a large scan needs a yes

    @property
    def max_bytes(self) -> int:
        return int(self.limits.max_mb * 1024 * 1024)

    def _add_missing_columns(self) -> None:
        """A library.db made by 1.1.0's first build lacks the newer columns."""
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(documents)")}
        for column in ("bytes", "approved"):
            if column not in have:
                self._conn.execute(
                    f"ALTER TABLE documents ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
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

    def documents(self) -> list[Document]:
        return [Document(**dict(r)) for r in self._sql("SELECT * FROM documents ORDER BY name")]

    def get(self, doc_id: str) -> Document | None:
        rows = self._sql("SELECT * FROM documents WHERE id = ?", (doc_id,))
        return Document(**dict(rows[0])) if rows else None

    def find(self, ref: str) -> Document | None:
        """A document by its id or its name, or a word of its name ("the lease")."""
        ref = ref.strip().lower()
        docs = self.documents()
        for test in (
            lambda d: d.id == ref or d.name.lower() == ref,
            lambda d: Path(d.name).stem.lower() == ref,
            lambda d: ref and ref in d.name.lower(),
            lambda d: set(words_in(ref)) & set(words_in(Path(d.name).stem)),
        ):
            if found := [d for d in docs if test(d)]:
                return found[0]
        return None

    def contents_text(self) -> str:
        """For every prompt: which files the library holds, so a question they may answer
        goes to them first, not to the web. Past LISTED_IN_PROMPTS files, a summary."""
        docs = self.documents()
        if not docs:
            return ""
        intro = (
            "\n\n## The user's files (the library)\nQuestions these may answer, even when "
            "the user doesn't mention a file, are answered from them first.\n"
        )
        if len(docs) <= LISTED_IN_PROMPTS:
            return intro + "\n".join(f"- {doc.name}{brief(doc)}" for doc in docs)
        kinds: dict[str, int] = {}
        for doc in docs:
            kind = doc.suffix.lstrip(".").upper() or "other"
            kinds[kind] = kinds.get(kind, 0) + 1
        counts = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items(), key=lambda k: -k[1]))
        newest = sorted(docs, key=lambda d: d.created_at, reverse=True)[:10]
        return (
            intro
            + f"{len(docs)} files ({counts}). Newest:\n"
            + "\n".join(f"- {doc.name}{brief(doc)}" for doc in newest)
            + "\nFind the rest with list_documents and search_documents."
        )

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
        if size > cap * 1024 * 1024:
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

    def add_file(self, name: str, path: Path, replace: bool = False) -> tuple[Document, str]:
        """Keep a received file and queue it. Returns the document and what happened:
        added, already there (same contents), replaced, or exists (same name, different
        contents, and `replace` not given: nothing is done)."""
        name = Path(name).name.strip() or "untitled"
        digest = hashlib.sha256()
        with path.open("rb") as data:
            while block := data.read(1 << 20):
                digest.update(block)
        doc_id = digest.hexdigest()[:16]
        if existing := self.get(doc_id):
            path.unlink(missing_ok=True)
            return existing, "already there"
        same_name = [d for d in self.documents() if d.name.lower() == name.lower()]
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
        logger.info(f"Library: {name} {'replaced' if same_name else 'added'}")
        return self.get(doc_id), "replaced" if same_name else "added"

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
        self._worker = asyncio.create_task(self._work(), name="library")
        for doc in self.documents():
            if doc.status in ("waiting", "processing", "paused"):
                self._enqueue(doc.id)
        for stale in self.incoming.glob("*.part"):  # uploads abandoned over a day ago
            if time.time() - stale.stat().st_mtime > 86400:
                stale.unlink(missing_ok=True)

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    def _enqueue(self, doc_id: str) -> None:
        if not self._loop or doc_id in self._queued:
            return
        doc = self.get(doc_id)
        self._queued.add(doc_id)
        self._order += 1
        item = (doc.bytes if doc else 0, self._order, doc_id)  # smallest first
        self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    async def _work(self) -> None:
        while True:
            try:
                _, _, doc_id = await asyncio.wait_for(self._queue.get(), RECHECK_SECONDS)
            except TimeoutError:
                await self._idle()
                continue
            self._queued.discard(doc_id)
            doc = self.get(doc_id)
            if not doc or doc.status in ("ready", "asking", "failed"):
                continue
            started = time.monotonic()
            try:
                finished = await self._process(doc)
            except Exception as error:
                logger.exception(f"Library: couldn't read {doc.name}")
                self._set(doc.id, status="failed", note=f"Couldn't read it: {error}")
                continue
            if not self.get(doc.id) or not finished:
                continue  # deleted while being read, or waiting for a yes or for tomorrow
            self._set(doc.id, status="ready")
            logger.info(f"Library: {doc.name} ready in {time.monotonic() - started:.0f} s")
            for listener in list(self.on_ready):
                listener(self.get(doc.id))
            if self._queue.empty():
                await self._idle()

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
        if suffix in PDF:
            return await self._read_pdf(doc, path)
        self._clear(doc.id)
        self._set(doc.id, status="processing", done=0, note=None)
        if suffix in IMAGES:
            if self.scans_left() < 1:
                self._set(doc.id, status="paused", note=paused_note())
                return False
            self._count_scans(1)
            parts = [Part("page 1", (await self._see([path]))[0], scanned=True)]
        elif suffix in MARKDOWN:
            markdown = await asyncio.to_thread(to_markdown, path)
            parts = sections(markdown, suffix)
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
                if not pages:
                    self._set(doc.id, status="paused", note=paused_note())
                    return False
                images = [image for _, image in pages if image]
                self._count_scans(len(images))
                seen = iter(await self._see(images)) if images else iter(())
                parts = [
                    Part(f"page {start + i + 1}", next(seen) if image else text, bool(image))
                    for i, (text, image) in enumerate(pages)
                ]
                await self._store(doc, start, parts)
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
        """Transcribe page images with Claude vision, a few per call, a few calls at once."""
        if not self._vision:
            return ["[unclear: scanned page, no vision configured]"] * len(images)
        slots = asyncio.Semaphore(VISION_AT_ONCE)

        async def batch(group: list[Path]) -> list[str]:
            async with slots:
                texts = await self._vision(group)
            return (texts + ["[unclear]"] * len(group))[: len(group)]

        groups = [images[i : i + VISION_PAGES] for i in range(0, len(images), VISION_PAGES)]
        return [text for texts in await asyncio.gather(*map(batch, groups)) for text in texts]

    async def _store(self, doc: Document, first: int, parts: list[Part]) -> None:
        """Keep the parts and their chunks (searchable by keyword at once), then embed the
        chunks for meaning search."""
        new: list[tuple[int, str]] = []
        with self._lock:
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
        await self._embed(new)

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
            with self._lock:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO vectors (rowid, embedding) VALUES (?, ?)",
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
        text, shown = "", []
        for part in chosen:
            piece = (
                f"[{part['location']}]{' (scanned)' if part['scanned'] else ''}\n{part['text']}\n\n"
            )
            if text and len(text) + len(piece) > READ_AT_MOST:
                break
            text += piece[:READ_AT_MOST]
            shown.append(part["location"])
        more = chosen[len(shown) :]
        return (
            {"document": doc.name, "text": text}
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
        results = []
        for citation in citations:
            doc = self.find(str(citation.get("document", "")))
            quote = normal(str(citation.get("quote", "")))
            if not doc:
                results.append("that document isn't in the library")
                continue
            if len(quote) < 8:
                results.append("the quote is too short to check: quote a full phrase")
                continue
            parts = self._parts(doc.id)
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


def amount(tokens: int) -> str:
    return f"{tokens / 1e6:.1f} million" if tokens >= 1e6 else f"{tokens:,}"


def paused_note() -> str:
    return "Today's limit for reading scanned pages is used up: it carries on tomorrow."


def within_budget(pages: list, scans_left: int) -> tuple[list, bool]:
    """The pages that fit today's scan budget, in order, and whether it ran out."""
    kept, scans = [], 0
    for text, image in pages:
        if image and scans == scans_left:
            return kept, True
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
        with path.open(newline="", encoding="utf-8", errors="replace") as file:
            yield from table_parts("", csv.reader(file))
        return
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in book.worksheets:
            yield from table_parts(f"sheet {sheet.title}", sheet.iter_rows(values_only=True))
    finally:
        book.close()


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
    with path.open(encoding="utf-8", errors="replace") as file:
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


def page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def pdf_pages(path: Path, start: int, count: int, folder: Path) -> list[tuple[str, Path | None]]:
    """(text, None) for each page with text; ("", image) for a page that needs reading
    as an image (a scan)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    pages = []
    try:
        for i in range(start, min(start + count, len(pdf))):
            page = pdf[i]
            textpage = page.get_textpage()
            text = textpage.get_text_bounded().replace("\r\n", "\n").strip()
            textpage.close()
            if len(text) >= SCANNED_BELOW:
                pages.append((text, None))
            else:
                image = folder / f"page-{i + 1}.png"
                page.render(scale=200 / 72).to_pil().save(image)
                pages.append(("", image))
            page.close()
    finally:
        pdf.close()
    return pages


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
    for c in citations:
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


def claude_vision(model: str) -> Vision:
    """Claude reads page images (through the Agent SDK, so on your Claude plan)."""
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    instructions = (
        "You transcribe document page images exactly. For each image, in the order given, "
        "write a line '=== <file name> ===' and then the page's text word for word: no "
        "corrections, no summaries, no commentary. Keep tables as Markdown tables. Describe "
        "figures and charts briefly in [brackets]. Write [unclear] for anything you cannot "
        "read. Read every image with the Read tool first."
    )

    async def see(images: list[Path]) -> list[str]:
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=instructions,
            tools=["Read"],
            allowed_tools=["Read"],
            setting_sources=[],
            cwd=images[0].parent,
            max_turns=len(images) + 3,
        )
        files = "\n".join(str(image) for image in images)
        answer = ""
        async for message in query(prompt=f"Transcribe these pages:\n{files}", options=options):
            if isinstance(message, ResultMessage):
                answer = message.result or ""
        texts = {}
        for block in re.split(r"^=== ", answer, flags=re.MULTILINE)[1:]:
            name, _, text = block.partition("\n")
            texts[name.strip(" =")] = text.strip()
        return [texts.get(image.name, "[unclear]") for image in images]

    return see
