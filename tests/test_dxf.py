"""The DXF reader: what is the part, and when is a file refused."""

import math

import pytest

from core.dxf import read_dxf
from tests.conftest import dxf_bytes, rect


def part_of(doc, layers=None):
    return read_dxf(dxf_bytes(doc), "part.dxf").part(layers)


# ── Clean drawings ────────────────────────────────────────────────────────────

def test_plate_with_a_hole(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    msp.add_circle((150, 100), 40)
    report = part_of(doc)
    assert report.problems == []
    assert (round(report.outline.width_mm, 3), round(report.outline.height_mm, 3)) == (300, 200)
    assert len(report.holes) == 1
    # curves are flattened to segments within 0.2 mm, so a Ø80 hole comes out
    # about 0.5 % smaller than πr²
    assert math.isclose(report.holes[0].area_mm2, math.pi * 40 ** 2, rel_tol=1e-2)


def test_outline_drawn_as_separate_lines_is_chained(drawing):
    doc, msp = drawing()
    for a, b in [((0, 0), (400, 0)), ((400, 0), (400, 100)), ((400, 100), (100, 100)),
                 ((100, 100), (100, 300)), ((100, 300), (0, 300)), ((0, 300), (0, 0))]:
        msp.add_line(a, b)
    report = part_of(doc)
    assert report.problems == []
    assert math.isclose(report.outline.area_mm2, 60_000)


@pytest.mark.parametrize("layer", ["FRAME", "INFO", "Title_Block", "DIM", "BEND_UP"])
def test_furniture_layers_are_left_out_by_name(drawing, layer):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    doc.layers.add(layer)
    rect(msp, -50, -50, 400, 300, layer=layer)
    dxf = read_dxf(dxf_bytes(doc), "part.dxf")
    assert layer not in dxf.suggested_layers()
    report = dxf.part()
    assert report.problems == [] and round(report.outline.width_mm) == 300


def test_the_layer_choice_can_be_changed(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200, layer="CUT")
    rect(msp, 400, 0, 100, 100, layer="EXTRA")
    dxf = read_dxf(dxf_bytes(doc), "part.dxf")
    assert round(dxf.part({"EXTRA"}).outline.width_mm) == 100


def test_detail_views_outside_the_part_are_dropped(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    msp.add_circle((450, 100), 30)          # a detail view next to the part
    msp.add_line((400, 300), (500, 300))    # a stray line
    report = part_of(doc)
    assert report.problems == [] and len(report.dropped) == 2
    assert round(report.outline.width_mm) == 300


def test_dashed_and_bend_lines_are_drawn_not_cut(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    msp.add_line((0, 100), (300, 100), dxfattribs={"linetype": "DASHED"})
    doc.layers.add("BEND")
    msp.add_line((150, 0), (150, 200), dxfattribs={"layer": "BEND"})
    report = part_of(doc)
    assert report.problems == [] and len(report.reference_lines) == 2


def test_part_inside_a_block_is_read(drawing):
    doc, msp = drawing()
    block = doc.blocks.new("PART")
    rect(block, 0, 0, 300, 200)
    msp.add_blockref("PART", (10, 10))
    report = part_of(doc)
    assert report.problems == [] and round(report.outline.width_mm) == 300


def test_countersink_and_thread_symbol_are_marks_of_their_hole(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    msp.add_circle((100, 100), 5.25)              # countersink Ø10.5 …
    msp.add_circle((100, 100), 2.75)              # … around the Ø5.5 through hole
    msp.add_circle((200, 100), 1.65)              # M4 tap hole Ø3.3
    msp.add_arc((200, 100), 2.0, 255, 195)        # ISO thread symbol: ¾ circle Ø4
    report = part_of(doc)
    assert report.problems == []
    assert len(report.holes) == 2                  # Ø10.5 and Ø3.3
    assert len(report.reference_lines) == 2        # inner Ø5.5 + the thread arc


def test_thread_drawn_as_circle_plus_symbol_as_in_pro_engineer(drawing):
    # Pro/ENGINEER draws an M4 thread as a full Ø4 circle, the ¾-circle symbol
    # of the same Ø4, and the Ø3.3 tap hole inside — all concentric. After
    # flattening, the arc can come out a hair smaller than the circle.
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    msp.add_circle((200, 100), 2.0)
    msp.add_arc((200, 100), 1.995, 255, 195)
    msp.add_circle((200, 100), 1.65)
    report = part_of(doc)
    assert report.problems == []
    assert len(report.holes) == 1 and len(report.reference_lines) == 2


# ── Units ─────────────────────────────────────────────────────────────────────

def test_unit_from_the_header_is_applied(drawing):
    doc, msp = drawing(units=1)                    # inches
    rect(msp, 0, 0, 10, 5)
    report = part_of(doc)
    assert math.isclose(report.outline.width_mm, 254) and report.unit_label == "tuuma"


def test_unit_from_a_text_label(drawing):
    doc, msp = drawing(units=0)
    rect(msp, 0, 0, 12, 8)
    msp.add_text('Un="inch"')
    dxf = read_dxf(dxf_bytes(doc), "part.dxf")
    assert dxf.problems == [] and dxf.unit_note
    assert math.isclose(dxf.part().outline.width_mm, 12 * 25.4)


def test_unit_from_an_iso_sheet_size(drawing):
    doc, msp = drawing(units=0)
    doc.layers.add("FORMAT")
    rect(msp, 0, 0, 840, 594, layer="FORMAT")      # an A1 drawing sheet
    rect(msp, 100, 100, 300, 200)
    dxf = read_dxf(dxf_bytes(doc), "part.dxf")
    assert dxf.problems == [] and "A1" in dxf.unit_note
    assert round(dxf.part().outline.width_mm) == 300


def test_no_unit_anywhere_is_guessed_as_mm(drawing):
    doc, msp = drawing(units=0)
    rect(msp, 0, 0, 300, 200)
    dxf = read_dxf(dxf_bytes(doc), "part.dxf")
    assert dxf.unit_guessed and dxf.unit_label == "mm" and dxf.problems == []
    assert round(dxf.part().outline.width_mm) == 300


def test_no_unit_in_an_imperial_drawing_is_guessed_as_inch(drawing):
    doc, msp = drawing(units=0)
    doc.header["$MEASUREMENT"] = 0
    rect(msp, 0, 0, 10, 5)
    dxf = read_dxf(dxf_bytes(doc), "part.dxf")
    assert dxf.unit_guessed and dxf.unit_label == "tuuma"
    assert math.isclose(dxf.part().outline.width_mm, 254)


# ── Refused, with a reason ────────────────────────────────────────────────────

def refused(doc) -> str:
    report = part_of(doc)
    assert report.problems, "expected the part to be refused"
    return " ".join(report.problems)




def test_gap_in_the_outline(drawing):
    doc, msp = drawing()
    for a, b in [((0, 0), (300, 0)), ((300, 0), (300, 200)), ((300, 200), (0, 200)), ((0, 200), (0, 2))]:
        msp.add_line(a, b)
    assert "suljettua ääriviivaa" in refused(doc)


def test_gap_in_a_hole(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    for a, b in [((100, 50), (200, 50)), ((200, 50), (200, 150)), ((200, 150), (100, 150)), ((100, 150), (100, 55))]:
        msp.add_line(a, b)
    assert "ei sulkeudu" in refused(doc)


def test_self_crossing_outline(drawing):
    doc, msp = drawing()
    msp.add_lwpolyline([(0, 0), (300, 200), (300, 0), (0, 200)], close=True)
    assert "leikkaa itseään" in refused(doc)


def test_not_a_dxf_file():
    report = read_dxf(b"this is not a dxf file", "x.dxf").part()
    assert "ei voitu lukea" in " ".join(report.problems)
