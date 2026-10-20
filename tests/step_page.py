"""A page for test_step: drop an assembly STEP (2 × Kulma, Pohja, 6 × Tappi)
together with a DXF of Pohja, then draw the BOM and the cards."""

from types import SimpleNamespace

import ezdxf
import streamlit as st

from tests.conftest import dxf_bytes
from tests.step_models import assembly_step
from view import bom
from view.dxf_tab import _ITEMS, _ingest, _render_part_cards, _resync, _sync

if "dropped" not in st.session_state:
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    data, model = dxf_bytes(doc), assembly_step()
    _ingest([SimpleNamespace(file_id="d1", name="Pohja.dxf", getvalue=lambda: data),
             SimpleNamespace(file_id="s1", name="runko.step", getvalue=lambda: model)])
    st.session_state["dropped"] = True
parts = _sync()
labels = {fid: f"#{i + 1} · {d.name}" for i, (fid, d) in enumerate(parts)}
bom.render_bom({sid: labels[cid] for cid, item in st.session_state[_ITEMS].items()
                for sid in item.members}, _resync)
products = _render_part_cards(parts, ["S235"], {("2", "S235 | 1000x2000"): 900.0}, False)
st.session_state["cards"] = [(p["name"], p["qty"], p["report"].bends,
                              round(p["width"], 1), round(p["height"], 1)) for p in products]
