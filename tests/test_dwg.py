"""DWG drawings: which converter is used, and that a converted file reads
like a DXF. The converters themselves are faked (neither is installed in CI)."""

import ezdxf
import pytest

from core import dwg
from tests.conftest import dxf_bytes, rect


@pytest.fixture
def plate_dxf():
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    rect(doc.modelspace(), 0, 0, 300, 200)
    return dxf_bytes(doc)


@pytest.fixture
def no_oda(monkeypatch):
    monkeypatch.setattr(dwg, "_oda_installed", lambda: False)


def test_a_dxf_is_read_as_before(plate_dxf, no_oda):
    f = dwg.read_drawing(plate_dxf, "plate.DXF")
    assert f.problems == [] and f.part().outline is not None


def test_without_a_converter_the_card_says_to_save_as_dxf(no_oda, monkeypatch):
    monkeypatch.setattr(dwg, "libredwg_executable", lambda: None)
    assert dwg.converter() is None
    f = dwg.read_drawing(b"AC1032...", "part.dwg")
    assert len(f.problems) == 1 and dwg.SAVE_AS_DXF in f.problems[0]
    assert f.part().problems == f.problems        # nothing else piled on


def test_libredwg_converts_when_oda_is_missing(plate_dxf, no_oda, monkeypatch, tmp_path):
    # A stand-in dwg2dxf: writes the plate to the -o file, as the real one would.
    src = tmp_path / "plate.dxf"
    src.write_bytes(plate_dxf)
    fake = tmp_path / "dwg2dxf"
    fake.write_text(f'#!/bin/sh\n[ "$1" = -y ] && [ "$2" = -o ] && cp "{src}" "$3"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("LIBREDWG_BIN", str(fake))
    assert dwg.converter() == "LibreDWG"
    f = dwg.read_drawing(b"AC1032...", "plate.dwg")
    assert f.problems == [] and f.name == "plate.dwg"
    assert round(f.part().outline.width_mm) == 300


def test_a_failed_conversion_is_a_problem_not_a_crash(no_oda, monkeypatch, tmp_path):
    fake = tmp_path / "dwg2dxf"
    fake.write_text('#!/bin/sh\necho "ERROR: unsupported version" >&2\nexit 1\n')
    fake.chmod(0o755)
    monkeypatch.setenv("LIBREDWG_BIN", str(fake))
    f = dwg.read_drawing(b"AC1032...", "bad.dwg")
    assert "unsupported version" in f.problems[0] and dwg.SAVE_AS_DXF in f.problems[0]


def test_oda_is_preferred_when_installed(monkeypatch):
    monkeypatch.setattr(dwg, "_oda_installed", lambda: True)
    monkeypatch.setattr(dwg, "libredwg_executable", lambda: "/usr/bin/dwg2dxf")
    assert dwg.converter() == "ODA"


def test_the_unit_is_carried_into_libredwgs_minimal_dxf():
    minimal = b"999\nLibreDWG\n  0\nSECTION\n  2\nHEADER\n  9\n$ACADVER\n  1\nAC1024\n  0\nENDSEC\n"
    full = b"  9\n$MEASUREMENT\n 70\n     1\n  9\n$INSUNITS\n 70\n     4\n  9\n$X\n"
    out = dwg._with_units(minimal, full)
    assert b"$INSUNITS\n 70\n     4" in out and b"$MEASUREMENT\n 70\n     1" in out
    assert out.index(b"$INSUNITS") < out.index(b"$ACADVER")
    assert dwg._with_units(minimal, b"no units here") == minimal
