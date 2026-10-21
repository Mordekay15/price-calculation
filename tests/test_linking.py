"""Linking PDFs and STEP files to parts by part number."""

from core.linking import link_files, part_key


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
