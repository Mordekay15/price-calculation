"""A page for test_assemblies: four part cards: the first one's DXF calls out
press nuts, the last one's PDF KALEI nuts and PEMs. Each card's work is kept
in "work"."""

import ezdxf
import streamlit as st

from core.model import InputFile
from tests.conftest import dxf_bytes
from tests.fastener_pdf import drawing_pdf
from view import attachments
from view.dxf_files import STORE, add_drawing, sync, sync_assemblies
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
    for fid, note, w, name in [("f0", "M5-PURISTEMUTTERIT X4KPL", 100, "levy.dxf"),
                               ("f1", None, 120, "kulma.dxf"), ("f2", None, 140, "kansi.dxf"),
                               ("f3", None, 160, "kotelo.dxf")]:
        add_drawing(InputFile(fid, name, "dxf", None), part(note, w))
    # its fasteners on its PDF
    attachments.add_doc(InputFile("d3", "kotelo.pdf", "pdf", None), drawing_pdf())
sync()
sync_assemblies()
products = _render_part_cards(list(st.session_state[STORE].items()), ["S235"],
                              {("2", "S235 | 1000x2000"): 900.0})
st.session_state["ids"] = [p["id"] for p in products]
st.session_state["work"] = {p["id"]: p["work"] for p in products}
