"""The DXF reader: what is the part, and when is a file refused."""

import math

import pytest

from core.dxf import read_dxf
from core.geometry import min_area_angle, point_in_polygon, rotate_translate
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


# ── CAD export noise (from real Inventor flat patterns) ──────────────────────

def test_zero_length_stubs_in_the_outline_are_chained_through(drawing):
    # Inventor leaves 0.004 mm lines between outline segments; each one used to
    # be read as a closed loop and, the outline broken there, picked as a
    # zero-area outline.
    doc, msp = drawing()
    for a, b in [((0, 0), (300, 0)), ((300, 0), (300, 200)), ((300, 200), (150.0264, 200)),
                 ((150.0264, 200), (150.022, 200)), ((150.022, 200), (0, 200)), ((0, 200), (0, 0))]:
        msp.add_line(a, b)
    report = part_of(doc)
    assert report.problems == []
    assert math.isclose(report.outline.area_mm2, 60_000, rel_tol=1e-6)


def test_ends_a_hair_apart_across_a_grid_border_still_meet(drawing):
    # 1578.023 and 1578.047 are 0.024 mm apart but round to different 0.05 mm cells.
    doc, msp = drawing()
    rect(msp, 1500, 0, 200, 100)
    for a, b in [((1578.023, 57.267), (1578.023, 59.767)), ((1578.023, 59.767), (1556.036, 59.765)),
                 ((1556.036, 59.765), (1556.036, 57.265)), ((1556.036, 57.265), (1578.047, 57.267))]:
        msp.add_line(a, b)
    report = part_of(doc)
    assert report.problems == []
    assert len(report.holes) == 1


def test_a_hole_edge_overshooting_its_corner_is_not_a_self_crossing(drawing):
    # The bottom edge runs 0.023 mm past the right edge: snapped onto the corner.
    doc, msp = drawing()
    rect(msp, 0, 0, 400, 100)
    for a, b in [((305.13070048986242, 57.2667633604561317), (305.1305034145744912, 59.7667633527499618)),
                 ((305.1305034145744912, 59.7667633527499618), (283.1429760329447731, 59.7650273349572032)),
                 ((283.1429760329447731, 59.7650273349572032), (283.143173088236324, 57.2650273426273628)),
                 ((305.153761142550195, 57.2667764654256288), (283.1345494911570881, 57.2650404462652602))]:
        msp.add_line(a, b)
    report = part_of(doc)
    assert report.problems == []
    assert len(report.holes) == 1


def test_collinear_edges_of_a_long_outline_do_not_cross(drawing):
    # Two top edges on the same line, a picometre apart in y, 2 m long.
    doc, msp = drawing()
    msp.add_lwpolyline([(2251.25, 206.2746068139259), (2251.25, 0.96), (0.09, 0.96),
                        (0.09, 206.2746068139328), (287.13, 206.2746068139281),
                        (288.63, 207.77), (295.63, 207.77),
                        (297.13, 206.2746068139281)], close=True)
    assert part_of(doc).problems == []


def test_a_nearly_straight_tiny_arc_does_not_cross_itself(drawing):
    # An Inventor bulge of 1e-5 over a 0.065 mm chord flattens into four
    # 0.016 mm segments on almost one line; non-neighbours touch that line
    # but cannot cross (ITM-072574).
    doc, msp = drawing()
    msp.add_lwpolyline([(1358.1120566320319, 41.6609813251880, 1.21656677019e-05),
                        (1358.1109486234379, 41.5956124246452, 0),
                        (1378.1138565258350, 41.5956124246448, 0),
                        (1378.1138565258350, 300, 0),
                        (1358.1120566320319, 300, 0)], format="xyb", close=True)
    assert part_of(doc).problems == []


def test_a_part_drawn_diagonally_is_laid_along_its_length(drawing):
    # A 2900 × 80 bar with a hole, drawn at 45°: 2108 × 2108 as drawn
    doc, msp = drawing()
    bar = rotate_translate([(0, 0), (2900, 0), (2900, 80), (0, 80)], 45, (0, 0))
    msp.add_lwpolyline(bar, close=True)
    msp.add_circle(rotate_translate([(1450, 40)], 45, (0, 0))[0], 20)
    report = part_of(doc)
    assert report.problems == []
    assert (round(report.outline.width_mm), round(report.outline.height_mm)) == (2900, 80)
    assert point_in_polygon(report.holes[0].points[0], report.outline.points)


def test_a_straight_part_is_left_as_drawn(drawing):
    doc, msp = drawing()
    rect(msp, 10, 20, 80, 300)          # standing: Sparrow turns it a quarter itself
    report = part_of(doc)
    assert report.outline.points[0] == (10, 20)
    assert (report.outline.width_mm, report.outline.height_mm) == (80, 300)


def test_min_area_angle_lays_the_long_side_horizontal():
    for angle in (0, 30, 45, 120):
        bar = rotate_translate([(0, 0), (500, 0), (500, 40), (0, 40)], angle, (0, 0))
        turned = rotate_translate(bar, min_area_angle(bar), (0, 0))
        xs, ys = [p[0] for p in turned], [p[1] for p in turned]
        assert math.isclose(max(xs) - min(xs), 500, abs_tol=1e-6)
        assert math.isclose(max(ys) - min(ys), 40, abs_tol=1e-6)


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
