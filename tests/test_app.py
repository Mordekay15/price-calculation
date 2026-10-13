"""Smoke test: the whole page renders without an exception."""

from streamlit.testing.v1 import AppTest


def test_app_renders_both_tabs():
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Hintalaskuri", "DXF-nestaus"]


def test_a_missing_solver_is_said_on_the_page(monkeypatch):
    # Without scipy the fewest-sheets and mixed-size plans can't be made; the
    # page must not quietly show only the old ones.
    import view.common

    def page():
        from view.common import render_groups
        render_groups({}, lambda key, products: None)

    monkeypatch.setattr(view.common, "have_solver", lambda: False)
    at = AppTest.from_function(page).run()
    assert any("scipy" in w.value for w in at.warning)
    monkeypatch.setattr(view.common, "have_solver", lambda: True)
    assert not AppTest.from_function(page).run().warning
