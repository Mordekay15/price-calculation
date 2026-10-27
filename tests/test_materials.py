"""Reading materials from drawings and proposing catalogue materials."""

from core.materials import (
    HIGH,
    Hint,
    extract_hints,
    load_aliases,
    match_thickness,
    propose,
    save_aliases,
)

MATERIALS = ["S355MC", "S235JR", "RST 2B", "RST HARJATTU", "HST 2B", "ALUMIINI 5754", "DC01"]
THICK = {"S355MC": ["2", "3"], "S235JR": ["2"], "RST 2B": ["1.5", "2.5"],
         "RST HARJATTU": ["2.5"], "HST 2B": ["2"], "ALUMIINI 5754": ["3"], "DC01": ["1"]}


def _t(material):
    return THICK.get(material, [])


def _one(hints, note=(), aliases=None):
    return propose({"p": ("100325.dxf", list(hints))}, list(note), MATERIALS, _t, aliases)[0]


def test_extract_grades_and_thickness():
    (h,) = extract_hints("Material: Stainless steel 304, t = 2.5 mm", "PDF")
    assert (h.family, h.grade, h.thickness_mm) == ("RST", "304", 2.5)
    (h,) = extract_hints("DXFt tulee s355 2mm", "RFQ")
    assert (h.family, h.grade, h.thickness_mm) == ("S", "S355", 2.0)
    assert extract_hints("EN AW-5754 3 mm", "x")[0].family == "ALUMIINI"
    assert extract_hints("1.4404", "x")[0].family == "HST"
    assert extract_hints("DC01", "x")[0].family == "KY"


def test_rfq_note_fills_parts_without_own_hints():
    p = _one([], note=extract_hints("DXFt tulee s355 2mm", "Tarjouspyyntö"))
    assert (p.material, p.thickness, p.conflict) == ("S355MC", "2", "")


def test_pdf_stainless_maps_to_2b():
    p = _one(extract_hints("Stainless steel 304, t = 2.5 mm", "100325.pdf"))
    assert (p.material, p.thickness) == ("RST 2B", "2.5")
    assert p.evidence and p.evidence[0].source == "100325.pdf"


def test_disagreeing_thickness_is_a_conflict():
    hints = (extract_hints("RST 304 t=2.5", "PDF") + extract_hints("t=2.54", "DXF"))
    p = _one(hints)
    assert "paksuus" in p.conflict


def test_own_files_beat_the_note():
    p = _one(extract_hints("S235JR 2 mm", "PDF"), note=extract_hints("S355 3 mm", "RFQ"))
    assert (p.material, p.thickness, p.conflict) == ("S235JR", "2", "")


def test_missing_thickness_is_flagged():
    p = _one(extract_hints("S355 4 mm", "PDF"))
    assert p.material == "S355MC" and p.thickness is None
    assert "ei ole hinnastossa" in p.conflict


def test_alias_wins(tmp_path):
    path = tmp_path / "aliases.json"
    save_aliases({"  Customer  Steel X ": {"material": "DC01", "thickness": "1"}}, path)
    aliases = load_aliases(path)
    assert "customer steel x" in aliases
    p = _one([Hint("PDF", "Customer steel X")], aliases=aliases)
    assert (p.material, p.thickness, p.confidence, p.via) == ("DC01", "1", HIGH, "alias")


def test_match_thickness_tolerance():
    assert match_thickness(2.04, ["1.5", "2"]) == "2"
    assert match_thickness(2.1, ["1.5", "2"]) is None
    assert match_thickness(None, ["2"]) is None


def test_hot_dip_galvanised_with_its_coating():
    text = "Material:HDG STEEL SHEET 1,5MM\nDX51D+Z275-M-A-C\nEN 10346\n(min size 22x22 mm)"
    hints = extract_hints(text, "3AUA0000098622.pdf")
    assert [(h.family, h.grade, h.thickness_mm) for h in hints] == [
        ("SINK", "", 1.5), ("SINK", "Z275", None)]
    for materials in (["Kuumasinkitty Z275", "Sähkösinkitty ZE", "Kuumasinkitty DX51D+Z100MAC",
                       "DC01"], ["KU-SI DX51D+Z100", "KU-SI DX51D+Z275", "DC01"]):
        p = propose({"p": ("x.dxf", hints)}, [], materials, lambda m: ["1", "1.5"])[0]
        assert "Z275" in p.material and p.thickness == "1.5" and p.confidence == HIGH
        assert not p.conflict


def test_a_title_block_read_without_spaces_still_gives_the_material():
    # DRW-021780_A.pdf: pdfplumber runs the title block's words together.
    hints = extract_hints("Material: Dimensions: Weight(kg):\nS355MCEN10149-2 PL3x520x400 NA", "PDF")
    assert [(h.family, h.grade, h.thickness_mm) for h in hints] == [("S", "S355", 3.0)]
    p = _one(hints)
    assert (p.material, p.thickness, p.conflict) == ("S355MC", "3", "")


def _w(text, x0, top, x1=None, h=6):
    return {"text": text, "x0": x0, "x1": x1 or x0 + 6 * len(text), "top": top, "bottom": top + h}


def test_the_material_field_is_read_under_or_beside_its_label():
    from core.materials import material_field

    # DRW-021780_A.pdf: labels on one row, values on the row under them.
    words = [_w("Material:", 626, 665, 649), _w("Dimensions:", 853, 665, 886),
             _w("Weight(kg):", 1052, 663, 1084),
             _w("S355MCEN10149-2", 626, 683, 721, 9), _w("PL3x520x400", 853, 682, 927, 9),
             _w("NA", 1052, 684, 1068), _w("Designedby:", 626, 697, 661)]
    assert material_field(words) == "S355MCEN10149-2"
    # The value right after the label, up to the next label.
    words = [_w("Materiaali:", 10, 100, 60), _w("S235JR", 70, 100), _w("Paino:", 200, 100)]
    assert material_field(words) == "S235JR"
    assert material_field([_w("Note:", 10, 10), _w("S235", 50, 10)]) == ""


def test_the_material_field_beats_the_other_lines():
    from dataclasses import replace

    note = extract_hints("kiinnitysruuvi A2 / S235 kiinnike", "PDF")
    field = [replace(h, field=True) for h in extract_hints("S355MCEN10149-2", "PDF, Material")]
    p = _one(note + field + extract_hints("PL 3 x 520 x 400", "PDF"))
    assert (p.material, p.thickness, p.conflict) == ("S355MC", "3", "")


def test_two_grades_of_one_kind_are_a_conflict():
    p = _one(extract_hints("S235JR\nS355MC t=3", "PDF"))
    assert "S235" in p.conflict and "S355" in p.conflict


def test_a_material_run_into_its_label_is_read():
    from core.materials import material_field

    # ABB's 3AXD drawings: "Material:HDG STEEL SHEET 1,5MM".
    words = [_w("Material:HDG", 10, 100), _w("STEEL", 90, 100), _w("SHEET", 130, 100),
             _w("1,5MM", 170, 100)]
    assert material_field(words) == "HDG STEEL SHEET 1,5MM"
