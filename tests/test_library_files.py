"""A folder of mixed files: zips, Windows text, script-made spreadsheets, old Office files,
pictures in PowerPoint, files Alfred can't read, and starting afresh."""

import asyncio
import io
import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from fpdf import FPDF
from openpyxl import Workbook
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from test_library import make, ready, run  # noqa: F401 (run is a fixture)

from alfred import library as library_module
from alfred.library import Library, Limits, Refused


def zipped(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return out.getvalue()


async def settled(library: Library) -> list:
    for _ in range(500):
        if (docs := library.documents()) and all(d.status in ("ready", "failed") for d in docs):
            return docs
        await asyncio.sleep(0.01)
    raise AssertionError(library.documents())


def test_a_zip_is_unpacked_into_its_files(run, tmp_path):  # noqa: F811
    policy = b"Late payments incur a penalty of 5% per month."
    data = zipped({
        "ME4/policy.txt": policy,
        "ME4/copy.txt": policy,  # the same file twice
        "ME4/reports/q3.csv": b"Month,Sales\nJuly,120\n",
        "ME4/archive/q3.csv": b"Month,Sales\nJuly,99\n",  # same name, other folder
        "ME4/inner.zip": zipped({"notes.txt": b"The office opens at 8:30."}),
        "ME4/../../escape.txt": b"climbs out", "ME4/__MACOSX/._x": b"", "ME4/.DS_Store": b"",
    })  # fmt: skip

    async def body(library):
        doc, what = library.add("ME4.zip", data)
        assert doc is None and what == (
            "ME4.zip: 4 added, 1 already there, 1 zip(s) inside unpacked, "
            "1 skipped (a path outside the zip)"
        )
        names = sorted(d.name for d in await settled(library))
        assert names == ["archive/q3.csv", "inner/notes.txt", "policy.txt", "reports/q3.csv"]
        assert not list(tmp_path.rglob("escape.txt")) and not list(library.incoming.iterdir())
        assert (await library.search("late payment penalty"))[0]["document"] == "policy.txt"

    run(body)


def test_a_zip_bomb_or_a_broken_zip_is_refused(run, monkeypatch):  # noqa: F811
    async def body(library):
        _, what = library.add("bomb.zip", zipped({"zeros.txt": b"0" * (6 << 20)}))  # 5 MB cap
        assert what == "bomb.zip: 1 skipped (too large)" and library.documents() == []
        small = zipped({"a.txt": b"a" * 100_000})
        monkeypatch.setattr(
            library, "space", lambda: {"used": 0, "limit": len(small) + 9, "free": 1 << 50}
        )
        with pytest.raises(Refused, match="there isn't room"):
            library.add("big.zip", small)
        with pytest.raises(Refused, match="can't be opened"):
            library.add("broken.zip", b"PK\3\4 not a zip")

    run(body)


def test_windows_text_and_formulas_read_right(run):  # noqa: F811
    book = Workbook()  # made by a script: no saved results
    for row in [("Month", "Amount"), ("Jan", 100), ("Total", "=SUM(B2:B2)")]:
        book.active.append(row)
    sheet = io.BytesIO()
    book.save(sheet)

    async def body(library):
        csv = await ready(library, "staff.csv", "Name,Town\nJuan,Peñafrancia\n".encode("cp1252"))
        assert library.check([{"document": "staff.csv", "location": "rows 2-2",
                               "quote": "Juan | Peñafrancia"}]) == [None]  # fmt: skip
        bom = await ready(library, "ids.csv", "﻿ID,Amount\n7,42\n".encode())
        assert "| ID | Amount |" in library.read(bom.id, "rows 2-2")["text"]
        utf16 = await ready(library, "notes.txt", "Cañon office\n".encode("utf-16"))
        assert "Cañon" in library.read(utf16.id, "lines 1-1")["text"]
        xlsx = await ready(library, "sales.xlsx", sheet.getvalue())
        assert "| Total | =SUM(B2:B2) |" in library.read(xlsx.id, "rows 2-3")["text"]
        assert csv.status == "ready"

    run(body)


def test_files_it_cant_read_fail_with_the_reason(run, monkeypatch):  # noqa: F811
    monkeypatch.setattr(library_module, "libreoffice", lambda: None)

    async def body(library):
        binary = await ready(library, "mystery.xyz", os.urandom(500) + b"\0")
        assert binary.status == "failed" and binary.note == "Alfred can't read .xyz files."
        doc = await ready(library, "minutes.doc", b"\xd0\xcf\x11\xe0 old Word")
        assert doc.status == "failed" and "install LibreOffice" in doc.note
        conf = await ready(library, "app.conf", b"budget = 4200 pesos\n")  # unknown, but text
        assert conf.status == "ready"

    run(body)


@pytest.mark.skipif(not library_module.libreoffice(), reason="LibreOffice isn't installed")
def test_an_old_office_file_is_converted_and_keeps_its_name(run):  # noqa: F811
    rtf = rb"{\rtf1\ansi\pard The board meets every second Tuesday.\par}"

    async def body(library):
        doc = await ready(library, "minutes.rtf", rtf)
        found = await library.search("when does the board meet")
        assert doc.status == "ready" and found[0]["document"] == "minutes.rtf"
        assert doc.readable.endswith(".docx")
        library.delete(doc.id)
        assert not Path(doc.readable).exists()

    run(body)


def test_a_chart_in_powerpoint_is_read_and_can_be_looked_at(tmp_path):
    async def vision(images):
        return ["[bar chart: Q3 is highest at 120]" for _ in images]

    chart = tmp_path / "chart.png"
    Image.frombytes("RGB", (300, 200), os.urandom(180_000)).save(chart)  # big: not an icon
    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[5])
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.add_picture(
        str(chart), Inches(1), Inches(2)
    )
    out = io.BytesIO()
    deck.save(out)

    async def main():
        library = Library(tmp_path / "library", vision, Limits(max_mb=5))
        library.start()
        doc = await ready(library, "review.pptx", out.getvalue())
        assert (await library.search("which quarter is highest"))[0][
            "location"
        ] == "slide 2, picture 1"
        read = library.read("review", "slide 2, picture 1")
        assert "Q3" in read["text"] and Path(read["pictures"][0]).exists()
        assert library.scans_left() == library.limits.daily_pages - 1  # counts towards the limit
        library.delete(doc.id)
        assert not Path(read["pictures"][0]).exists()
        await library.stop()

    asyncio.run(main())


def test_clearing_and_files_still_being_read(run, tmp_path):  # noqa: F811
    waiting = make(tmp_path / "other")  # not started: what's added waits
    waiting.add("a.txt", b"Alpha is the first letter.")
    assert "1 still being read." in waiting.contents_text()

    async def body(library):
        changes = []
        library.on_change.append(lambda: changes.append(1))
        await ready(library, "a.txt", b"Alpha is the first letter.")
        library.record("home", "Alpha.", [])
        assert library.clear() == 1 and library.documents() == []
        assert library._sql("SELECT * FROM answers") == []
        await asyncio.sleep(0.01)
        assert len(changes) == 2  # added, then deleted

    run(body)


def test_zip_names_from_a_mac_and_broken_zips_inside(run):  # noqa: F811
    mac = zipped({"Peñafrancia notes.txt": b"Branch notes for the week."})
    mac = mac.replace(b"\x00\x08", b"\x00\x00")  # as a Mac writes it: UTF-8, not flagged
    bad = zipped({"a.txt": b"Alpha is first.", "bad.zip": b"PK\3\4 broken", "b.txt": b"Beta."})

    async def body(library):
        library.add("mac.zip", mac)
        _, what = library.add("outer.zip", bad)
        assert "1 skipped (a zip inside that can't be opened)" in what
        names = sorted(d.name for d in await settled(library))
        assert names == ["Peñafrancia notes.txt", "a.txt", "b.txt"]
        library.check_upload("huge.zip", 2 << 30)  # a zip may be larger than one file may

    run(body)


def test_a_short_name_that_fits_two_files_is_never_guessed(run):  # noqa: F811
    async def body(library):
        library.add(
            "q3.zip", zipped({"reports/q3.csv": b"a,b\n1,2\n", "archive/q3.csv": b"a\n9\n"})
        )
        await settled(library)
        assert (
            library.find("q3.csv") is None
            and library.find("reports/q3.csv").name == "reports/q3.csv"
        )

    run(body)


def test_a_file_deleted_while_it_is_read_leaves_nothing(run):  # noqa: F811
    rows = "".join(f"Item {i},{i * 3}\n" for i in range(200_000))  # read in many batches

    async def body(library):
        doc, _ = library.add("big.csv", f"Name,Count\n{rows}".encode())
        while (library.get(doc.id).done or 0) == 0:
            await asyncio.sleep(0.01)
        library.delete(doc.id)
        await asyncio.sleep(1)
        assert library._sql("SELECT count(*) FROM parts WHERE doc_id = ?", (doc.id,))[0][0] == 0
        assert await library.search("Item 199999") == []

    run(body)


def test_pictures_in_groups_pdfs_word_charts_and_odd_images(tmp_path):
    seen = []

    async def vision(images):
        for image in images:
            with Image.open(image) as opened:
                seen.append((image.suffix, max(opened.size)))
        return ["[bar chart: Q3 is highest]" for _ in images]

    chart = tmp_path / "chart.png"
    Image.frombytes("RGB", (300, 200), os.urandom(180_000)).save(chart)
    deck = Presentation()
    for _ in range(2):  # the same chart on two slides, once in a group
        group = deck.slides.add_slide(deck.slide_layouts[6]).shapes.add_group_shape()
        group.shapes.add_picture(str(chart), Inches(1), Inches(1))
    slides = io.BytesIO()
    deck.save(slides)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 7, "Quarterly report. The chart below shows sales by quarter.")
    pdf.image(str(chart), x=10, y=40, w=180)  # most of the page
    tiff = io.BytesIO()
    Image.frombytes("RGB", (3000, 100), os.urandom(900_000)).save(tiff, "TIFF")

    async def main():
        library = Library(tmp_path / "library", vision, Limits(max_mb=5, daily_pages=3))
        library.start()
        doc = await ready(library, "deck.pptx", slides.getvalue())
        assert [p["location"] for p in library._parts(doc.id)][-1] == "slide 1, picture 1"
        assert len(seen) == 1  # read once, though on two slides
        report = await ready(library, "report.pdf", bytes(pdf.output()))
        locations = [p["location"] for p in library._parts(report.id)]
        assert locations == ["page 1", "page 1, picture"] and len(seen) == 2
        image = await ready(library, "scan.tiff", tiff.getvalue())
        assert image.status == "ready" and seen[-1] == (
            ".jpg",
            1568,
        )  # a photo: JPEG, Claude's size
        Image.frombytes("RGB", (300, 200), os.urandom(180_000)).save(chart)  # a new picture
        more = Presentation()
        more.slides.add_slide(more.slide_layouts[6]).shapes.add_picture(str(chart), 0, 0)
        out = io.BytesIO()
        more.save(out)
        over = await ready(library, "more.pptx", out.getvalue())  # the daily limit (3) is used
        assert over.note == "1 picture(s) not read: today's scan limit"
        await library.stop()

    asyncio.run(main())


@pytest.mark.skipif(not library_module.libreoffice(), reason="LibreOffice isn't installed")
def test_a_word_chart_is_read_from_its_data(run, tmp_path):  # noqa: F811
    book = Workbook()
    for row in [("Quarter", "Sales"), ("Q1", 120), ("Q3", 190)]:
        book.active.append(row)
    data = io.BytesIO()
    book.save(data)
    (tmp_path / "x.rtf").write_text(r"{\rtf1 Sales report.\par}")
    office = ["soffice", "--headless", "--convert-to", "docx", "--outdir", tmp_path]
    subprocess.run([*office, tmp_path / "x.rtf"], capture_output=True, check=True)
    word = tmp_path / "report.docx"  # a Word file with a chart's data inside, as Word keeps it
    with zipfile.ZipFile(tmp_path / "x.docx") as plain, zipfile.ZipFile(word, "w") as out:
        for item in plain.infolist():
            out.writestr(item, plain.read(item))
        out.writestr("word/embeddings/Microsoft_Excel_Worksheet.xlsx", data.getvalue())

    async def body(library):
        doc = await ready(library, "report.docx", word.read_bytes())
        found = await library.search("Q3 sales")
        assert found[0]["location"] == "chart 1, sheet Sheet, rows 2-3" and doc.status == "ready"

    run(body)


def test_text_is_searchable_before_pictures_and_meaning_catches_up(tmp_path):
    reading = asyncio.Event()

    async def vision(images):
        reading.set()
        await asyncio.sleep(1)  # Claude takes a while
        return ["[chart]" for _ in images]

    chart = tmp_path / "chart.png"
    Image.frombytes("RGB", (300, 200), os.urandom(180_000)).save(chart)
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Quarterly sales review"
    slide.shapes.add_picture(str(chart), 0, 0)
    out = io.BytesIO()
    deck.save(out)

    async def main():
        library = make(tmp_path)
        library._vision = vision
        library.start()
        doc, _ = library.add("review.pptx", out.getvalue())
        await reading.wait()  # the picture is being read, in the second pass...
        assert (await library.search("quarterly sales"))[0]["document"] == "review.pptx"
        assert (
            library.get(doc.id).status == "ready" and library.get(doc.id).later
        )  # ...the text is in
        await ready(library, "review.pptx", out.getvalue())
        assert library.vectors and not library._embedding  # meaning search caught up
        await library.stop()

    asyncio.run(main())


def deck_with_picture(tmp_path, title: str) -> bytes:
    chart = tmp_path / f"{title}.png"
    Image.frombytes("RGB", (300, 200), os.urandom(180_000)).save(chart)  # big: not an icon
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = title
    slide.shapes.add_picture(str(chart), 0, 0)
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


def test_every_files_text_comes_before_any_pictures(tmp_path):
    async def vision(images):  # Claude is slow: both workers could be stuck here
        await asyncio.sleep(30)
        return ["[chart]" for _ in images]

    async def main():
        library = Library(tmp_path / "library", vision, Limits(max_mb=5))
        library.add("a.pptx", deck_with_picture(tmp_path, "Alpha sales"))
        library.add("b.pptx", deck_with_picture(tmp_path, "Beta costs"))
        library.add("notes.txt", b"Gamma notes " * 40_000)  # bigger: read after both decks
        library.start()
        for _ in range(400):  # Claude is still on the first picture all this time
            if all(d.status == "ready" for d in library.documents()):
                break
            await asyncio.sleep(0.01)
        assert all(d.status == "ready" for d in library.documents())  # text: nobody waited
        assert {d.name for d in library.documents() if d.later} == {"a.pptx", "b.pptx"}
        assert (await library.search("beta costs"))[0]["document"] == "b.pptx"
        await library.stop()

    asyncio.run(main())


def test_two_workers_never_go_over_the_daily_scan_limit(tmp_path):
    async def main():
        library = make(tmp_path, daily_pages=1)
        photo = io.BytesIO()
        Image.frombytes("RGB", (300, 200), os.urandom(180_000)).save(photo, "PNG")
        library.add("one.png", photo.getvalue())
        library.add("two.png", photo.getvalue()[:-1] + b"\0")  # another file, same size
        library.start()
        for _ in range(400):
            if {d.status for d in library.documents()} == {"ready", "paused"}:
                break
            await asyncio.sleep(0.01)
        assert sorted(d.status for d in library.documents()) == ["paused", "ready"]
        assert library.scans_left() == 0
        await library.stop()

    asyncio.run(main())


def test_each_file_gets_a_line_on_what_it_is_and_is_found_by_it(tmp_path):
    asked = []

    async def describer(files):
        asked.append([name for name, _ in files])
        return ["Lease for Unit 4B, Makati, March 2024" for _ in files]

    async def main():
        library = Library(tmp_path / "library", None, Limits(max_mb=5), describer=describer)
        library.start()
        doc = await ready(library, "scan_0041.txt", b"This lease is between the Owner and...")
        for _ in range(400):
            if library.get(doc.id).about:
                break
            await asyncio.sleep(0.01)
        assert library.find("the lease").name == "scan_0041.txt" and asked == [["scan_0041.txt"]]
        assert "Unit 4B" in library.contents_text()
        described = library.get(doc.id).describe(with_file=False)
        assert described["about"].startswith("Lease") and described["added"] == "just now"
        await library.stop()

    asyncio.run(main())


def test_a_failed_description_changes_nothing(tmp_path):
    async def describer(files):
        raise RuntimeError("Claude is down")

    async def main():
        library = Library(tmp_path / "library", None, Limits(max_mb=5), describer=describer)
        library.start()
        doc = await ready(library, "notes.txt", b"Some notes")
        await asyncio.sleep(0.05)
        assert library.get(doc.id).about is None  # tried again with the next file
        assert library.find("notes").name == "notes.txt"
        await library.stop()

    asyncio.run(main())


def test_the_newest_file_comes_first_and_can_be_searched_alone(run):  # noqa: F811
    from alfred.tools import delegate_tools

    class Params:
        async def result_callback(self, result):
            self.result = result

    async def body(library):
        await ready(library, "old.txt", b"The refund window is 30 days.")
        await asyncio.sleep(0.01)
        await ready(library, "new.txt", b"Refunds take 5 working days.")
        tools = {t.__name__: t for t in delegate_tools(*[None] * 6, library=library)}
        params = Params()
        await tools["list_documents"](params)
        assert [d["document"] for d in params.result["documents"]] == ["new.txt", "old.txt"]
        await tools["search_files"](params, query="refund", document="new.txt")
        assert {p["document"] for p in params.result["passages"]} == {"new.txt"}

    run(body)


def test_pictures_that_fail_lose_only_the_pictures(tmp_path):
    async def vision(images):
        raise RuntimeError("Claude is down")

    async def main():
        library = Library(tmp_path / "library", vision, Limits(max_mb=5))
        library.start()
        doc = await ready(library, "a.pptx", deck_with_picture(tmp_path, "Alpha sales"))
        assert "couldn't be read" in library.get(doc.id).note
        after = await ready(library, "b.txt", b"Still reading files")  # the worker lives on
        assert after.status == "ready" and await library.search("alpha sales")
        await library.stop()

    asyncio.run(main())


def test_only_the_backend_gets_what_each_file_is(run):  # noqa: F811
    async def body(library):
        doc = await ready(library, "scan_0041.txt", b"text")
        library._set(doc.id, about="Lease for Unit 4B")
        assert "Unit 4B" in library.contents_text()
        assert "Unit 4B" not in library.contents_text(with_about=False)  # GPT-Live's

    run(body)


def test_pictures_are_saved_the_way_claude_reads_them(tmp_path):
    from alfred.library import save_picture

    def picture(size, kind, noise=True):
        out = io.BytesIO()
        pixels = os.urandom(size[0] * size[1] * 3) if noise else b"\xff" * (size[0] * size[1] * 3)
        Image.frombytes("RGB", size, pixels).save(out, kind)
        return out.getvalue()

    small = picture((400, 300), "JPEG")
    kept = save_picture(small, tmp_path / "a")
    assert kept.suffix == ".jpg" and kept.read_bytes() == small  # already right: as it came
    photo = save_picture(picture((4000, 3000), "PNG"), tmp_path / "b")
    with Image.open(photo) as opened:  # a photo: JPEG, shrunk to what Claude reads
        assert photo.suffix == ".jpg" and opened.width * opened.height <= 1_150_000
    chart = Image.new("RGB", (3000, 2000), "white")
    chart.paste((200, 30, 30), (100, 100, 900, 1500))  # a bar: few colours
    tiff = io.BytesIO()
    chart.save(tiff, "TIFF")
    drawing = save_picture(tiff.getvalue(), tmp_path / "c")
    assert drawing.suffix == ".png"  # a drawing stays sharp
    assert save_picture(b"not a picture", tmp_path / "d") is None


def test_blank_and_repeated_pictures_dont_go_to_claude(tmp_path):
    sent = []

    async def vision(images):
        sent.extend(images)
        return [f"[chart {len(sent)}]" for _ in images]

    async def main():
        library = Library(tmp_path / "library", vision, Limits(max_mb=5))
        chart = tmp_path / "chart.png"
        Image.frombytes("RGB", (300, 200), os.urandom(180_000)).save(chart)
        white = tmp_path / "white.png"
        Image.new("RGB", (300, 200), "white").save(white)
        assert await library._see([chart, white]) == ["[chart 1]", "[blank]"]
        again = tmp_path / "again.png"
        again.write_bytes(chart.read_bytes())  # the same picture, in another file
        assert await library._see([again]) == ["[chart 1]"] and len(sent) == 1

    asyncio.run(main())


def test_pictures_go_to_claude_in_batches_of_the_set_size(tmp_path):
    calls = []

    async def vision(images):
        calls.append(len(images))
        return ["[picture]" for _ in images]

    async def main():
        library = Library(tmp_path / "library", vision, Limits(vision_per_call=3))
        pictures = []
        for n in range(7):
            pictures.append(tmp_path / f"{n}.png")
            Image.frombytes("RGB", (60, 40), os.urandom(7200)).save(pictures[-1])
        await library._see(pictures)
        assert sorted(calls) == [1, 3, 3]

    asyncio.run(main())


def test_saving_meaning_vectors_twice_is_harmless(run):  # noqa: F811
    async def body(library):
        doc = await ready(library, "notes.txt", b"Refunds take five working days.")
        rows = [(r[0], "Refunds take five working days.") for r in library._sql(
            "SELECT rowid FROM chunks WHERE doc_id = ?", (doc.id,))]  # fmt: skip
        assert await library._embed(rows) and await library._embed(rows)  # the idle catch-up
        assert (await library.search("refund"))[0]["document"] == "notes.txt"

    run(body)


def test_markdown_is_read_by_its_headings(run):  # noqa: F811
    async def body(library):
        notes = b"Intro line.\n\n# Installation\nRun setup.sh first.\n\n## Wi-Fi\nUse 5 GHz.\n"
        doc = await ready(library, "guide.md", notes)
        assert [p["location"] for p in library._parts(doc.id)] == [
            "start", "§ Installation", "§ Wi-Fi"]  # fmt: skip
        assert (await library.search("setup"))[0]["location"] == "§ Installation"

    run(body)


def flow_deck(joined: bool) -> bytes:
    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    boxes = []
    for n, name in enumerate(["Order placed", "Payment", "Shipped"]):
        box = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(1 + 3 * n), Inches(2), Inches(2), Inches(1)
        )
        box.text = name
        boxes.append(box)
    for a, b in zip(boxes, boxes[1:], strict=False):  # each box to the next
        arrow = slide.shapes.add_connector(
            MSO_CONNECTOR.STRAIGHT, Inches(1), Inches(4), Inches(3), Inches(4)
        )
        if joined:  # drawn by attaching the arrow to both boxes, as PowerPoint does
            arrow.begin_connect(a, 3)
            arrow.end_connect(b, 1)
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


def test_a_flowchart_is_read_from_the_deck_itself(run, tmp_path):  # noqa: F811
    from alfred.library import slide_diagrams

    path = tmp_path / "flow.pptx"
    path.write_bytes(flow_deck(joined=True))
    parts, loose, slides = slide_diagrams(path)
    assert parts[0].location == "slide 1, diagram" and slides == 1 and loose == []
    assert "- Order placed → Payment\n- Payment → Shipped" in parts[0].text

    async def body(library):  # exact, so its quotes check like any text
        doc = await ready(library, "flow.pptx", path.read_bytes())
        found = library.check([{"document": doc.name, "location": "slide 1, diagram",
                                "quote": "Payment → Shipped"}])  # fmt: skip
        assert found == [None]

    run(body)


def test_smartart_is_read_as_its_outline():
    from alfred.library import smartart_outline

    data = b"""<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
        xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><dgm:ptLst>
      <dgm:pt modelId="0" type="doc"/>
      <dgm:pt modelId="1"><dgm:t><a:p><a:r><a:t>CEO</a:t></a:r></a:p></dgm:t></dgm:pt>
      <dgm:pt modelId="2"><dgm:t><a:p><a:r><a:t>Finance</a:t></a:r></a:p></dgm:t></dgm:pt>
      <dgm:pt modelId="3"><dgm:t><a:p><a:r><a:t>Sales</a:t></a:r></a:p></dgm:t></dgm:pt>
      <dgm:pt modelId="9" type="pres"><dgm:t><a:p><a:r><a:t>x</a:t></a:r></a:p></dgm:t></dgm:pt>
    </dgm:ptLst><dgm:cxnLst>
      <dgm:cxn modelId="10" srcId="0" destId="1" srcOrd="0"/>
      <dgm:cxn modelId="11" srcId="1" destId="3" srcOrd="1"/>
      <dgm:cxn modelId="12" srcId="1" destId="2" srcOrd="0"/>
      <dgm:cxn modelId="13" type="presOf" srcId="1" destId="9"/>
    </dgm:cxnLst></dgm:dataModel>"""
    assert smartart_outline(data) == "- CEO\n  - Finance\n  - Sales"  # in order; layout left out


@pytest.mark.skipif(not library_module.libreoffice(), reason="LibreOffice isn't installed")
def test_loose_drawings_are_shown_to_claude_as_the_slide(tmp_path):
    seen = []

    async def vision(images):
        seen.extend(images)
        return ["Order placed → Payment → Shipped" for _ in images]

    async def main():
        library = Library(tmp_path / "library", vision, Limits(max_mb=5))
        library.start()
        doc = await ready(library, "loose.pptx", flow_deck(joined=False))
        locations = [p["location"] for p in library._parts(doc.id)]
        assert "slide 1, drawing" in locations and len(seen) == 1  # the slide, drawn whole
        assert library.read(doc.id, "slide 1, drawing")["pictures"]  # kept, to look at again
        await library.stop()

    asyncio.run(main())


def test_a_diagram_drawn_in_a_pdf_goes_to_claude_a_ruled_page_doesnt(tmp_path):
    seen = []

    async def vision(images):
        seen.extend(images)
        return ["- Plan → Build → Test → Ship" for _ in images]

    pdf = FPDF()
    pdf.add_page()  # a flowchart drawn with boxes and lines, no picture in it
    pdf.set_font("Helvetica", size=11)
    pdf.cell(0, 10, "Release process")
    for n, name in enumerate(["Plan", "Build", "Test", "Ship"]):
        pdf.rect(20, 40 + 50 * n, 60, 25)
        pdf.text(30, 55 + 50 * n, name)
        if n:
            pdf.line(50, 15 + 50 * n, 50, 40 + 50 * n)
    for n in range(4):
        pdf.rect(120, 40 + 50 * n, 60, 25)
    pdf.add_page()  # text with a rule under the heading: not a diagram
    pdf.cell(0, 10, "Terms")
    pdf.line(10, 20, 200, 20)
    pdf.set_y(30)
    pdf.multi_cell(0, 7, "Payment is due within thirty days of the invoice. " * 20)

    async def main():
        library = Library(tmp_path / "library", vision, Limits(max_mb=5))
        library.start()
        doc = await ready(library, "process.pdf", bytes(pdf.output()))
        locations = [p["location"] for p in library._parts(doc.id)]
        assert "page 1, picture" in locations and "page 2, picture" not in locations
        assert len(seen) == 1
        await library.stop()

    asyncio.run(main())
