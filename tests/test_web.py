"""The apps page: each project's static site and pages, the running apps, and nothing
else; only when turned on."""

import os
import urllib.error
import urllib.request

import pytest

from alfred.apps import Apps
from alfred.config import ConfigError, load_config
from alfred.projects import Projects
from alfred.web import start_web


def fetch(web, path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{web.port}{path}", timeout=5) as reply:
            return reply.status, reply.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""


def test_off_unless_a_port_is_set(tmp_path):
    projects = Projects(tmp_path)
    assert start_web(projects, Apps(projects, str), None) is None
    assert load_config({"OPENAI_API_KEY": "sk-test"}).web_port is None
    assert load_config({"OPENAI_API_KEY": "sk-test", "ALFRED_WEB_PORT": "8080"}).web_port == 8080
    with pytest.raises(ConfigError):
        load_config({"OPENAI_API_KEY": "sk-test", "ALFRED_WEB_PORT": "80"})


def test_serves_sites_and_pages_and_nothing_else(tmp_path):
    (tmp_path / "secret.txt").write_text("private notes")
    projects = Projects(tmp_path / "projects")
    game = projects.path("flappy-bird")
    (game / "dist").mkdir()
    (game / "dist" / "index.html").write_text("<h1>Flappy</h1>")
    (game / "src.py").write_text("print('source')")  # not in the site folder
    (game / ".env").write_text("KEY=1")
    os.symlink(tmp_path / "secret.txt", game / "dist" / "sneaky.txt")  # a trick
    (game / ".alfred" / "pages").mkdir(parents=True)
    (game / ".alfred" / "pages" / "report.html").write_text("<p>All green</p>")
    notes = projects.path("notes")
    (notes / "index.html").write_text("<p>Notes</p>")
    (notes / ".env").write_text("KEY=1")
    projects.path("empty")

    web = start_web(projects, Apps(projects, str), 0)  # port 0: any free port
    try:
        assert fetch(web, "/flappy-bird/") == (200, "<h1>Flappy</h1>")
        assert fetch(web, "/pages/flappy-bird/report.html") == (200, "<p>All green</p>")
        assert fetch(web, "/notes/") == (200, "<p>Notes</p>")  # a site in the project itself
        status, index = fetch(web, "/")
        assert status == 200 and "flappy-bird (site)" in index and "report" in index
        assert "empty" not in index  # nothing to open
        assert fetch(web, "/flappy-bird/src.py")[0] == 404  # only the site folder
        assert fetch(web, "/notes/.env")[0] == 404  # never hidden files
        assert fetch(web, "/flappy-bird/sneaky.txt")[0] == 404  # links out are refused
        assert fetch(web, "/flappy-bird/../../secret.txt")[0] == 404
        assert fetch(web, "/%2e%2e/secret.txt")[0] == 404
        assert fetch(web, "/nope/")[0] == 404
        assert f":{web.port}/" in web.address_text()
    finally:
        web.stop()
