"""What the user fixes by hand on a drawing the reader couldn't read on its
own, pure (no Streamlit):

- **Choosing the part** on a drawing sheet whose views the reader couldn't
  tell apart (``FlatShape.views``): ``pick_view`` makes the chosen view's
  part the file's shape, exactly as if the reader had picked it.
- **Marking lines as not cut** when lines inside the part close into no
  outline (``FlatShape.unclosed``): ``set_not_cut`` moves them to the part's
  markings (drawn, never cut) and takes the problem off once none is left.

Both can be taken back, and both change the ``ReadResult`` in place, so the
cards, the nesting and the price read the fixed shape."""

from __future__ import annotations

from dataclasses import replace

from core.dxf import is_slit, is_unclosed_problem, unclosed_problem
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
