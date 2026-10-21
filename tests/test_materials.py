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
