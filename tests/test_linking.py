"""Linking PDFs and STEP files to parts by part number."""

from core.linking import link_files, part_key, revision, unlinked


def test_revisions_are_ignored():
    assert part_key("100326-revA.dxf") == part_key("100326.pdf") == "100326"
    assert part_key("100325_A.PDF") == "100325"
    assert part_key("20250922-6.dxf") == "20250922-6"   # a number, not a revision
    assert revision("100326-revA.pdf") == "revA" and revision("100326.pdf") == ""


def test_files_go_to_their_parts():
    parts = {"a": "bracket_L.dxf", "b": "bracket_R.dxf", "c": "100326-revA.dxf"}
    files = {"x": "bracket_L.pdf", "y": "100326.pdf", "z": "other.pdf"}
    links = link_files(parts, files)
    assert links == {"a": ["x"], "c": ["y"]}    # bracket_L.pdf stays off bracket_R
    assert unlinked(links, files) == ["z"]
