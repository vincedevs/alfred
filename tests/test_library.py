"""The library: files read into located text, searched, read back, and citations checked.
Claude vision is a stand-in here; the files are made on the spot."""

import asyncio
from pathlib import Path

import pandas as pd
import pytest
from fpdf import FPDF
from PIL import Image
from pptx import Presentation

from alfred import library as library_module
from alfred.library import (
    DIMENSIONS,
    Library,
    Limits,
    Refused,
    answer_page,
    normal,
    pick,
    sections,
    windows,
)

SCAN = "Scanned copy: the deposit is two months' rent, returned within thirty days."


async def fake_vision(images: list[Path]) -> list[str]:
    return [SCAN for _ in images]


# A stand-in for OpenAI's embeddings that knows a few synonyms: words of one meaning land
# on the same numbers, so "cancel" finds "termination" by meaning, not by keyword.
MEANINGS = {"cancel": "end", "terminate": "end", "termination": "end", "quit": "end",
            "pet": "animal", "pets": "animal", "dog": "animal", "rent": "money",
            "deposit": "money", "pesos": "money"}  # fmt: skip


async def fake_embedder(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        vector = [0.0] * DIMENSIONS
        for word in normal(text).split():
            vector[hash(MEANINGS.get(word, word)) % DIMENSIONS] += 1.0
        length = sum(v * v for v in vector) ** 0.5 or 1.0
        vectors.append([v / length for v in vector])
    return vectors


def make(tmp_path, embedder=fake_embedder, **limits) -> Library:
    limits = {"max_mb": 5, **limits}
    return Library(tmp_path / "library", fake_vision, Limits(**limits), embedder, "fake")


def pdf_bytes(tmp_path, pages: list[str]) -> bytes:
    pdf = FPDF()
    for text in pages:
        pdf.add_page()
        if text:  # an empty page stands in for a scan: no text layer
            pdf.set_font("Helvetica", size=11)
            pdf.multi_cell(0, 7, text)
    path = tmp_path / "made.pdf"
    pdf.output(str(path))
    return path.read_bytes()


LEASE = [
    "Residential Lease. This lease is between the Owner and the Tenant for the flat at "
    "12 Mango Street, Makati.",
    "14. Termination. Either party may terminate this lease with sixty (60) days' written "
    "notice. Early termination by the Tenant forfeits the deposit.",
    "",  # scanned
]


async def ready(library: Library, name: str, data: bytes):
    doc, _ = library.add(name, data)
    for _ in range(400):
        doc = library.get(doc.id)
        if doc.status in ("ready", "failed"):
            return doc
        await asyncio.sleep(0.01)
    raise AssertionError(f"{name} never finished: {doc}")


@pytest.fixture
def run(tmp_path):
    """Runs a test body with a started library, then stops it."""

    def go(body):
        async def main():
            library = make(tmp_path)
            library.start()
            try:
                await body(library)
            finally:
                await library.stop()

        asyncio.run(main())

    return go


def test_a_pdf_is_read_page_by_page_with_scans_by_vision(run, tmp_path):
    async def body(library):
        doc = await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        assert doc.status == "ready" and (doc.done, doc.total) == (3, 3)
        assert "1 scanned page" in doc.note
        page = library.read("lease", "page 2")
        assert "sixty (60) days" in page["text"] and "[page 2]" in page["text"]
        assert "(scanned)" in library.read("Lease.pdf", "3")["text"]
        assert "[page 1]" in library.read("lease", "1-2")["text"]  # a range
        assert "error" in library.read("lease", "page 9")
        assert "error" in library.read("the contract", "page 1")

    run(body)


def test_search_finds_other_forms_of_a_word(run, tmp_path):
    async def body(library):
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        found = await library.search("can I terminate early?")  # the page says "termination"
        assert found[0]["document"] == "Lease.pdf" and found[0]["location"] == "page 2"
        assert (await library.search("deposit returned"))[0]["location"] in ("page 3", "page 2")
        assert library._by_keyword("the a of", None) == []  # only common words
        assert await library.search("deposit", document="nothing-like-it") != []  # unknown: all

    run(body)


def test_office_files_get_slides_sheets_and_sections(tmp_path):
    deck = Presentation()
    for title in ("Q3 results", "Revenue grew 12%"):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = title
    deck.save(tmp_path / "deck.pptx")
    with pd.ExcelWriter(tmp_path / "book.xlsx") as sheets:
        pd.DataFrame({"month": ["Jan", "Feb"], "sales": [100, 250]}).to_excel(
            sheets, sheet_name="Sales", index=False
        )
    (tmp_path / "page.html").write_text("<h1>Policy</h1><p>Intro</p><h2>Refunds</h2><p>60 days</p>")
    md = library_module.to_markdown
    assert [p.location for p in sections(md(tmp_path / "deck.pptx"), ".pptx")] == [
        "slide 1",
        "slide 2",
    ]
    html = sections(md(tmp_path / "page.html"), ".html")
    assert [p.location for p in html] == ["§ Policy", "§ Refunds"] and "60 days" in html[1].text


def test_sections_stay_readable_and_unique():
    long = sections("## Terms\n" + ("word " * 3000), ".docx")
    assert long[0].location == "§ Terms (part 1)" and len(long[0].text) <= 6000
    twice = sections("## Fees\none\n## Fees\ntwo", ".docx")
    assert [p.location for p in twice] == ["§ Fees", "§ Fees (2)"]  # each names one place
    assert all(len(w) <= 1500 for w in windows("x " * 5000, 1500, 200))


def test_text_files_images_and_spreadsheets(run, tmp_path):
    async def body(library):
        lines = "\n".join(f"line {i}" for i in range(1, 121))
        notes = await ready(library, "notes.txt", lines.encode())
        assert [p["location"] for p in library._parts(notes.id)] == ["lines 1-80", "lines 81-120"]
        assert (notes.done, notes.total) == (2, 2)
        buffer = tmp_path / "photo.png"
        Image.new("RGB", (40, 40), "white").save(buffer)
        photo = await ready(library, "receipt.png", buffer.read_bytes())
        assert SCAN in library.read(photo.id, "page 1")["text"]
        frame = pd.DataFrame({"month": ["Jan"], "sales": [100]})
        frame.to_csv(tmp_path / "s.csv", index=False)
        sheet = await ready(library, "sales.csv", (tmp_path / "s.csv").read_bytes())
        assert "worked out from the file" in sheet.note  # exact figures come from code
        assert sheet.describe()["file"] == sheet.path  # the agent calculates from it

    run(body)


def test_citations_are_checked_against_the_file(run, tmp_path):
    async def body(library):
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        good = {
            "document": "Lease",
            "location": "page 2",
            "quote": "sixty (60) days’ written notice",
        }
        wrong_page = good | {"location": "page 1"}
        made_up = good | {"quote": "ninety (90) days' written notice"}
        across = good | {"location": "page 1", "quote": "Street, Makati. 14. Termination"}
        scanned = {"document": "Lease", "location": "page 3", "quote": "deposit is two months rent"}
        short = good | {"quote": "notice"}
        missing = good | {"document": "Car loan"}
        results = library.check([good, wrong_page, made_up, scanned, short, missing])
        assert results[0] is None and results[3] is None  # exact; loose on a scan
        assert "isn't at page 1" in results[1] and "isn't at" in results[2]
        assert "too short" in results[4] and "isn't in the library" in results[5]
        assert library.check([across]) == [None]  # a quote across a page break
        page = answer_page("Sixty days' notice.", [good, scanned], library)
        assert "#page=2" in page and "scanned copy" in page and "sixty (60)" in page

    run(body)


def test_same_file_is_skipped_new_version_replaces_delete_removes(run, tmp_path):
    async def body(library):
        first = await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        assert library.add("Lease.pdf", Path(first.path).read_bytes())[1] == "already there"
        changed = LEASE[:2] + ["14b. The rent is 30,000 pesos a month, due on the 5th."]
        second = await ready(library, "lease.pdf", pdf_bytes(tmp_path, changed))
        assert [d.id for d in library.documents()] == [second.id]  # the old one is gone
        assert (await library.search("rent pesos"))[0]["location"] == "page 3"
        assert library.delete(second.id) and library.documents() == []
        assert await library.search("termination") == [] and not Path(second.path).exists()
        with pytest.raises(Refused, match="the limit is 5 MB"):
            library.add("huge.txt", b"x" * (6 * 1024 * 1024))

    run(body)


def test_pages_are_searchable_while_the_rest_is_read(run, tmp_path, monkeypatch):
    async def body(library):
        monkeypatch.setattr(library_module, "PAGES_AT_ONCE", 1)
        gate = asyncio.Event()

        async def slow_vision(images):
            await gate.wait()  # the scanned page 3 takes a while
            return [SCAN for _ in images]

        library._vision = slow_vision
        ready_docs = []
        library.on_ready.append(ready_docs.append)
        doc, _ = library.add("Lease.pdf", pdf_bytes(tmp_path, LEASE))
        for _ in range(200):
            if await library.search("termination"):
                break
            await asyncio.sleep(0.01)
        assert await library.search("termination")  # page 2 is in before page 3 is read
        assert library.get(doc.id).status == "processing" and ready_docs == []
        assert "still_processing" in library.read("lease", "page 1")
        gate.set()
        for _ in range(200):
            if library.get(doc.id).status == "ready":
                break
            await asyncio.sleep(0.01)
        assert [d.name for d in ready_docs] == ["Lease.pdf"]

    run(body)


def test_unfinished_files_carry_on_after_a_restart(tmp_path):
    async def main():
        first = make(tmp_path)  # never started: stays waiting
        doc, _ = first.add("Lease.pdf", pdf_bytes(tmp_path, LEASE))
        again = make(tmp_path)
        again.start()
        for _ in range(400):
            if again.get(doc.id).status == "ready":
                break
            await asyncio.sleep(0.01)
        assert again.get(doc.id).status == "ready"
        await again.stop()

    asyncio.run(main())


def test_quotes_match_however_they_are_typed():
    assert normal("Sixty (60) days’ written\nnotice.") == normal("sixty (60) days' written notice")
    assert library_module.loosely_in(normal("the deposit is two months rent"), normal(SCAN))
    assert not library_module.loosely_in(normal("the deposit is refundable"), normal(SCAN))
    parts = [{"ord": i, "location": f"page {i + 1}"} for i in range(5)] + [
        {"ord": 5, "location": "§ 14. Termination"}
    ]
    assert [p["ord"] for p in pick(parts, "pages 2-3")] == [1, 2]
    assert [p["ord"] for p in pick(parts, "Termination")] == [5]


def test_the_agent_checks_its_answer_before_giving_it(run, tmp_path):
    import json

    from alfred.agent import library_tools

    async def body(library):
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        pages = tmp_path / "home" / ".alfred" / "pages"
        tools = {t.name: t.handler for t in library_tools(library, "home", pages)}

        async def call(name, **args):
            text = (await tools[name](args))["content"][0]["text"]
            return json.loads(text) if text[:1] in "[{" else text

        assert (await call("list_documents"))[0]["status"] == "ready"
        hits = await call("search_documents", query="terminate", document="")
        assert hits[0]["location"] == "page 2"
        assert "sixty" in (await call("read_document", document="Lease", location="page 2"))["text"]
        wrong = {"document": "Lease", "location": "page 2", "quote": "ninety days' notice"}
        refused = await call("cite_answer", answer="Ninety days.", citations=[wrong])
        assert refused["checked"] is False and refused["fix"][0]["citation"] == 1
        assert not pages.exists()  # nothing written for an unchecked answer
        right = wrong | {"quote": "sixty (60) days' written notice"}
        scan = {"document": "Lease", "location": "page 3", "quote": "two months' rent"}
        accepted = await call("cite_answer", answer="Sixty days.", citations=[right, scan])
        assert accepted["checked"] and "scanned copy" in accepted["say"]
        assert accepted["page"].startswith("pages/home/answer-")
        assert len(list(pages.glob("answer-*.html"))) == 1
        empty = await call("cite_answer", answer="Sixty days.", citations=[])
        assert empty["checked"] is False  # an answer from documents needs a citation

    run(body)


def test_claude_vision_answers_are_split_per_page(tmp_path, monkeypatch):
    import claude_agent_sdk

    from alfred.library import claude_vision

    asked = {}

    async def fake_query(prompt, options):
        asked.update(prompt=prompt, tools=options.tools, model=options.model)
        yield claude_agent_sdk.ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
            session_id="s", result="=== page-1.png ===\nFirst page.\n=== page-2.png ===\nSecond.",
        )  # fmt: skip

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    images = [tmp_path / "page-1.png", tmp_path / "page-2.png", tmp_path / "page-3.png"]
    texts = asyncio.run(claude_vision("claude-haiku-5-5")(images))
    assert texts == ["First page.", "Second.", "[unclear]"]  # a page it skipped is unclear
    assert asked["tools"] == ["Read"] and "page-3.png" in asked["prompt"]


def test_the_library_list_reaches_the_prompts(run, tmp_path):
    async def body(library):
        assert library.contents_text() == ""  # nothing to mention
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        text = library.contents_text()
        assert "- Lease.pdf (3 pages)" in text and "answered from them first" in text

    run(body)


def test_hybrid_search_finds_other_wording_by_meaning(tmp_path):
    async def main():
        hybrid, keyword_only = make(tmp_path / "a"), make(tmp_path / "b", embedder=None)
        for library in (hybrid, keyword_only):
            library.start()
            await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        # "cancel" isn't in the lease: keyword search alone finds nothing
        assert await keyword_only.search("cancel") == []
        found = await hybrid.search("cancel")
        assert found[0]["location"] == "page 2"  # "Termination", found by meaning
        both = await hybrid.search("termination notice")  # found both ways: ranked first
        assert both[0]["location"] == "page 2"
        only_lease = await hybrid.search("cancel", document="lease")
        assert {f["document"] for f in only_lease} == {"Lease.pdf"}
        for library in (hybrid, keyword_only):
            await library.stop()

    asyncio.run(main())


def test_offline_embeddings_catch_up_later(tmp_path):
    async def main():
        online = False

        async def flaky(texts):
            if not online:
                raise ConnectionError("no internet")
            return await fake_embedder(texts)

        library = make(tmp_path, embedder=flaky)
        library.start()
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        assert (await library.search("termination"))[0]["location"] == "page 2"  # keywords
        assert await library.search("cancel") == []  # no meaning search yet
        online = True
        assert await library.embed_missing() > 0
        assert (await library.search("cancel"))[0]["location"] == "page 2"
        await library.stop()

    asyncio.run(main())


def test_a_new_embedding_model_embeds_everything_again(tmp_path):
    async def main():
        library = make(tmp_path)
        library.start()
        await ready(library, "Lease.pdf", pdf_bytes(tmp_path, LEASE))
        await library.stop()
        again = Library(tmp_path / "library", fake_vision, Limits(), fake_embedder, "newer")
        assert again._sql("SELECT count(*) FROM vectors")[0][0] == 0  # old ones dropped
        assert await again.embed_missing() > 0

    asyncio.run(main())


def test_uploads_arrive_in_pieces_and_resume(run, tmp_path):
    async def body(library):
        data = pdf_bytes(tmp_path, LEASE)
        first, rest = data[:1000], data[1000:]
        assert library.receive("upload-0001", 0, first, len(data), "Lease.pdf") == 1000
        assert library.receive("upload-0001", 0, first, len(data), "Lease.pdf") == 1000  # again
        assert library.receive("upload-0001", 5000, rest, len(data), "Lease.pdf") == 1000  # gap
        assert library.receive("upload-0001", 1000, rest, len(data), "Lease.pdf") == len(data)
        doc, what = library.finish("upload-0001", "Lease.pdf")
        assert what == "added" and Path(doc.path).read_bytes() == data
        assert not list(library.incoming.glob("*.part"))
        with pytest.raises(Refused, match="no proper id"):
            library.receive("../../x", 0, b"x", 1, "a.txt")
        changed = pdf_bytes(tmp_path, LEASE[:2])
        library.receive("upload-0002", 0, changed, len(changed), "Lease.pdf")
        assert library.finish("upload-0002", "Lease.pdf")[1] == "exists"  # no yes to replace
        library.receive("upload-0003", 0, changed, len(changed), "Lease.pdf")
        assert library.finish("upload-0003", "Lease.pdf", replace=True)[1] == "replaced"

    run(body)


def test_caps_and_space_are_kept(tmp_path, monkeypatch):
    library = make(tmp_path, max_mb=5, max_gb=0.000005)  # about 5 KB in all
    with pytest.raises(Refused, match="limit is 5 MB"):
        library.check_upload("big.pdf", 6 * 1024 * 1024)
    big_doc = library_module.WHOLE_FILE_MB * 1024 * 1024 + 1
    with pytest.raises(Refused, match="read whole"):
        Library(tmp_path / "l2", None, Limits(max_mb=500)).check_upload("big.docx", big_doc)
    with pytest.raises(Refused, match="library is full"):
        library.check_upload("notes.txt", 10_000)
    roomy = make(tmp_path / "roomy")
    monkeypatch.setattr(
        library_module.shutil, "disk_usage", lambda _p: type("U", (), {"free": 10 << 30})()
    )
    with pytest.raises(Refused, match="keeps 10 GB free"):
        roomy.check_upload("notes.txt", 1000)
    assert roomy.space()["free"] == 10 << 30


def test_spreadsheets_are_read_row_by_row(run, tmp_path):
    async def body(library):
        rows = pd.DataFrame({"month": [f"m{i}" for i in range(1200)], "sales": range(1200)})
        with pd.ExcelWriter(tmp_path / "book.xlsx") as sheets:
            rows.to_excel(sheets, sheet_name="Sales", index=False)
            pd.DataFrame({"a": [1]}).to_excel(sheets, sheet_name="Notes", index=False)
        doc = await ready(library, "book.xlsx", (tmp_path / "book.xlsx").read_bytes())
        locations = [p["location"] for p in library._parts(doc.id)]
        assert locations == [
            "sheet Sales, rows 2-501", "sheet Sales, rows 502-1001", "sheet Sales, rows 1002-1201",
            "sheet Notes, rows 2-2",
        ]  # fmt: skip
        second = library.read("book", "sheet Sales, rows 502-1001")["text"]
        assert second.splitlines()[1].startswith("| month | sales |")  # the header, again
        assert "| m500 | 500 |" in second
        whole = library.read("book", "sheet Sales")["text"]  # every part of the sheet
        assert "| m0 | 0 |" in whole and "| m1199 | 1199 |" in whole
        rows.to_csv(tmp_path / "s.csv", index=False)
        csv_doc = await ready(library, "sales.csv", (tmp_path / "s.csv").read_bytes())
        assert library._parts(csv_doc.id)[0]["location"] == "rows 2-501"

    run(body)


def test_small_files_are_read_first(tmp_path):
    async def main():
        library = make(tmp_path)
        order = []
        library.on_ready.append(lambda d: order.append(d.name))
        library.add("big.txt", b"big file " * 50_000)
        library.add("small.txt", b"a small file")
        library.start()  # both waiting: the small one goes first
        for _ in range(400):
            if len(order) == 2:
                break
            await asyncio.sleep(0.01)
        assert order == ["small.txt", "big.txt"]
        await library.stop()

    asyncio.run(main())


def test_a_large_scan_waits_for_a_yes(tmp_path):
    async def main():
        library = make(tmp_path, ask_pages=1)
        asked = []
        library.on_asking.append(asked.append)
        library.start()
        doc, _ = library.add("Scan.pdf", pdf_bytes(tmp_path, ["", "", "Typed page " * 10]))
        for _ in range(200):
            if library.get(doc.id).status == "asking":
                break
            await asyncio.sleep(0.01)
        waiting = library.get(doc.id)
        assert waiting.status == "asking" and "2 scanned pages, about 4,000 tokens" in waiting.note
        assert asked and library.scans_left() == 1000  # nothing read yet
        assert library.approve("scan").approved
        for _ in range(200):
            if library.get(doc.id).status == "ready":
                break
            await asyncio.sleep(0.01)
        assert library.get(doc.id).status == "ready" and library.scans_left() == 998
        await library.stop()

    asyncio.run(main())


def test_the_daily_scan_limit_pauses_and_carries_on(tmp_path):
    async def main():
        library = make(tmp_path, daily_pages=1)
        library.start()
        pages = ["Typed page one " * 5, "", ""]  # one scan fits today, the next waits
        doc, _ = library.add("Scan.pdf", pdf_bytes(tmp_path, pages))
        for _ in range(200):
            if library.get(doc.id).status == "paused":
                break
            await asyncio.sleep(0.01)
        paused = library.get(doc.id)
        assert paused.status == "paused" and "tomorrow" in paused.note
        assert (await library.search("typed"))[0]["location"] == "page 1"  # usable meanwhile
        assert paused.done == 2 and library.scans_left() == 0  # page 2's scan used today's one
        library.limits.daily_pages = 10  # as if it were tomorrow
        await library._idle()
        for _ in range(200):
            if library.get(doc.id).status == "ready":
                break
            await asyncio.sleep(0.01)
        assert library.get(doc.id).status == "ready"
        assert [p["location"] for p in library._parts(doc.id)] == ["page 1", "page 2", "page 3"]
        await library.stop()

    asyncio.run(main())


def test_a_big_library_is_summarised_in_prompts(run, tmp_path, monkeypatch):
    async def body(library):
        monkeypatch.setattr(library_module, "LISTED_IN_PROMPTS", 2)
        for i in range(3):
            await ready(library, f"note-{i}.txt", f"note {i}".encode())
        text = library.contents_text()
        assert "3 files (3 TXT). Newest:" in text and "list_documents" in text

    run(body)


def test_a_library_from_the_first_build_gets_the_new_columns(tmp_path):
    import sqlite3

    root = tmp_path / "library"
    root.mkdir()
    with sqlite3.connect(root / "library.db") as old:
        old.execute(
            "CREATE TABLE documents (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, "
            "status TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL "
            "DEFAULT 0, note TEXT, created_at REAL NOT NULL)"
        )
        old.execute("INSERT INTO documents VALUES ('a', 'a.txt', '/x', 'ready', 1, 1, NULL, 0)")
    library = make(tmp_path)
    assert library.get("a").bytes == 0 and library.get("a").approved == 0
