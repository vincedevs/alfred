"""The apps page: each project's static site and pages, the running apps, and nothing
else; only when turned on."""

import json
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
        assert (
            status == 200 and "/flappy-bird/" in index and "/pages/flappy-bird/report.html" in index
        )
        assert "/empty/" not in index  # a project with nothing to open isn't listed
        assert fetch(web, "/flappy-bird/src.py")[0] == 404  # only the site folder
        assert fetch(web, "/notes/.env")[0] == 404  # never hidden files
        assert fetch(web, "/flappy-bird/sneaky.txt")[0] == 404  # links out are refused
        assert fetch(web, "/flappy-bird/../../secret.txt")[0] == 404
        assert fetch(web, "/%2e%2e/secret.txt")[0] == 404
        assert fetch(web, "/nope/")[0] == 404
        assert f":{web.port}/" in web.address_text()
    finally:
        web.stop()


def test_the_library_page_takes_uploads_in_pieces(tmp_path):
    import asyncio

    from alfred.library import Library, Limits

    async def main():
        projects = Projects(tmp_path / "projects")
        library = Library(tmp_path / "library", None, Limits(max_mb=1))
        library.start()
        web = start_web(projects, Apps(projects, str), 0, library)
        base = f"http://127.0.0.1:{web.port}"

        def post(path, data=b""):
            request = urllib.request.Request(base + path, data=data, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=5) as reply:
                    return reply.status, json.loads(reply.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        def piece(offset, data, size=12, name="notes.txt"):
            query = f"id=page-test-01&name={name}&size={size}&offset={offset}"
            return post(f"/library/upload?{query}", data)

        try:
            assert await asyncio.to_thread(piece, 0, b"Rent: ") == (200, {"have": 6})
            assert (await asyncio.to_thread(piece, 0, b"Rent: "))[1] == {"have": 6}  # resent
            status, done = await asyncio.to_thread(piece, 6, b"30,000")
            assert status == 200 and done["done"] == "added" and done["document"] == "notes.txt"
            for _ in range(100):
                if library.documents()[0].status == "ready":
                    break
                await asyncio.sleep(0.02)
            status, page = await asyncio.to_thread(fetch, web, "/library")
            assert status == 200 and "Hand Alfred a file" in page and "Choose a folder" in page
            status, listed = await asyncio.to_thread(fetch, web, "/library.json")
            listed = json.loads(listed)
            (entry,) = listed["documents"]
            assert entry["document"] == "notes.txt" and entry["status"] == "ready"
            assert entry["kind"] == "TXT" and entry["bytes"] == 12
            assert listed["space"]["used"] == 12 and listed["space"]["limit"] == 64 << 30
            doc = library.documents()[0]
            assert (await asyncio.to_thread(fetch, web, f"/library/files/{doc.id}"))[
                1
            ] == "Rent: 30,000"
            too_big = await asyncio.to_thread(piece, 0, b"x", 2 << 20, "big.txt")
            assert too_big[0] == 413 and "limit is 1 MB" in too_big[1]["error"]
            assert (await asyncio.to_thread(post, "/library/approve?id=nothing"))[0] == 404
            assert (await asyncio.to_thread(post, f"/library/delete?id={doc.id}"))[0] == 200
            assert library.documents() == []
            for asset in ("/static/alfred.css", "/static/library.js"):
                assert (await asyncio.to_thread(fetch, web, asset))[0] == 200
            assert (await asyncio.to_thread(fetch, web, "/static/../web.py"))[0] == 404
            assert "/library" in (await asyncio.to_thread(fetch, web, "/"))[1]
        finally:
            web.stop()
            await library.stop()

    asyncio.run(main())
