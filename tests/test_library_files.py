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
        assert library.scans_left() == 999  # it counts towards the daily limit
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
    Image.new("RGB", (3000, 100), "white").save(tiff, "TIFF")

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
        assert image.status == "ready" and seen[-1] == (".png", 2000)  # a PNG, shrunk
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
        await reading.wait()  # the picture is being read...
        assert (await library.search("quarterly sales"))[0]["document"] == "review.pptx"
        assert library.get(doc.id).status == "processing"  # ...and the text is searchable
        await ready(library, "review.pptx", out.getvalue())
        assert library.vectors and not library._embedding  # meaning search caught up
        await library.stop()

    asyncio.run(main())
