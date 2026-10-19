"""STEP models: the BOM, unfolding sheet parts, and the DXF tab's cards."""

import math

import pytest

pytest.importorskip("OCP")

from core.dxf import read_dxf                         # noqa: E402
from core.step import flat_dxf, read_step, unfold     # noqa: E402
from tests.step_models import (                       # noqa: E402
    assembly_step, bent_part, l_bracket, part_step, pin, plate)


def _size(flat):
    xs = [p[0] for p in flat.outline]
    ys = [p[1] for p in flat.outline]
    return round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)


def test_the_bom_counts_parts_through_subassemblies():
    model = read_step(assembly_step(), "runko.step")
    assert not model.problems
    assert [(r.level, r.name, r.qty, r.total) for r in model.bom] == [
        (0, "Runko", 1, 1), (1, "Kulma", 2, 2), (1, "Pohja", 1, 1),
        (1, "Tappiryhmä", 2, 2), (2, "Tappi", 3, 6)]
    assert {p.name: p.qty for p in model.parts} == {"Kulma": 2, "Pohja": 1, "Tappi": 6}


def test_a_bad_file_has_a_problem_not_an_error():
    assert read_step(b"not a step file", "x.step").problems


def test_a_part_file_has_its_one_part():
    model = read_step(part_step(plate(), "100325"), "100325.step")
    assert [(p.name, p.qty) for p in model.parts] == [("100325", 1)]


@pytest.mark.parametrize("segments", [
    [(40, 90), (30, 0)],                      # L
    [(40, 90), (30, 90), (40, 0)],            # U
    [(40, 90), (30, -90), (40, 0)],           # Z: bends both ways
    [(50, 45), (30, 0)],
    [(30, -135), (30, 0)],
])
def test_bent_parts_unfold_to_the_neutral_length(segments):
    t, r, k = 2.0, 2.0, 0.44
    flat = unfold(bent_part(segments, t, r), k)
    assert flat.is_sheet and flat.thickness_mm == pytest.approx(t)
    length = sum(L for L, _ in segments) + sum(abs(math.radians(a)) * (r + k * t)
                                              for _, a in segments)
    assert sorted(_size(flat)) == pytest.approx(sorted((40.0, length)), abs=1e-3)
    bends = [a for _, a in segments if a]
    assert [round(b.angle_deg) for b in flat.bends] == [abs(a) for a in bends]
    assert all(b.inner_radius_mm == pytest.approx(r) for b in flat.bends)
    if len(bends) == 2:   # same way: same side up; Z: opposite
        assert (flat.bends[0].up == flat.bends[1].up) == (bends[0] * bends[1] > 0)


def test_the_l_bracket_keeps_its_hole_and_k_changes_the_length():
    half = unfold(l_bracket(), 0.5)
    assert len(half.holes) == 1
    # Outside flanges 50 and 30, t 2, r 2: 46 + 26 + π/2 × (2 + 0.5 × 2).
    assert max(_size(half)) == pytest.approx(72 + math.pi / 2 * 3, abs=1e-3)
    assert max(_size(unfold(l_bracket(), 0.33))) < max(_size(half))
    # At K = 0.5 the flat area times the thickness is the volume.
    assert half.flat_area_mm2 * 2 == pytest.approx(half.volume_mm3, rel=0.005)


def test_a_flat_plate_has_no_bends_and_a_pin_is_not_sheet():
    flat = unfold(plate())
    assert flat.is_sheet and flat.thickness_mm == pytest.approx(3) and not flat.bends
    assert sorted(_size(flat)) == [60, 100]
    p = unfold(pin())
    assert not p.is_sheet and "ei levyosa" in p.reason


def test_the_flat_dxf_reads_back_with_its_bends_and_thickness():
    dxf = read_dxf(flat_dxf(unfold(bent_part([(40, 90), (30, 90), (40, 0)])), "U"), "U.step")
    report = dxf.part()
    assert not report.problems and report.bends == 2 and dxf.unit_label == "mm"
    assert any("t = 2 mm" in t for t in dxf.texts)


def test_an_assembly_becomes_cards_with_its_quantities():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("step_page.py", default_timeout=60).run()
    assert not at.exception
    # Pohja has a DXF: no card from the model. Tappi isn't sheet.
    names = [c[0] for c in at.session_state["cards"]]
    assert names == ["Pohja.dxf", "Kulma.step"]
    _, qty, bends, w, h = at.session_state["cards"][1]
    assert qty == 2 and bends == 1 and sorted((w, h)) == [40.0, 76.5]
    table = at.dataframe[0].value
    assert list(table["Kpl yhteensä"]) == [1, 2, 1, 2, 6]
    assert list(table["Tyyppi"]) == ["kokoonpano", "levyosa", "levyosa", "kokoonpano",
                                     "ei levyosa"]
    assert table["Kortti"][2].startswith("DXF: ")
    at.number_input(key="dxf_step_k_input").set_value(0.5).run()
    assert sorted(at.session_state["cards"][1][3:]) == [40.0, 76.7]
