"""Validated answers: web facts checked like file facts, "not in your files" said honestly,
no unchecked answer from the files, quick answers from the delegate, and a web page on the
user's own network never fetched."""

import asyncio
import json
import socket
from zoneinfo import ZoneInfo

import pytest
from test_conversation import FakeLLM, Params, services
from test_library import LEASE, pdf_bytes, ready, run  # noqa: F401 (run is a fixture)

from alfred import webcheck
from alfred.agent import checked_answers, library_tools
from alfred.announcer import Announcer
from alfred.reminders import Reminders
from alfred.tools import delegate_tools

PAGE = "The central bank caps late-payment penalties at 3% a month."


def tools_for(library, tmp_path, monkeypatch, answer=None):
    async def fetch(url):
        if "down" in url:
            raise ValueError("HTTP 403")
        return PAGE

    monkeypatch.setattr(webcheck, "fetch_text", fetch)
    pages = tmp_path / "pages"
    tools = {t.name: t.handler for t in library_tools(library, "home", pages, answer)}

    async def call(name, **args):
        text = (await tools[name](args))["content"][0]["text"]
        return json.loads(text) if text[:1] in "[{" else text

    return call, pages


def test_a_web_fact_is_checked_and_marked(run, tmp_path, monkeypatch):  # noqa: F811
    async def body(library):
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        call, pages = tools_for(library, tmp_path, monkeypatch)
        assert PAGE in await call("read_web_page", url="https://bank.example/caps")
        lease = {"document": "Lease", "location": "page 2", "quote": "sixty (60) days' written"}
        web = {"url": "https://bank.example/caps", "quote": "caps late-payment penalties at 3%"}
        for _ in range(2):
            accepted = await call("cite_answer", answer="60 days; 3% cap.", citations=[lease, web])
            assert accepted["checked"] and "from the web" in accepted["say"]
        answer_pages = list(pages.glob("answer-*.html"))
        assert len(answer_pages) == 2  # two answers never share a page
        page = answer_pages[0].read_text()
        assert "(from the web)" in page and page.index("Lease.pdf") < page.index("bank.example")
        wrong = await call("cite_answer", answer="5%.", citations=[web | {"quote": "caps at 5%"}])
        assert wrong["fix"][0]["problem"] == "the quote isn't on that page"
        down = await call("cite_answer", answer="3%.", citations=[web | {"url": "https://down"}])
        assert "couldn't open that page (HTTP 403)" in down["fix"][0]["problem"]

    run(body)


def test_not_in_the_files_is_said_honestly(run, tmp_path, monkeypatch):  # noqa: F811
    async def body(library):
        doc = await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        answer = {"read_files": False, "checked": False, "held": 0}
        call, _ = tools_for(library, tmp_path, monkeypatch, answer)
        await call("search_documents", query="parking fees", document="")
        said = await call("cite_answer", answer="Not in the lease.", found=False)
        assert answer["read_files"] and answer["checked"] and "isn't in their files" in said["say"]
        library._set(doc.id, status="asking")
        said = (await call("cite_answer", answer="-", found=False))["say"]
        assert "waiting for the user's yes on the library page" in said
        assert (await call("cite_answer", answer="Sixty days."))["checked"] is False

    run(body)


def test_an_unchecked_answer_from_the_files_is_held_back_once():
    async def main():
        answer = {"read_files": False, "checked": False, "held": 0}
        stop = checked_answers(answer)
        assert await stop({}, None, None) == {}  # the files weren't read
        answer["read_files"] = True
        held = await stop({}, None, None)
        assert held["decision"] == "block" and "just finish" in held["reason"]
        assert await stop({}, None, None) == {}  # once: a task that only used them ends

    asyncio.run(main())


def test_the_delegate_answers_simple_file_questions_itself(run, tmp_path):  # noqa: F811
    async def body(library):
        _, _, agent, _, _ = services(tmp_path, None)
        tools = {t.__name__: t for t in delegate_tools(agent, *[None] * 5, None, library=library)}
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        params = Params()
        await tools["search_files"](params, query="notice to terminate the lease")
        hit = params.result["passages"][0]
        assert (hit["document"], hit["location"]) == ("Lease.pdf", "page 2")
        quote = {"document": "Lease.pdf", "location": "page 2", "quote": "sixty (60) days' written"}
        await tools["check_answer_from_files"](params, answer="Sixty days.", citations=[quote])
        assert params.result["checked"] and "on the apps page" in params.result["page"]
        wrong = quote | {"quote": "ninety days' notice"}
        await tools["check_answer_from_files"](params, answer="Ninety.", citations=[wrong])
        assert params.result["checked"] is False

    run(body)


@pytest.mark.parametrize(
    "url", ["http://192.168.1.6:8080/library", "http://127.0.0.1/", "http://localhost/",
            "http://10.0.0.1/", "ftp://example.com/x", "file:///etc/passwd"],
)  # fmt: skip
def test_pages_on_the_users_own_network_are_refused(url):
    with pytest.raises(ValueError):
        asyncio.run(webcheck.public_address(url))


def test_a_page_is_fetched_from_the_address_checked(monkeypatch, tmp_path):
    def addresses(*found):
        return lambda *_: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in found]

    monkeypatch.setattr(socket, "getaddrinfo", addresses("93.184.216.34"))
    assert asyncio.run(webcheck.public_address("https://a.example/r")) == (
        "a.example",
        "93.184.216.34",
    )
    monkeypatch.setattr(socket, "getaddrinfo", addresses("93.184.216.34", "10.0.0.5"))
    with pytest.raises(ValueError):  # one private address is enough to refuse
        asyncio.run(webcheck.public_address("https://a.example/r"))
    assert "3% a month." in webcheck.page_text(f"<p>{PAGE}</p>".encode(), "text/html")
    assert "3% a month." in webcheck.page_text(pdf_bytes(tmp_path, [PAGE]), "application/pdf")


def test_files_arriving_tell_gpt_live_quietly(run, tmp_path):  # noqa: F811
    async def body(library):
        db, questions, agent, _, _ = services(tmp_path, None)
        sent = []

        class LLM(FakeLLM):
            def create_task(self, coroutine, name):
                asyncio.ensure_future(coroutine)

            async def send_client_event(self, event):
                sent.append(event)

        reminders = Reminders(db, ZoneInfo("UTC"))
        announcer = Announcer(LLM(), db, agent, questions, reminders, library=library)
        announcer.start()
        announcer.session_open = True
        await ready(library, "a.txt", b"Alpha is the first letter.")
        await ready(library, "b.txt", b"Beta is the second letter.")
        await asyncio.sleep(0.05)
        assert len(sent) == 1 and sent[0].type == "session.instructions.append"  # once; silent
        assert "backend" in sent[0].content
        announcer.stop()

    run(body)
