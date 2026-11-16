"""The apps page: what Alfred built, on your network, at
http://<this computer>.local:<ALFRED_WEB_PORT>/ while he runs. Off unless the port is set.

    /                    every project with something to open, and the running apps
    /<project>/          its static site: the first of dist/, build/, out/, public/ or the
                         project folder that has an index.html (no backend needed)
    /pages/<project>/    pages the agent made for you (reports, diffs), in .alfred/pages/

Nothing else is shared: no hidden files (a project's .env stays private) and nothing a
link points to outside those folders. Anyone on the same network can open the pages.
"""

import html
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from loguru import logger

from alfred.apps import Apps
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

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/":
            return self._send_index()
        super().do_GET()

    def translate_path(self, path: str) -> str:
        missing = str(self.projects.root / ".not-shared")  # doesn't exist: "404 not found"
        parts = [p for p in unquote(urlsplit(path).path).split("/") if p]
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

    def _send_index(self) -> None:
        rows = []
        for app in self.apps.list():
            rows.append(link(app["address"], f"{app['project']} (app, {app['status']})"))
        for name in self.projects.names():
            if site_folder(self.projects.root / name):
                rows.append(link(f"/{name}/", f"{name} (site)"))
            for page in sorted((self.projects.root / name / PAGES).glob("*.html")):
                rows.append(link(f"/pages/{name}/{page.name}", f"{name}: {page.stem}"))
        body = "\n".join(rows) or "<li>Nothing yet: ask Alfred to build something.</li>"
        page = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-"
            "width'><title>Alfred</title><body style='font:18px system-ui;margin:2em'>"
            f"<h1>What Alfred built</h1><ul>{body}</ul>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, format: str, *args) -> None:
        logger.debug(f"Web: {self.address_string()} {format % args}")


def link(href: str, text: str) -> str:
    return f"<li><a href='{html.escape(href)}'>{html.escape(text)}</a></li>"


class WebShare:
    def __init__(self, projects: Projects, apps: Apps, port: int):
        handler = type("Handler", (AppsPage,), {"projects": projects, "apps": apps})
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
            "pages at <address>pages/<project>/<file>."
        )

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def start_web(projects: Projects, apps: Apps, port: int | None) -> WebShare | None:
    """Start the apps page if a port is set. Never stops Alfred from starting."""
    if port is None:
        return None
    try:
        web = WebShare(projects, apps, port)
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
