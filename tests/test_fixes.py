"""What the user fixes by hand: the part chosen on a drawing sheet, and the
lines inside a part marked as not cut."""

from core.fixes import pick_view, set_not_cut, unclosed_lines
from core.items import score_dxf
from core.readers import read_dxf_file
from tests.conftest import dxf_bytes, rect
from tests.test_dxf import _flat, _sheet


def _two_views(drawing):
    doc, msp = drawing()
    _sheet(msp)
    _flat(msp)
    rect(msp, 240, 100, 150, 40)        # another view that could be the part
    msp.add_circle((260, 120), 5)
    return read_dxf_file(dxf_bytes(doc), "f1", "DRW-1.dxf")


def test_the_view_chosen_on_the_sheet_becomes_the_files_part(drawing):
    result = _two_views(drawing)
    unread = result.shapes[0]
    assert unread.problems and not unread.outline and result.is_drawing
    assert [tuple(round(v) for v in o.size_mm) for o in unread.views.options] \
        == [(150, 40), (100, 25), (80, 25)]                  # the last: the side view
    assert len(unread.views.sheet) > 3 and len(unread.views.outlines) == 3

    shape = pick_view(result, 1)
    assert result.shapes == [shape] and shape.views.picked == 1
    assert shape.problems == [] and len(shape.holes) == 2 and shape.origin == "f1"
    assert tuple(round(v) for v in shape.size_mm) == (100, 25)
    assert score_dxf(shape, [], None).valid

    # Another view, then none: as read.
    assert tuple(round(v) for v in pick_view(result, 0).size_mm) == (150, 40)
    assert pick_view(result, None) is unread and unread.views.picked is None
    assert pick_view(result, 7) is unread                    # no such view


def test_a_file_with_its_part_has_no_views_to_choose(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    result = read_dxf_file(dxf_bytes(doc), "f1", "plate.dxf")
    assert result.shapes[0].views is None and pick_view(result, 0) is None


def test_lines_marked_as_not_cut_are_drawn_not_cut_and_the_part_is_priced(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    msp.add_lwpolyline([(50, 50), (80, 50), (80, 90)])       # two open lines:
    msp.add_lwpolyline([(150, 50), (180, 50), (180, 90)])    # marks, or holes with a gap
    shape = read_dxf_file(dxf_bytes(doc), "f1", "plate.dxf").shapes[0]
    assert "2 viivaa" in shape.problems[0] and len(unclosed_lines(shape)) == 2
    cut = shape.cut_length_mm

    set_not_cut(shape, {0})
    assert "1 viiva " in shape.problems[0] and len(shape.open_cuts) == 1
    set_not_cut(shape, {0, 1})
    assert shape.problems == [] and shape.open_cuts == [] and len(shape.markings) == 2
    assert round(cut - shape.cut_length_mm) == 140           # the two lines' length
    assert score_dxf(shape, [], None).valid

    set_not_cut(shape, set())                                # taken back
    assert "2 viivaa" in shape.problems[0] and len(shape.open_cuts) == 2
    assert shape.markings == [] and round(shape.cut_length_mm) == round(cut)


def _folded(drawing):
    """A plate whose outline runs back across its own edge near a corner,
    leaving a small stray loop outside the plate."""
    doc, msp = drawing()
    msp.add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200), (0, 150), (-10, 160),
                        (10, 170)], close=True)
    msp.add_circle((150, 100), 20)
    return read_dxf_file(dxf_bytes(doc), "f1", "plate.dxf").shapes[0]


def test_the_outer_edge_of_an_outline_that_crosses_itself_can_be_used(drawing):
    from core.dxf import SELF_CROSSING
    from core.fixes import can_use_outer_edge, crossings, use_outer_edge

    shape = _folded(drawing)
    assert shape.problems == [SELF_CROSSING] and can_use_outer_edge(shape)
    drawn = shape.outline
    assert len(crossings(drawn)) == 1 and crossings(shape.holes[0]) == []

    assert use_outer_edge(shape)
    assert shape.problems == [] and crossings(shape.outline) == []
    assert tuple(round(v) for v in shape.size_mm) == (300, 200)   # the plate; the stray loop is left out
    assert len(shape.holes) == 1 and score_dxf(shape, [], None).valid
    assert can_use_outer_edge(shape)                         # still there to take back

    use_outer_edge(shape, False)
    assert shape.outline is drawn and shape.problems == [SELF_CROSSING]


def test_a_part_that_doesnt_cross_itself_is_not_offered_its_outer_edge(drawing):
    from core.fixes import can_use_outer_edge, use_outer_edge

    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    shape = read_dxf_file(dxf_bytes(doc), "f1", "plate.dxf").shapes[0]
    assert not can_use_outer_edge(shape) and not use_outer_edge(shape, False)
    assert shape.drawn_rings is None
