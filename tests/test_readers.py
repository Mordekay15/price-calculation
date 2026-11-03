"""One reader per file type (core.readers): each returns its shapes and facts,
and the model's derived values (core.model)."""

import io

import ezdxf
import pytest

from core import dwg, step
from core.model import BoughtPart, FlatShape, InputFile, Value, file_kind
from core.readers import (
    read_dwg_file,
    read_dxf_file,
    read_file,
    read_pdf_file,
    read_step_file,
)
from tests.conftest import dxf_bytes, rect

needs_ocp = pytest.mark.skipif(not step.available(), reason="cadquery-ocp not installed")


def _plate(texts=()):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    rect(msp, 0, 0, 300, 200)
    msp.add_circle((50, 50), 10)
    for i, t in enumerate(texts):
        msp.add_text(t, height=3).set_placement((400, 20 * i))
    return dxf_bytes(doc)


def _pdf(lines) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(842, 595))
    for i, line in enumerate(lines):
        c.drawString(40, 550 - 14 * i, line)
    c.save()
    return buf.getvalue()


def test_file_kind_by_extension_and_others_left_out():
    assert [file_kind(n) for n in ("a.DXF", "b.stp", "c.STEP", "d.pdf", "e.zip", "f.prt", "g")] \
        == ["dxf", "step", "step", "pdf", "zip", None, None]


def test_a_dxf_gives_its_part_and_the_facts_in_its_texts():
    r = read_dxf_file(_plate(["S355MC t=3", "M5-PURISTEMUTTERIT X4KPL"]), "f1", "ITM-064555_A.dxf")
    (shape,) = r.shapes
    assert shape.priceable and shape.origin == "f1"
    assert [round(v) for v in sorted(shape.size_mm)] == [200, 300]
    assert len(shape.holes) == 1
    assert shape.net_area_mm2 == pytest.approx(300 * 200 - 3.1416 * 100, rel=1e-3)
    assert shape.cut_length_mm == pytest.approx(1000 + 2 * 3.1416 * 10, rel=1e-3)
    assert r.of("part_number")[0].value == "itm-064555"
    assert any(f.value["grade"] == "S355" for f in r.of("material"))
    assert [f.value for f in r.of("thickness")] == [3.0]
    assert r.of("fastener")[0].value == {"name": "M5-PURISTEMUTTERIT", "kpl": 4}
    assert not r.is_drawing and not r.is_assembly


def test_a_dwg_is_converted_then_read_like_a_dxf(monkeypatch, tmp_path):
    monkeypatch.setattr(dwg, "_oda_installed", lambda: False)
    src = tmp_path / "plate.dxf"
    src.write_bytes(_plate())
    fake = tmp_path / "dwg2dxf"
    fake.write_text(f'#!/bin/sh\n[ "$1" = -y ] && [ "$2" = -o ] && cp "{src}" "$3"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("LIBREDWG_BIN", str(fake))
    r = read_dwg_file(b"AC1032...", "f2", "plate.dwg")
    conv, _ = r.record
    assert conv.converter == "libredwg" and conv.dxf_kind == "full" and conv.dxf is not None
    assert r.shapes[0].priceable and round(max(r.shapes[0].size_mm)) == 300


def test_a_dwg_that_cant_convert_has_no_shape_and_says_why(monkeypatch):
    monkeypatch.setattr(dwg, "_oda_installed", lambda: False)
    monkeypatch.setattr(dwg, "libredwg_executable", lambda: None)
    r = read_dwg_file(b"AC1032...", "f3", "part.dwg")
    assert r.shapes == [] and r.record.dxf is None
    assert dwg.SAVE_AS_DXF in r.problems[0]


def test_a_pdf_gives_facts_only():
    pdf = _pdf(["MATERIAL: S355MC", "THICKNESS 3 mm",
                "1 ITM-064555.A Plate 2 pcs", "2 ITM-064556.A Cover 1 pcs",
                "PEM-FH-M4-12", "6 PCS"])
    r = read_pdf_file(pdf, "p1", "ASM-070000.pdf")
    assert r.shapes == []
    assert {(f.value["part_key"], f.value["kpl"]) for f in r.of("bom_qty")} \
        == {("itm-064555", 2), ("itm-064556", 1)}
    assert {"name": "PEM-FH-M4-12", "kpl": 6} in [f.value for f in r.of("fastener")]
    assert any(f.value["grade"] == "S355" for f in r.of("material"))


def test_a_pdf_titled_assembly_is_an_assembly():
    assert read_pdf_file(_pdf(["KOKOONPANO", "Runko"]), "p2", "ITM-0700.pdf").is_assembly
    assert not read_pdf_file(_pdf(["Plate", "S355 3 mm"]), "p3", "ITM-064555.pdf").is_assembly


@needs_ocp
def test_a_step_assembly_gives_flat_shapes_bought_bodies_and_thickness():
    from tests.step_models import assembly_step

    r = read_step_file(assembly_step(), "s1", "runko.step")
    assert r.is_assembly
    assert len(r.shapes) == 2                         # Kulma, Pohja
    kulma = next(s for s in r.shapes if s.bends)
    (bend,) = kulma.bends
    assert bend.angle_deg == pytest.approx(90, abs=0.5) and bend.inner_radius_mm == pytest.approx(2)
    assert bend.up is not None and bend.line is not None
    assert {f.value["name"] for f in r.of("bought_body")} == {"Tappi"}
    assert all(f.strength == "measured" for f in r.of("thickness"))
    assert {f.value["part_key"]: f.value["kpl"] for f in r.of("bom_qty")} == {"kulma": 2, "pohja": 1}


def test_read_file_picks_the_reader_by_kind():
    f = InputFile("f9", "x.dxf", "dxf", load=lambda: _plate())
    assert read_file(f).shapes[0].priceable
    assert read_file(InputFile("z", "a.zip", "zip", load=lambda: b"")).shapes == []


def test_value_user_wins_and_bought_parts_add_up():
    v = Value(auto=6, source="ITM-064555.pdf, s. 1: 6 PCS")
    assert v.value == 6
    v.user = 4
    assert v.value == 4
    pem = BoughtPart("PEM-FH-M4-12", Value(6), Value(0.05), Value(0.20))
    assert pem.unit_eur == pytest.approx(0.25) and pem.total_eur == pytest.approx(1.50)


def test_an_empty_shape_is_not_priceable():
    assert not FlatShape("x", []).priceable
