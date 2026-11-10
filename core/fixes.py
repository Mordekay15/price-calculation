"""What the user fixes by hand on a drawing the reader couldn't read on its
own, pure (no Streamlit):

- **Choosing the part** on a drawing sheet whose views the reader couldn't
  tell apart (``FlatShape.views``): ``pick_view`` makes the chosen view's
  part the file's shape, exactly as if the reader had picked it.
- **Marking lines as not cut** when lines inside the part close into no
  outline (``FlatShape.unclosed``): ``set_not_cut`` moves them to the part's
  markings (drawn, never cut) and takes the problem off once none is left.

- **Using the outer edge** of an outline that crosses itself (edges drawn
  over each other, a flange drawn folded onto the plate): ``use_outer_edge``
  replaces each such ring with the edge round everything it encloses.

Each can be taken back, and each changes the ``ReadResult`` in place, so the
cards, the nesting and the price read the fixed shape."""

from __future__ import annotations

from dataclasses import replace

from core.dxf import (
    SELF_CROSSING,
    _segments_cross,
    crosses_itself,
    is_slit,
    is_unclosed_problem,
    unclosed_problem,
)
from core.items import looks_like_drawing
from core.model import FlatShape, ReadResult


def pick_view(result: ReadResult, k: int | None) -> FlatShape | None:
    """Make view ``k`` of the file's drawing sheet its part (None: no view,
    as read). Returns the file's shape after, None when it has no views."""
    at = next((i for i, s in enumerate(result.shapes) if s.views is not None), None)
    if at is None:
        return None
    choice = result.shapes[at].views
    if k is not None and not 0 <= k < len(choice.options):
        k = None
    choice.picked = k
    shape = choice.unread if k is None else replace(choice.options[k], views=choice)
    result.shapes[at] = shape
    result.is_drawing = looks_like_drawing(shape)
    return shape


def unclosed_lines(shape: FlatShape) -> list[list]:
    """The lines inside the part that close into no outline, in a fixed
    order (the ones marked as not cut too)."""
    if shape.unclosed is None:
        shape.unclosed = [line for line in shape.open_cuts if not is_slit(line)]
    return shape.unclosed


def set_not_cut(shape: FlatShape, marked) -> None:
    """Say which of ``unclosed_lines`` (by index) are drawing marks, not
    cuts: they become markings, the rest stay cuts that block pricing."""
    lines = unclosed_lines(shape)
    marked = frozenset(i for i in marked if 0 <= i < len(lines))
    for i, line in enumerate(lines):
        cut = any(c is line for c in shape.open_cuts)
        if i in marked and cut:
            shape.open_cuts = [c for c in shape.open_cuts if c is not line]
            shape.markings = [*shape.markings, line]
        elif i not in marked and not cut:
            shape.markings = [m for m in shape.markings if m is not line]
            shape.open_cuts = [*shape.open_cuts, line]
    shape.not_cut = marked
    left = len(lines) - len(marked)
    shape.problems = ([p for p in shape.problems if not is_unclosed_problem(p)]
                      + ([unclosed_problem(left)] if left else []))


def crossings(ring: list) -> list[tuple[float, float]]:
    """Where a closed ring crosses itself (none for a ring that doesn't)."""
    if not crosses_itself(ring):
        return []
    n = len(ring)
    out = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        for j in range(i + 2, n):
            c, d = ring[j], ring[(j + 1) % n]
            if (i == 0 and j == n - 1) or not _segments_cross(a, b, c, d):
                continue
            den = (b[0] - a[0]) * (d[1] - c[1]) - (b[1] - a[1]) * (d[0] - c[0])
            t = ((c[0] - a[0]) * (d[1] - c[1]) - (c[1] - a[1]) * (d[0] - c[0])) / den
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
    return out


def outer_edge(ring: list) -> list | None:
    """The edge round what a self-crossing ring encloses: its loops joined
    into one outline (the biggest piece, when a stray loop only touches the
    rest at a point). None when it encloses nothing (or shapely is missing)."""
    try:
        from shapely.geometry import LineString
        from shapely.ops import polygonize, unary_union
    except ImportError:
        return None
    lines = unary_union(LineString([*ring, ring[0]]))        # cut at every crossing
    area = unary_union(list(polygonize(lines)))
    if area.is_empty:
        return None
    if area.geom_type != "Polygon":                          # loops that only touch
        area = max(area.geoms, key=lambda g: g.area)
    return [(x, y) for x, y in area.exterior.coords[:-1]]


def can_use_outer_edge(shape: FlatShape) -> bool:
    """Is this fix on offer: the shape is (or was, before the fix) refused
    for an outline that crosses itself?"""
    return shape.drawn_rings is not None or SELF_CROSSING in shape.problems


def use_outer_edge(shape: FlatShape, on: bool = True) -> bool:
    """Replace each ring that crosses itself with its outer edge (``on``
    False: as drawn again). Returns whether every crossing is gone."""
    if shape.drawn_rings is None:
        if not on:
            return False
        shape.drawn_rings = (shape.outline, shape.holes)
    outline, holes = shape.drawn_rings
    if on:
        fixed = [(outer_edge(r) or r) if crosses_itself(r) else r for r in (outline, *holes)]
        shape.outline, shape.holes = fixed[0], fixed[1:]
    else:
        shape.outline, shape.holes, shape.drawn_rings = outline, holes, None
    left = any(crosses_itself(r) for r in (shape.outline, *shape.holes))
    shape.problems = ([p for p in shape.problems if p != SELF_CROSSING]
                      + ([SELF_CROSSING] if left else []))
    return not left
