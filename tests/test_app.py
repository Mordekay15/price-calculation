"""Smoke test: the whole page renders without an exception."""

from streamlit.testing.v1 import AppTest


def test_app_renders_both_tabs():
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Hintalaskuri", "DXF-nestaus"]
