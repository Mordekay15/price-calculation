"""A page for test_pdf_assembly: a plate's DXF and an assembly drawing PDF
that no card's name matches, dropped together, and the part cards drawn."""

from types import SimpleNamespace

import ezdxf
import streamlit as st

from tests.conftest import dxf_bytes
from tests.test_pdf_assembly import _iso_assembly
from view import assemblies
from view.dxf_files import cards, ingest, sync_assemblies
from view.dxf_tab import _render_part_cards

doc = ezdxf.new()
doc.header["$INSUNITS"] = 4
doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
plate, drawing = dxf_bytes(doc), _iso_assembly()
ingest([SimpleNamespace(file_id="f1", name="100325.dxf", getvalue=lambda: plate),
        SimpleNamespace(file_id="f2", name="100200.pdf", getvalue=lambda: drawing)])
sync_assemblies()
parts = list(cards().items())
_render_part_cards(parts, ["S235"], {("2", "S235 | 1000x2000"): 900.0}, False)
a = st.session_state[assemblies.ASSEMBLIES].get("100200")
st.session_state["got"] = None if a is None else (
    a.drawing_pdf is not None, sorted(a.parts.values()), a.awaited)
