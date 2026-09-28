"""Smoke test: the whole page renders without an exception."""

from streamlit.testing.v1 import AppTest


def test_app_renders_both_tabs():
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Hintalaskuri", "DXF-nestaus"]


def test_a_combination_of_sizes_renders():
    def page():
        from core.sparrow import sparrow_options
        from tests.test_sparrow import fake_solver, square
        from view.drawing import draw_sparrow_layout
        from view.sheet_usage import render_group

        parts = [square(1000, quantity=3)]
        lookup = {("2", "S235 | 1000x2000"): 800.0, ("2", "S235 | 1000x1000"): 1000.0}
        result = sparrow_options(lookup, "S235", "2", 2.0, parts, run_fn=fake_solver)
        render_group("S235", "2", 2.0, result, margin_pct=0.0, key="combo",
                     draw_layout=lambda o: draw_sparrow_layout(o, parts, "combo"))

    at = AppTest.from_function(page, default_timeout=60).run()
    assert not at.exception
    assert any("1.0 × 2.0 m ×1 + 1.0 × 1.0 m ×1" in s.value for s in at.success)
