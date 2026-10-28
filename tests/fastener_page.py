"""A page for test_assemblies: three part cards, the first one's drawing
calling out press nuts."""

import ezdxf
import streamlit as st

from core.dxf import read_dxf
from tests.conftest import dxf_bytes
from view.dxf_files import FILES, STORE, sync, sync_assemblies
from view.dxf_tab import _render_part_cards


def part(note: str | None, w: float) -> bytes:
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (w, 0), (w, 50), (0, 50)], close=True)
    if note:
        msp.add_text(note, dxfattribs={"insert": (0, 80), "height": 5})
    return dxf_bytes(doc)


if "seeded" not in st.session_state:
    st.session_state["seeded"] = True
    st.session_state[FILES] = {
        "f0": read_dxf(part("M5-PURISTEMUTTERIT X4KPL", 100), "levy.dxf"),
        "f1": read_dxf(part(None, 120), "kulma.dxf"),
        "f2": read_dxf(part(None, 140), "kansi.dxf")}
sync()
sync_assemblies()
products = _render_part_cards(list(st.session_state[STORE].items()), ["S235"],
                              {("2", "S235 | 1000x2000"): 900.0})
st.session_state["ids"] = [p["id"] for p in products]
