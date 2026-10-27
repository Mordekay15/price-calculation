"""Undo for the Poista buttons (``view.undo``)."""

from streamlit.testing.v1 import AppTest


def test_undo_puts_back_what_a_removal_changed_and_keeps_later_changes():
    def page():
        import streamlit as st

        from view.undo import render_undo, undoable

        ss = st.session_state
        if "cards" not in ss:
            ss.cards = {"a": 1, "b": 2, "c": 3}
            ss.hidden = {"x"}
            ss.mat_b = "S235"

        def remove(cid):
            ss.cards.pop(cid)
            ss.hidden.add(cid)
            ss.pop(f"mat_{cid}", None)

        st.button("Poista", key="dxf_del_b", on_click=undoable(remove, "b"), args=("b",))
        render_undo()

    at = AppTest.from_function(page).run()
    at.button(key="dxf_del_b").click().run()
    assert list(at.session_state.cards) == ["a", "c"] and "mat_b" not in at.session_state
    at.session_state.cards["d"] = 4                       # changed after the removal
    at.button(key="ui_undo_do").click().run()
    assert list(at.session_state.cards) == ["a", "b", "c", "d"]   # back in its place
    assert at.session_state.hidden == {"x"} and at.session_state.mat_b == "S235"
    assert not any(b.key == "ui_undo_do" for b in at.button)
