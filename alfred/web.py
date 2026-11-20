"""The apps page: what Alfred built, on your network, at
http://<this computer>.local:<ALFRED_WEB_PORT>/ while he runs. Off unless the port is set.

    /                    every project with something to open, and the running apps
    /<project>/          its static site: the first of dist/, build/, out/, public/ or the
                         project folder that has an index.html (no backend needed)
    /pages/<project>/    pages the agent made for you (reports, diffs), in .alfred/pages/
    /library             the library: upload files to ask about, see their status, delete

Nothing else is shared: no hidden files (a project's .env stays private) and nothing a
link points to outside those folders. Anyone on the same network can open the pages.
"""

import html
import json
import re
import socket
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from loguru import logger

from alfred.apps import Apps
from alfred.library import KEEP_FREE_GB, WHOLE_FILE_MB, Library, Refused
from alfred.projects import Projects

SITE_FOLDERS = ("dist", "build", "out", "public", ".")
PAGES = Path(".alfred") / "pages"


def site_folder(project: Path) -> Path | None:
    for name in SITE_FOLDERS:
        if (project / name / "index.html").is_file():
            return (project / name).resolve()
    return None


class AppsPage(SimpleHTTPRequestHandler):
    projects: Projects
    apps: Apps
    library: Library | None = None

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            return self._send_html(index_page(self.projects, self.apps, self.library))
        if path.rstrip("/") == "/library" and self.library:
            return self._send_html(library_page(self.library))
        if path == "/library.json" and self.library:
            return self._send_json(200, library_json(self.library))
        super().do_GET()

    def do_POST(self) -> None:
        """The library's actions: a piece of an upload, delete, and yes to a large scan."""
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        size = int(self.headers.get("Content-Length") or 0)
        if size > CHUNK_AT_MOST:
            self._discard(size)
            return self._send_json(413, {"error": "Send uploads in pieces of 16 MB or less."})
        body = self.rfile.read(size) if size else b""
        if not self.library:
            return self.send_error(404)
        if self.headers.get("X-Alfred") != "1":  # only the library page sends it, not other sites
            return self._send_json(403, {"error": "Use the library page."})
        if url.path == "/library/delete":
            deleted = self.library.delete(query.get("id", ""))
            return self._send_json(200 if deleted else 404, {"deleted": deleted})
        if url.path == "/library/approve":
            doc = self.library.approve(query.get("id", ""))
            return self._send_json(200 if doc else 404, {"reading": bool(doc)})
        if url.path == "/library/clear":
            return self._send_json(200, {"cleared": self.library.clear()})
        if url.path != "/library/upload":
            return self.send_error(404)
        try:
            total, offset = int(query.get("size", -1)), int(query.get("offset", -1))
            name = query.get("name", "untitled")
            have = self.library.receive(query.get("id", ""), offset, body, total, name)
            if have < total:
                return self._send_json(200, {"have": have})
            doc, what = self.library.finish(query["id"], name, query.get("replace") == "1")
        except (Refused, ValueError) as error:
            return self._send_json(413, {"error": str(error)})
        status = 409 if what == "exists" else 200
        return self._send_json(status, {"have": have, "done": what, "document": doc and doc.name})

    def _discard(self, size: int) -> None:
        while size > 0:  # read it away first, so the browser hears the answer
            size -= len(self.rfile.read(min(size, 1 << 20))) or size

    def _send_json(self, status: int, value) -> None:
        self._send(status, json.dumps(value).encode(), "application/json")

    def translate_path(self, path: str) -> str:
        missing = str(self.projects.root / ".not-shared")  # doesn't exist: "404 not found"
        parts = [p for p in unquote(urlsplit(path).path).split("/") if p]
        if len(parts) == 2 and parts[0] == "static" and (STATIC / parts[1]).is_file():
            return str(STATIC / parts[1])  # the pages' own stylesheet and script
        if parts[:2] == ["library", "files"] and len(parts) == 3 and self.library:
            doc = self.library.get(parts[2])  # an uploaded file, for "#page=12" links
            return doc.path if doc else missing
        pages = parts[:1] == ["pages"]
        if pages:
            parts = parts[1:]
        if not parts or parts[0] not in self.projects.names():
            return missing
        project = self.projects.root / parts[0]
        root = (project / PAGES).resolve() if pages else site_folder(project)
        if root is None or any(p.startswith(".") for p in parts[1:]):
            return missing
        real = root.joinpath(*parts[1:]).resolve()  # follows links to where they lead
        return str(real) if real == root or root in real.parents else missing

    def _send_html(self, page: str) -> None:
        self._send(200, page.encode(), "text/html; charset=utf-8")

    def _send_text(self, status: int, text: str) -> None:
        self._send(status, text.encode(), "text/plain; charset=utf-8")

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        logger.debug(f"Web: {self.address_string()} {format % args}")


CHUNK_AT_MOST = 16 * 1024 * 1024  # bytes in one upload piece (the page sends 8 MB)
STATIC = Path(__file__).parent / "static"  # the pages' stylesheet, script and logo
HEAD = (  # every page: the stylesheet, and his bow tie as the tab's icon
    "<link rel=stylesheet href='/static/alfred.css'>"
    "<link rel=icon type='image/svg+xml' href='/static/alfred-logo.svg'>"
)
MARK = "<a class=mark href='/'><img src='/static/alfred-logo.svg' alt=''>Alfred</a>"


def layout(title: str, here: str, body: str, script: str = "") -> str:
    """Every page: the household bar (Alfred, what he built, the library) and the body."""
    links = [("/", "What he built"), ("/library", "Library")]
    nav = "".join(
        f"<a href='{href}'{' aria-current=page' if href == here else ''}>{label}</a>"
        for href, label in links
    )
    tail = f"<script src='/static/{script}' defer></script>" if script else ""
    return (
        "<!doctype html><html lang=en><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title>{HEAD}"
        f"<header class=bar>{MARK}<nav>{nav}</nav></header>"
        f"<main>{body}<p class=signature>At your service.</p></main>{tail}"
    )


def rows(items: list[str], empty: str) -> str:
    inner = "".join(items) if items else f"<li class=empty>{html.escape(empty)}</li>"
    return f"<ul class='rows sheet'>{inner}</ul>"


def row(name: str, href: str, meta: str, status: str = "", status_class: str = "") -> str:
    side = f"<div class=side><span class='status {status_class}'>{html.escape(status)}</span></div>"
    return (
        f"<li><div class=name><a href='{html.escape(href)}'>{html.escape(name)}</a></div>"
        f"<div class=meta>{html.escape(meta)}</div>{side if status else ''}</li>"
    )


def page_title(page: Path) -> str:
    """A page's own <title>, else its file name in words."""
    found = re.search(r"<title>(.*?)</title>", page.read_text(errors="replace")[:4000], re.S)
    return html.unescape(found.group(1)).strip() if found else page.stem.replace("-", " ")


NO_APPS = "No apps running. Ask him to build one and start it."
NO_PAGES = "No pages yet: reports and answers appear here."


def index_page(projects: Projects, apps: Apps, library: Library | None) -> str:
    running = [
        row(a["project"], a["address"], a["address"], a["status"].split(" ")[0].capitalize(),
            "running" if a["status"] == "running" else "stopped")
        for a in apps.list()
    ]  # fmt: skip
    sites, pages = [], []
    for name in projects.names():
        if site_folder(projects.root / name):
            sites.append(row(name, f"/{name}/", f"Static site, from the {name} project"))
        for page in sorted((projects.root / name / PAGES).glob("*.html"), reverse=True):
            when = time.strftime("%d %b, %H:%M", time.localtime(page.stat().st_mtime))
            pages.append(row(page_title(page), f"/pages/{name}/{page.name}", f"{name}, {when}"))
    shelf = ""
    if library is not None:
        docs = library.documents()
        ready = sum(d.status == "ready" for d in docs)
        count = f"{len(docs)} file{'s' * (len(docs) != 1)}, {ready} ready" if docs else "empty"
        shelf = f"<h2>The library</h2>{rows([row('Files to ask about', '/library', count)], '')}"
    body = (
        "<h1>What Alfred built</h1><p class=lead>The apps Alfred is running, the sites and "
        "pages he has made for you, and the files you have given him.</p>"
        f"<h2>Running now</h2>{rows(running, NO_APPS)}"
        f"<h2>Sites</h2>{rows(sites, 'No sites yet.')}"
        f"<h2>Pages he made for you</h2>{rows(pages[:30], NO_PAGES)}"
        f"{shelf}"
    )
    return layout("Alfred", "/", body)


def library_page(library: Library) -> str:
    limit = library.max_bytes >> 20
    body = (
        "<h1>The library</h1><p class=lead>Files Alfred answers questions from. Ask him "
        "anything they cover; he says where the answer is, and every quote is checked "
        "against the file.</p>"
        f"<button class=tray id=tray type=button data-limit='{library.max_bytes}'>"
        "<img src='/static/alfred-logo.svg' alt=''>"
        "<strong>Hand Alfred a file</strong><span>Drop files, a folder or a zip here, or "
        "click to choose files.</span><span class=formats>PDF, Word, PowerPoint, Excel, CSV, "
        f"EPUB, web pages, text and images. Up to {limit} MB each; {WHOLE_FILE_MB} MB for Word, "
        "PowerPoint, EPUB and web pages.</span></button>"
        "<input id=picker type=file multiple hidden>"
        "<input id=folder type=file webkitdirectory hidden>"
        "<p class=hint><button class=link id=pick-folder type=button>Choose a folder</button>. "
        "Scanned pages and photos are read by Claude, so they take longer; a large scan "
        "waits for your yes here.</p>"
        "<p class=space id=space></p>"
        "<ul class='rows' id=uploads></ul>"
        "<h2>Files</h2><ul class='rows sheet' id=files><li class=empty>Loading…</li></ul>"
        "<p class=hint><button class='link' id=clear type=button hidden>Clear the library"
        "</button></p>"
    )
    return layout("Alfred's library", "/library", body, "library.js")


def library_json(library: Library) -> dict:
    found, docs = [], library.documents()
    waiting = sorted(
        (d for d in docs if d.status == "waiting"), key=lambda d: (d.bytes, d.created_at)
    )
    places = {d.id: n for n, d in enumerate(waiting, start=1)}  # 1: read next
    for doc in docs:
        unit = ("page" if doc.suffix == ".pdf" else "part") + ("s" if doc.total != 1 else "")
        found.append(
            doc.describe(with_file=False)
            | {
                "done": doc.done,
                "total": doc.total,
                "kind": doc.suffix.lstrip(".").upper() or "FILE",
                "bytes": doc.bytes,
                "parts": f"{doc.total} {unit}" if doc.status == "ready" and doc.total else "",
                "added": time.strftime("%d %b", time.localtime(doc.created_at)),
                "queue": places.get(doc.id),
            }
        )
    return {"documents": found, "space": library.space(), "keep_free": KEEP_FREE_GB << 30}


class WebShare:
    def __init__(self, projects: Projects, apps: Apps, port: int, library: Library | None = None):
        handler = type(
            "Handler", (AppsPage,), {"projects": projects, "apps": apps, "library": library}
        )
        handler = partial(handler, directory=str(projects.root))
        self._server = ThreadingHTTPServer(("0.0.0.0", port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="web", daemon=True)
        self.address = address(self.port)
        self.ip_address = f"http://{lan_ip()}:{self.port}/"

    def start(self) -> None:
        self._thread.start()
        logger.info(f"The apps page is at {self.address} (also {self.ip_address})")

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def address_text(self) -> str:
        """For Alfred's instructions, so "where can I see it?" gets a real answer (the
        agent can't tell: its sandbox has its own network)."""
        return (
            f"\n\n## The apps page\nWhat Alfred built is listed at {self.address} (or "
            f"{self.ip_address}) on the user's network: static sites at <address><project>/, "
            "pages at <address>pages/<project>/<file>, the user's files to ask about at "
            "<address>library."
        )

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def start_web(
    projects: Projects, apps: Apps, port: int | None, library: Library | None = None
) -> WebShare | None:
    """Start the apps page if a port is set. Never stops Alfred from starting."""
    if port is None:
        return None
    try:
        web = WebShare(projects, apps, port, library)
    except OSError as error:  # the port is already in use
        logger.warning(f"Couldn't start the apps page on port {port}: {error}")
        return None
    web.start()
    return web


def address(port: int) -> str:
    """The name other devices on the network can use, e.g. http://raspy.local:8080/."""
    name = socket.gethostname()
    if not name.endswith(".local"):
        name += ".local"
    return f"http://{name}:{port}/"


def lan_ip() -> str:
    """This computer's address on the home network (no data is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.168.0.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
