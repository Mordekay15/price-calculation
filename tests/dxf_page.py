"""A page for test_dxf_tab: ``st.session_state["n"]`` part cards (S235, 2 mm
priced) and the run row's nesting angle."""

from types import SimpleNamespace

import ezdxf
import streamlit as st

from core.dxf import read_dxf
from tests.conftest import dxf_bytes
from view.dxf_tab import (
    _ANGLE_MODE,
    _PER_PART,
    _FILES,
    _STORE,
    _ingest,
    _render_angle_controls,
    _render_part_cards,
    _sync,
)

doc = ezdxf.new()
doc.header["$INSUNITS"] = 4
doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
data = dxf_bytes(doc)
if "seeded" not in st.session_state:
    st.session_state["seeded"] = True
    st.session_state[_FILES] = {f"f{i}": read_dxf(data, f"p{i}.dxf")
                                for i in range(st.session_state.get("n", 1))}
    _sync()
store = st.session_state.get(_STORE, {})
# "add": drop that many more files
names = [f"p{len(store) + i}.dxf" for i in range(st.session_state.pop("add", 0))]
_ingest([SimpleNamespace(file_id=f"f{n[1:-4]}", name=n, getvalue=lambda: data) for n in names])
parts = list(st.session_state.get(_STORE, {}).items())
per_part = len(parts) > 1 and st.session_state.get(_ANGLE_MODE) == _PER_PART
products = _render_part_cards(parts, ["S235"], {("2", "S235 | 1000x2000"): 900.0}, per_part)
shared = _render_angle_controls(len(parts))
st.session_state["angles"] = [shared if shared is not None else p["angles"] for p in products]
st.session_state["materials"] = [(p["material"], p["thickness"]) for p in products]
st.session_state["qtys"] = [p["qty"] for p in products]
