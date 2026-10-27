"""Linking PDFs and STEP files to parts by part number."""

from core.linking import callouts, link_files, part_key


def test_revisions_are_ignored():
    assert part_key("100326-revA.dxf") == part_key("100326.pdf") == "100326"
    assert part_key("100325_A.PDF") == "100325"
    assert part_key("20250922-6.dxf") == "20250922-6"   # a number, not a revision


def test_files_go_to_their_parts():
    parts = {"a": "bracket_L.dxf", "b": "bracket_R.dxf", "c": "100326-revA.dxf"}
    files = {"x": "bracket_L.pdf", "y": "100326.pdf", "z": "other.pdf"}
    links = link_files(parts, files)
    assert links == {"a": ["x"], "c": ["y"]}    # bracket_L.pdf stays off bracket_R


def test_a_drawing_named_otherwise_links_by_the_part_number_in_its_text():
    from core.linking import link_by_text

    parts = {"a": "ITM-072558_A.dxf", "b": "ITM-072560_A.dxf", "c": "12.dxf"}
    texts = {
        "pdf1": "Item / Drawing ID and Revision:\nITM-072558.A\nDRW-023179.A",
        "pdf2": "ITM 072560 rev A",                     # spaced
        "pdf3": "Assembly: ITM-072558, ITM-072560",     # names two: by hand
        "pdf4": "ITM-0725581 and 12 mm",                # longer number; "12" too short
    }
    assert link_by_text(parts, texts) == {"a": ["pdf1"], "b": ["pdf2"]}


def test_a_page_links_a_pdf_by_its_text():
    from streamlit.testing.v1 import AppTest

    def page():
        import io

        import streamlit as st
        from reportlab.pdfgen import canvas

        from view import attachments

        buf = io.BytesIO()
        pdf = canvas.Canvas(buf)
        pdf.drawString(72, 720, "Item / Drawing ID and Revision: ITM-072558.A")
        pdf.drawString(72, 700, "DRW-023179.A")
        pdf.save()
        if not attachments.docs():
            attachments.add_doc("d1", "DRW-023179.A.pdf", buf.getvalue())
        st.session_state["links"] = attachments.links(
            {"f1": "ITM-072558_A.dxf", "f2": "ITM-072560_A.dxf"})

    at = AppTest.from_function(page).run()
    assert not at.exception
    assert at.session_state["links"] == {"f1": ["d1"]}


def test_a_drawing_links_to_the_numbered_pieces_of_its_part():
    parts = {"a": "3aua0000098622_1.dxf", "b": "3aua0000098622_2.dxf", "c": "100325.dxf"}
    files = {"pdf": "3AUA0000098622.pdf", "stp": "3AUA0000098622.stp", "x": "100325_2.pdf"}
    assert link_files(parts, files) == {"a": ["pdf", "stp"], "b": ["pdf", "stp"]}


def test_a_drawing_names_the_numbered_pieces_in_its_text():
    from core.linking import link_by_text

    parts = {"a": "3aua0000098622_1.dxf", "b": "3aua0000098622_2.dxf"}
    texts = {"pdf": "Doc. No. Sheet 1\n3AUA0000098622\nDMS Number 3AXD10000037291"}
    assert link_by_text(parts, texts) == {"a": ["pdf"], "b": ["pdf"]}


def test_an_assembly_drawing_calls_out_its_items():
    # As an ABB assembly drawing's PDF reads: the callouts stand alone (or
    # after a scale and a dimension); the title block's numbers are among words.
    text = ("3AXD50001139896 A.2+ We reserve all rights\n"
            "3AXD50000040570 (ASSEM) E.3+ © ABB Oy.\n"
            "1:2 12 3AXD50000018805\n5x25 mm\nL=225 mm\n"
            "3AXD50000026889\n5x12 mm\n3AXD50001016005\n"
            "ASSEMBLY KIT: 3AXD50001107246\n"
            "DMS Number 3AXD10002100550 Weight kg Total 1\n328\n26-Feb-24")
    assert callouts(text) == ["3axd50000018805", "3axd50000026889", "3axd50001016005"]


def test_a_bom_row_gives_its_parts_quantity():
    from core.linking import bom_quantities

    bom = ("Pos ID.Rev Thumbnail Name and Description EN Qty Unit QRCode\n"
           "PH-002620.1 031 - Subcontractor Laser cutting Stencil 1\n"
           "1 ITM-064555.A Plate Installation guide, sofa, Plate_02 2 pcs\n"
           "2 ITM-064556.A Plate, sofa 4 kpl")
    drawing = "Material: S355MC\nEverything is possible ITM-064555.A\nDRW-021780.A"
    cards = {"c1": ["DRW-021780_A.dwg", "ITM-064555_A.dxf"], "c2": ["ITM-064556_A.dxf"],
             "c3": ["100325.dxf"]}
    assert bom_quantities(cards, {"PH-002620_1_BOM.pdf": bom, "DRW-021780_A.pdf": drawing}) == {
        "c1": (2, ["PH-002620_1_BOM.pdf"]), "c2": (4, ["PH-002620_1_BOM.pdf"])}
    # Two BOMs that both need the part: their quantities add up.
    other = "10 ITM-064555 Plate 3 pcs"
    assert bom_quantities(cards, {"a.pdf": bom, "b.pdf": other})["c1"] == (5, ["a.pdf", "b.pdf"])
