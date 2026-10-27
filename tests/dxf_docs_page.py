"""A page for test_dxf_tab: two part cards, a PDF drawing for the first, and
its material filled in from it (S235 2 mm and S355MC 3 mm priced)."""

import io
from types import SimpleNamespace

import ezdxf
import streamlit as st
from reportlab.pdfgen import canvas

from tests.conftest import dxf_bytes
from view.dxf_files import STORE, auto_fill_materials, ingest
from view.dxf_tab import _render_part_cards

doc = ezdxf.new()
doc.header["$INSUNITS"] = 4
doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
data = dxf_bytes(doc)
buf = io.BytesIO()
pdf = canvas.Canvas(buf)
pdf.drawString(72, 720, "Material: S355MC  t = 3 mm")
pdf.save()
if not st.session_state.get(STORE):
    ingest([SimpleNamespace(file_id="f0", name="100325.dxf", getvalue=lambda: data),
            SimpleNamespace(file_id="f1", name="200400.dxf", getvalue=lambda: data),
            SimpleNamespace(file_id="d0", name="100325-revA.pdf",
                            getvalue=lambda: buf.getvalue())])
parts = list(st.session_state[STORE].items())
materials = ["S235", "S355MC"]
lookup = {("2", "S235 | 1000x2000"): 900.0, ("3", "S355MC | 1000x2000"): 950.0}
auto_fill_materials(parts, materials, lookup)
products = _render_part_cards(parts, materials, lookup, False)
st.session_state["materials"] = [(p["material"], p["thickness"]) for p in products]
