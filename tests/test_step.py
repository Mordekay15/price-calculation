"""STEP models: the BOM, unfolding sheet parts, and the DXF tab's cards."""

import math

import pytest

pytest.importorskip("OCP")

from core.dxf import read_dxf                         # noqa: E402
from core.step import bends_on, flat_dxf, read_step, unfold  # noqa: E402
from tests.step_models import (                       # noqa: E402
    assembly_step, bent_part, creo_step, l_bracket, part_step, pin, plate)


def _size(flat):
    xs = [p[0] for p in flat.outline]
    ys = [p[1] for p in flat.outline]
    return round(max(xs) - min(xs), 3), round(max(ys) - min(ys), 3)


def test_the_bom_counts_parts_through_subassemblies():
    model = read_step(assembly_step(), "runko.step")
    assert not model.problems
    assert [(r.level, r.name, r.qty, r.total) for r in model.bom] == [
        (0, "Runko", 1, 1), (1, "Kulma", 2, 2), (1, "Pohja", 1, 1),
        (1, "Tappiryhmä", 2, 2), (2, "Tappi", 3, 6)]
    assert {p.name: p.qty for p in model.parts} == {"Kulma": 2, "Pohja": 1, "Tappi": 6}


def test_a_body_named_solid_takes_its_parts_name():
    """Creo names every body "SOLID": the part's own name is used instead, so
    cards don't all share the key "solid"."""
    model = read_step(creo_step(), "asm.step")
    assert sorted((p.name, p.qty) for p in model.parts) == [("100325", 1), ("PEM-M6", 4)]


def test_a_bad_file_has_a_problem_not_an_error():
    assert read_step(b"not a step file", "x.step").problems


def test_a_part_file_has_its_one_part():
    model = read_step(part_step(plate(), "100325"), "100325.step")
    assert [(p.name, p.qty) for p in model.parts] == [("100325", 1)]


def test_tiny_loose_bodies_in_a_part_are_left_out():
    """A form tool can leave 5 × 5 mm crumbs in a part's model: still one part."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS_Compound

    compound, builder = TopoDS_Compound(), BRep_Builder()
    builder.MakeCompound(compound)
    builder.Add(compound, plate())
    for x in (200.0, 220.0):
        builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 5.0, 5.0, 1.5).Shape())
    model = read_step(part_step(compound, "43422"), "43422.step")
    assert [(p.name, p.qty) for p in model.parts] == [("43422", 1)]


@pytest.mark.parametrize("segments", [
    [(40, 90), (30, 0)],                      # L
    [(40, 90), (30, 90), (40, 0)],            # U
    [(40, 90), (30, -90), (40, 0)],           # Z: bends both ways
    [(50, 45), (30, 0)],
    [(30, -135), (30, 0)],
])
def test_bent_parts_unfold_to_the_neutral_length(segments):
    t, r, k = 2.0, 2.0, 0.44
    flat = unfold(bent_part(segments, t, r), k)
    assert flat.is_sheet and flat.thickness_mm == pytest.approx(t)
    length = sum(L for L, _ in segments) + sum(abs(math.radians(a)) * (r + k * t)
                                              for _, a in segments)
    assert sorted(_size(flat)) == pytest.approx(sorted((40.0, length)), abs=1e-3)
    bends = [a for _, a in segments if a]
    assert [round(b.angle_deg) for b in flat.bends] == [abs(a) for a in bends]
    assert all(b.inner_radius_mm == pytest.approx(r) for b in flat.bends)
    if len(bends) == 2:   # same way: same side up; Z: opposite
        assert (flat.bends[0].up == flat.bends[1].up) == (bends[0] * bends[1] > 0)


def test_the_l_bracket_keeps_its_hole_and_k_changes_the_length():
    half = unfold(l_bracket(), 0.5)
    assert len(half.holes) == 1
    # Outside flanges 50 and 30, t 2, r 2: 46 + 26 + π/2 × (2 + 0.5 × 2).
    assert max(_size(half)) == pytest.approx(72 + math.pi / 2 * 3, abs=1e-3)
    assert max(_size(unfold(l_bracket(), 0.33))) < max(_size(half))
    # At K = 0.5 the flat area times the thickness is the volume.
    assert half.flat_area_mm2 * 2 == pytest.approx(half.volume_mm3, rel=0.005)


def test_a_flat_plate_has_no_bends_and_a_pin_is_not_sheet():
    flat = unfold(plate())
    assert flat.is_sheet and flat.thickness_mm == pytest.approx(3) and not flat.bends
    assert sorted(_size(flat)) == [60, 100]
    p = unfold(pin())
    assert not p.is_sheet and "ei levyosa" in p.reason


def test_the_flat_dxf_reads_back_with_its_bends_and_thickness():
    dxf = read_dxf(flat_dxf(unfold(bent_part([(40, 90), (30, 90), (40, 0)])), "U"), "U.step")
    report = dxf.part()
    assert not report.problems and report.bends == 2 and dxf.unit_label == "mm"
    assert any("t = 2 mm" in t for t in dxf.texts)


def test_an_assembly_becomes_cards_with_its_quantities():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("step_page.py", default_timeout=60).run()
    assert not at.exception
    # Pohja's DXF and its model part are one card, priced from the DXF (it
    # matches the unfolded blank). Tappi isn't sheet.
    names = [c[0] for c in at.session_state["cards"]]
    assert names == ["Pohja.dxf", "Kulma.step"]
    _, qty, bends, w, h = at.session_state["cards"][1]
    assert qty == 2 and bends == 1 and sorted((w, h)) == [40.0, 76.5]
    table = at.dataframe[0].value
    assert list(table["Kpl yhteensä"]) == [1, 2, 1, 2, 6]
    assert list(table["Tyyppi"]) == ["kokoonpano", "levyosa", "levyosa", "kokoonpano",
                                     "ei levyosa"]
    assert list(table["Kortti"])[1:3] == ["#2 · Kulma.step", "#1 · Pohja.dxf"]
    at.number_input(key="dxf_step_k_input").set_value(0.5).run()
    assert sorted(at.session_state["cards"][1][3:]) == [40.0, 76.7]


def test_a_parts_flat_drawing_model_and_pdf_make_one_card():
    # Kate's ITM-072558 folder: the flat DXF, the drawing as DXF (A4 frame and
    # title block naming ITM-072558.A), the model (body "Part1") and the PDF.
    from streamlit.testing.v1 import AppTest

    def page():
        import io
        from types import SimpleNamespace

        import ezdxf
        import streamlit as st
        from reportlab.pdfgen import canvas

        from core.step import flat_dxf, unfold
        from tests.conftest import dxf_bytes
        from tests.step_models import l_bracket, part_step
        from view.dxf_files import ingest, sync
        from view.dxf_tab import _render_part_cards

        flat = flat_dxf(unfold(l_bracket()), "ITM-072558")
        drawing = ezdxf.new()
        drawing.header["$INSUNITS"] = 4
        msp = drawing.modelspace()
        msp.add_lwpolyline([(0, 0), (297, 0), (297, 210), (0, 210)], close=True)
        msp.add_lwpolyline([(20, 20), (70, 20), (70, 60), (20, 60)], close=True)
        msp.add_text("ITM-072558.A", dxfattribs={"insert": (200, 10)})
        msp.add_text("DRW-023179.A", dxfattribs={"insert": (200, 5)})
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf)
        pdf.drawString(72, 720, "ITM-072558.A   DRW-023179.A")
        pdf.save()
        files = [("a", "DRW-023179_A.dxf", dxf_bytes(drawing)),
                 ("b", "DRW-023179_A.pdf", buf.getvalue()),
                 ("c", "ITM-072558_A.dxf", flat),
                 ("d", "ITM-072558_A.stp", part_step(l_bracket(), "Part1"))]
        if "done" not in st.session_state:
            st.session_state["done"] = True
            ingest([SimpleNamespace(file_id=i, name=n, getvalue=lambda d=d: d)
                    for i, n, d in files])
        parts = sync()
        _render_part_cards(parts, ["S235"], {("2", "S235 | 1000x2000"): 900.0}, False)
        st.session_state["cards"] = [(cid, d.name) for cid, d in parts]

    at = AppTest.from_function(page, default_timeout=60).run()
    assert not at.exception
    assert at.session_state["cards"] == [("a", "ITM-072558_A.dxf")]
    files = next(c.value for c in at.caption if c.value.startswith("Tiedostot:"))
    assert "✓ ITM-072558_A.dxf (vastaa STEP-mallin aihiota)" in files
    assert "DRW-023179_A.dxf (piirustuksen kehys" in files
    assert any("DRW-023179_A.pdf" in e.label for e in at.expander)
    at.selectbox(key="dxf_src_a").set_value("d|0:1:1:1/0").run()
    assert at.session_state["cards"] == [("a", "ITM-072558_A.stp")]


def test_a_card_takes_its_bends_from_the_model_and_its_material_from_the_pdf():
    # A flat DXF without bend lines, its model (one 90° bend, 2 mm) and the
    # drawing naming the material: the card is filled without a click.
    from streamlit.testing.v1 import AppTest

    def page():
        import io
        from types import SimpleNamespace

        import ezdxf
        import streamlit as st
        from reportlab.pdfgen import canvas

        from core.step import flat_dxf, unfold
        from tests.conftest import dxf_bytes
        from tests.step_models import l_bracket, part_step
        from view.dxf_files import auto_fill_materials, ingest, sync
        from view.dxf_tab import _render_part_cards

        flat = ezdxf.read(io.StringIO(flat_dxf(unfold(l_bracket()), "ITM-072558")
                                      .decode()))
        for e in list(flat.modelspace()):
            if e.dxf.layer.startswith("BEND"):
                flat.modelspace().delete_entity(e)
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf)
        pdf.drawString(72, 720, "ITM-072558.A")
        pdf.drawString(72, 700, "Material: S235JR")
        pdf.save()
        if "done" not in st.session_state:
            st.session_state["done"] = True
            ingest([SimpleNamespace(file_id=i, name=n, getvalue=lambda d=d: d) for i, n, d in
                    [("c", "ITM-072558_A.dxf", dxf_bytes(flat)),
                     ("d", "ITM-072558_A.stp", part_step(l_bracket(), "Part1")),
                     ("b", "ITM-072558_A.pdf", buf.getvalue())]])
        parts = sync()
        lookup = {("2", "S235 | 1000x2000"): 900.0, ("3", "S235 | 1000x2000"): 950.0}
        auto_fill_materials(parts, ["S235"], lookup)
        products = _render_part_cards(parts, ["S235"], lookup, False)
        st.session_state["cards"] = [(p["name"], p["material"], p["thickness"],
                                      p["report"].bends, p["work"].bends) for p in products]

    at = AppTest.from_function(page, default_timeout=60).run()
    assert not at.exception
    assert at.session_state["cards"] == [("ITM-072558_A.dxf", "S235", "2", 0, 1)]
    svg = next(m.value for m in at.markdown if m.value.startswith("<svg"))
    assert "#16a34a" in svg or "#ea580c" in svg          # the model's bend line
    assert any(c.value.startswith("Taivutuslinjat STEP-mallista") for c in at.caption)
    assert any(c.value.startswith("Materiaali täytetty automaattisesti:") and "STEP" in c.value
               for c in at.caption)


def test_the_models_bend_lines_are_placed_on_a_turned_mirrored_dxf():
    from core.step import Bend, FlatPart

    # An L-shaped blank with one bend across its long arm (no symmetry).
    outline = [(0, 0), (100, 0), (100, 30), (40, 30), (40, 60), (0, 60)]
    bend = Bend(90.0, 1.0, True, ((70.0, 0.0), (70.0, 30.0)))
    flat = FlatPart(True, 0.0, (100, 60, 2), 2.0, outline, [], [bend])

    def dxf(p):                      # the DXF: mirrored, turned 90° and moved
        x, y = -p[0], p[1]
        return -y + 500.0, x + 200.0

    placed = bends_on(flat, [[dxf(p) for p in outline]])
    assert placed is not None and len(placed) == 1
    (a, b), up = placed[0]
    assert {tuple(round(c, 3) for c in a), tuple(round(c, 3) for c in b)} == {
        tuple(round(c, 3) for c in dxf(p)) for p in bend.line}
    assert up is False               # seen from the other side
    assert bends_on(flat, [[(0, 0), (300, 0), (300, 10), (0, 10)]]) is None
    # The real unfolding on its own flat DXF: the line lands where it was.
    real = unfold(l_bracket())
    (line, _), = bends_on(real, [real.outline, *real.holes])
    assert {tuple(round(c, 3) for c in p) for p in line} == {
        tuple(round(c, 3) for c in p) for p in real.bends[0].line}


def test_an_assembly_step_becomes_an_assembly_of_its_cards():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace

        import ezdxf
        import streamlit as st

        from tests.conftest import dxf_bytes
        from tests.step_models import assembly_step
        from view import assemblies
        from view.dxf_files import ingest, source_cards, sync
        from view.dxf_tab import _render_part_cards

        if "dropped" not in st.session_state:
            doc = ezdxf.new()
            doc.header["$INSUNITS"] = 4
            doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)],
                                            close=True)
            data, model = dxf_bytes(doc), assembly_step()
            ingest([SimpleNamespace(file_id="d1", name="Pohja.dxf", getvalue=lambda: data),
                    SimpleNamespace(file_id="s1", name="runko.step", getvalue=lambda: model)])
            st.session_state["dropped"] = True
        parts = sync()
        assemblies.sync_from_models()
        _render_part_cards(parts, ["S235"], {("2", "S235 | 1000x2000"): 900.0}, False,
                           st.session_state.get("screen", "BOM & Tiedostot"))
        st.session_state["asm"] = [(a.name, dict(a.members),
                                    [(b.name, b.qty, b.fit_eur) for b in a.bought])
                                   for a in assemblies.current(source_cards())]
        st.session_state["parts"] = [(fid, d.name) for fid, d in parts]

    at = AppTest.from_function(page, default_timeout=60).run()
    assert not at.exception
    cards = dict((name, fid) for fid, name in at.session_state["parts"])
    # 2 × Kulma and Pohja are sheet cards; 6 × Tappi is a bought part.
    assert at.session_state["asm"] == [
        ("runko", {cards["Pohja.dxf"]: 1, cards["Kulma.step"]: 2}, [("Tappi", 6, 0.20)])]
    assert any(c.value == "Osat: #1 Pohja.dxf × 1 · #2 Kulma.step × 2" for c in at.caption)
    assert not any("Kokoonpanossa" in c.value for c in at.caption)
    # Its cards are drawn inside it, not among the loose parts.
    assert any("Kaikki osat ovat kokoonpanoissa." in c.value for c in at.caption)
    assert [m.value.split(" · ")[1] for m in at.markdown if m.value.startswith("**#")] == [
        "Pohja.dxf", "Kulma.step"]
    # Its cards are nested for the largest batch (10) of assemblies.
    qtys = [n for n in at.number_input if n.label == "Määrä (kpl)"]
    assert [n.value for n in qtys] == [10, 20] and all(n.disabled for n in qtys)
    # The assembly prices fasteners and welding, so its cards don't.
    at.session_state["screen"] = "Hinnoittelu"     # the work is drawn there
    at.run()
    keys = {n.key for n in at.number_input}
    assert f"work_bends_{cards['Kulma.step']}" in keys
    assert not {f"work_{k}_{cards['Kulma.step']}"
                for k in ("fasteners", "fastener_eur", "insert_eur", "weld")} & keys


def test_a_kept_models_bodies_are_read_again_when_needed():
    from streamlit.testing.v1 import AppTest

    def page():
        import streamlit as st

        from tests.step_models import assembly_step
        from view import attachments, bom

        if "done" not in st.session_state:
            data = assembly_step()
            attachments.add_doc("m", "runko.step", data, load=lambda: data)
            bom.add_model("m", "runko.step", data)
            st.session_state["done"] = True
        parts = bom.models()["m"]["model"].parts
        st.session_state["dropped"] = all(p.shape is None for p in parts)
        st.session_state["again"] = sorted(bom.shapes("m")) == sorted(p.key for p in parts)
        bom.refold(0.5)
        st.session_state["flats"] = len(bom.models()["m"]["flats"])

    at = AppTest.from_function(page, default_timeout=60).run()
    assert not at.exception
    assert at.session_state["dropped"] and at.session_state["again"]
    assert at.session_state["flats"] == 3
