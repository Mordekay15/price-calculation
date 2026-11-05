"""Is a drawing PDF an assembly drawing (``core.pdf_assembly``), and the
assembly a PDF on no card makes (``view.assemblies``)."""

import io

from reportlab.pdfgen import canvas

from core.pdf_assembly import ASSEMBLY, MAYBE, PART, PdfPage, analyse, read_pages

W, H = 1190, 842     # A3 landscape, points


def _sheet(texts=(), circles=()) -> bytes:
    """A drawing sheet: ``texts`` as ``(x, top, text)``, ``circles`` as
    ``(x, top, r)`` (y measured down, as pdfplumber does)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setFont("Helvetica", 8)
    for x, top, s in texts:
        c.drawString(x, H - top - 8, s)
    for x, top, r in circles:
        c.circle(x, H - top, r)
    c.save()
    return buf.getvalue()


def _row(top, *cells, xs=(800, 830, 870, 960, 1080)):
    return [(x, top, s) for x, s in zip(xs, cells) if s]


def _iso_assembly() -> bytes:
    """An ISO parts list on top of the title block, numbered upwards, and
    balloons pointing at the parts."""
    texts = [
        *_row(640, "3", "4", "ISO 4762 M5x12", "Screw", "8.8"),
        *_row(652, "2", "1", "100326", "Top plate", "S235"),
        *_row(664, "1", "2", "100325", "Side plate", "S235"),
        *_row(676, "Pos", "Qty", "Part No", "Description", "Material"),
        (800, 700, "Title: FRAME"), (800, 712, "ASSEMBLY DRAWING"),
        (1000, 712, "Drawing No 100200"), (800, 724, "Material: see parts list"),
        (300, 200, "1"), (420, 260, "2"), (360, 330, "3"),
    ]
    circles = [(302.5, 204, 9), (422.5, 264, 9), (362.5, 334, 9)]
    return _sheet(texts, circles)


def test_an_iso_assembly_drawing_is_an_assembly_with_its_parts_list():
    found = analyse(read_pages(_iso_assembly()), "100200")
    assert found.verdict == ASSEMBLY
    assert [(r.item, r.qty, r.number, r.bought) for r in found.rows] == [
        (1, 2, "100325", False), (2, 1, "100326", False), (3, 4, "", True)]
    assert found.balloons == [1, 2, 3]
    keys = {c.key for c in found.cues}
    assert {"bom", "bom_rows", "balloons", "balloons_match", "title", "see_list"} <= keys
    assert "osaluettelo +3" in found.why()


def test_a_parts_list_under_its_header_is_read_downwards():
    texts = [*_row(100, "ITEM NO.", "PART NUMBER", "DESCRIPTION", "QTY.",
                   xs=(800, 860, 960, 1080)),
             *_row(112, "1", "ITM-064555.A", "Plate", "2", xs=(800, 860, 960, 1080)),
             *_row(124, "2", "ITM-064556.A", "Cover", "1", xs=(800, 860, 960, 1080)),
             *_row(136, "3", "S-M5-2ZI", "PEM nut", "6", xs=(800, 860, 960, 1080))]
    found = analyse(read_pages(_sheet(texts)), "asm-070000")
    assert [(r.number, r.qty, r.bought) for r in found.rows] == [
        ("itm-064555", 2, False), ("itm-064556", 1, False), ("", 6, True)]
    assert found.verdict == ASSEMBLY


def test_a_part_drawing_is_a_part():
    texts = [(800, 700, "DETAIL DRAWING"), (800, 712, "Material: S235 2 mm"),
             (300, 300, "UP 90° R1"), (800, 724, "UNMARKED BEND RADII R=0,6MM"),
             (1000, 712, "100325")]
    found = analyse(read_pages(_sheet(texts)), "100325")
    assert found.verdict == PART
    assert found.rows == [] and found.score < 0


def test_a_sheet_calling_out_part_numbers_by_name_is_an_assembly():
    """ABB's way (RFQ 580-07): no parts list, the part numbers on their own
    lines by the parts, ASSEMBLY DRAWING in the title block."""
    texts = [(300, 200, "3AXD50000026889"), (500, 300, "3AXD50001016005"),
             (900, 760, "ASSEMBLY DRAWING"), (1000, 790, "3AXD50001139896"),
             (60, 790, "3AXD50000040570 (ASSEM) E.3+ © ABB Oy.")]
    found = analyse(read_pages(_sheet(texts)), "3axd50001139896")
    assert found.verdict == ASSEMBLY
    assert found.called_out == ["3axd50000026889", "3axd50001016005"]


def test_where_a_part_is_used_is_no_sign_of_an_assembly():
    texts = [(60, 790, "3AXD50000040570 (ASSEM) E.3+"), (900, 760, "DETAIL DRAWING")]
    assert analyse(read_pages(_sheet(texts)), "3axd50000035889").verdict == PART


def test_a_title_word_alone_is_only_maybe():
    found = analyse(read_pages(_sheet([(800, 700, "ASSEMBLY DRAWING")])), "1")
    assert found.verdict == MAYBE


def test_a_paper_size_is_no_weld():
    found = analyse(read_pages(_sheet([(900, 760, "A3"), (500, 300, "a4 4x50 (100)")])), "1")
    assert [c.key for c in found.cues] == ["weld"]
    found = analyse(read_pages(_sheet([(900, 760, "A3")])), "1")
    assert found.cues == []


def test_a_page_without_text_is_noted_as_scanned():
    page = PdfPage(1, W, H, words=[], paths=400)
    found = analyse([page], "1")
    assert found.scanned == [1]
    assert [c.key for c in found.cues] == ["scanned"]


def test_the_pdf_reader_gives_the_verdict_and_the_list_rows():
    from core.readers import read_pdf_file

    r = read_pdf_file(_iso_assembly(), "p1", "100200.pdf")
    [verdict] = r.of("is_assembly")
    assert verdict.value["yes"] and not verdict.value["maybe"]
    assert verdict.value["by"] == ["title", "parts_list"]
    assert {(f.value["part_key"], f.value["kpl"]) for f in r.of("bom_qty")} == {
        ("100325", 2), ("100326", 1)}
    assert {f.value for f in r.of("called_out")} >= {"100325", "100326"}
    # Its bought row is a fastener with the row's qty, not "SCREW" × 1.
    assert [f.value for f in r.of("fastener")] == [{"name": "ISO 4762 M5X12 SCREW", "kpl": 4}]


def test_an_assembly_pdf_on_no_card_makes_an_assembly():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace

        import streamlit as st

        from core.items import Choice
        from core.readers import read_pdf_file
        from tests.test_pdf_assembly import _iso_assembly
        from view import assemblies

        plate = SimpleNamespace(names={"p": "100325.dxf"},
                                scores={"p": Choice(100, "puhdas aihio")})
        said = read_pdf_file(_iso_assembly(), "d", "100200.pdf").facts
        assemblies.sync_from_drawings({"p": plate}, lambda cid: [],
                                      {"d": ("100200.pdf", said)})
        a = st.session_state[assemblies.ASSEMBLIES]["100200"]
        st.session_state["got"] = (a.drawing_pdf, a.drawn, a.awaited,
                                   assemblies.pdf_drawings())
        # The PDF gone (or now on a card): the assembly goes with it.
        assemblies.sync_from_drawings({"p": plate}, lambda cid: [], {})
        st.session_state["left"] = list(st.session_state[assemblies.ASSEMBLIES])

    at = AppTest.from_function(page).run()
    assert not at.exception
    assert at.session_state["got"] == ("d", {"p": 2}, ["100326"], {"100200": "d"})
    assert at.session_state["left"] == []


def test_a_pdf_that_is_only_maybe_makes_no_assembly():
    from streamlit.testing.v1 import AppTest

    def page():
        import streamlit as st

        from core.model import Fact
        from view import assemblies

        said = [Fact("is_assembly", {"yes": False, "maybe": True, "by": ["title"],
                                    "score": 2, "why": ""}, "d", ""),
                Fact("called_out", "100325", "d", "100325")]
        assemblies.sync_from_drawings({}, lambda cid: [], {"d": ("100200.pdf", said)})
        st.session_state["left"] = list(st.session_state.get(assemblies.ASSEMBLIES, {}))

    at = AppTest.from_function(page).run()
    assert not at.exception
    assert at.session_state["left"] == []


def test_on_the_page_an_assembly_pdf_dropped_with_its_parts_is_an_assembly():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("pdf_assembly_page.py", default_timeout=60).run()
    assert not at.exception
    assert at.session_state["got"] == (True, [2], ["100326"])
    assert any("Kokoonpanot (1)" in m.value for m in at.markdown)
