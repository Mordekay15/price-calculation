"""A page for test_dxf_tab: ``st.session_state["n"]`` part cards (S235, 2 mm
priced) and the run row's nesting angle."""

from types import SimpleNamespace

import ezdxf
import streamlit as st

from core.model import InputFile
from tests.conftest import dxf_bytes
from view.dxf_files import add_drawing, cards, ingest, sync
from view.dxf_nesting import ANGLE_MODE, _render_angle_controls
from view.dxf_tab import _render_part_cards

doc = ezdxf.new()
doc.header["$INSUNITS"] = 4
doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
data = dxf_bytes(doc)
if "seeded" not in st.session_state:
    st.session_state["seeded"] = True
    for i in range(st.session_state.get("n", 1)):
        add_drawing(InputFile(f"f{i}", f"p{i}.dxf", "dxf", None), data)
    sync()
store = cards()
# "add": drop that many more files
names = [f"p{len(store) + i}.dxf" for i in range(st.session_state.pop("add", 0))]
ingest([SimpleNamespace(file_id=f"f{n[1:-4]}", name=n, getvalue=lambda: data) for n in names])
parts = list(cards().items())
per_part = len(parts) > 1 and bool(st.session_state.get(ANGLE_MODE))
products = _render_part_cards(parts, ["S235"], {("2", "S235 | 1000x2000"): 900.0}, per_part)
shared = _render_angle_controls(len(parts), products)
st.session_state["angles"] = [shared if shared is not None else p.angles for p in products]
st.session_state["materials"] = [(p.material, p.thickness) for p in products]
st.session_state["qtys"] = [p.qty for p in products]
