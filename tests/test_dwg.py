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


def test_a_text_broken_over_two_lines_by_libredwg_is_joined_back():
    # M3440809-00.dwg: the title block's disclaimer came out over two lines,
    # and the DXF no longer read ("Invalid group code").
    from core.dwg import _join_broken_values

    broken = b"  0\r\nTEXT\r\n  1\r\nnot to be reproduced without the\r\nrepresentative thereof.\r\n  7\r\nSTYLE\r\n"
    assert _join_broken_values(broken).split(b"\n") == [
        b"  0", b"TEXT", b"  1", b"not to be reproduced without the representative thereof.",
        b"  7", b"STYLE", b""]


def _broken_objects_dxf() -> bytes:
    """A plate with a dashed bend line, whose OBJECTS section is written as
    LibreDWG wrote M3440659-00.dwg's: nameless root-dictionary entries
    pointing at handle 0, which ezdxf refuses ("Invalid handle 0")."""
    doc = ezdxf.new("R2000", setup=["linetypes"])
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    rect(msp, 0, 0, 300, 200)
    msp.add_line((100, 0), (100, 200), dxfattribs={"linetype": "DASHED"})
    data = dxf_bytes(doc)
    at = data.index(b"ACAD_GROUP")
    at = data.rindex(b"\n", 0, data.rindex(b"\n", 0, at)) + 1    # the entry's "  3" line
    return data[:at] + b"  3\n\n350\n0\n" * 2 + data[at:]


def test_a_broken_objects_section_makes_ezdxf_refuse_the_file():
    from core.dxf import read_dxf

    f = read_dxf(_broken_objects_dxf(), "plate.dxf")
    assert f.pieces == [] and "Invalid handle 0" in f.problems[0]


def test_the_objects_section_is_removed_and_the_rest_kept():
    data = _broken_objects_dxf()
    out = dwg._without_objects(data)
    assert b"\nOBJECTS\n" in data and b"\nOBJECTS\n" not in out
    for section in (b"HEADER", b"TABLES", b"BLOCKS", b"ENTITIES"):
        assert b"\n" + section + b"\n" in out
    assert out.rstrip().endswith(b"EOF")
    assert dwg._without_objects(b"  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n") \
        == b"  0\nSECTION\n  2\nENTITIES\n  0\nENDSEC\n"


def test_a_dwg_with_a_broken_objects_section_reads_in_full(no_oda, monkeypatch, tmp_path):
    # The full DXF reads once its OBJECTS section is gone: no fall back to the
    # minimal DXF (here a fake that would lose the dashed line), so the bend
    # line keeps its linetype and is counted.
    full, minimal = tmp_path / "full.dxf", tmp_path / "minimal.dxf"
    full.write_bytes(_broken_objects_dxf())
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    rect(doc.modelspace(), 0, 0, 300, 200)
    minimal.write_bytes(dxf_bytes(doc))
    fake = tmp_path / "dwg2dxf"
    fake.write_text(f'#!/bin/sh\nif [ "$2" = -m ]; then cp "{minimal}" "$4"; '
                    f'else cp "{full}" "$3"; fi\n')
    fake.chmod(0o755)
    monkeypatch.setenv("LIBREDWG_BIN", str(fake))
    f = dwg.read_drawing(b"AC1015...", "plate.dwg")
    assert f.problems == []
    assert [p.solid for p in f.pieces].count(False) == 1     # the dashed line
    part = f.part()
    assert round(part.outline.width_mm) == 300 and part.bends == 1


def test_an_entity_that_cant_be_read_is_skipped_and_logged(caplog):
    # M3440659-00.dwg's sheet has an OLE2FRAME (an object embedded from
    # another program): it used to keep the part from being priced.
    import logging

    from core.dxf import read_dxf

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    rect(doc.modelspace(), 0, 0, 300, 200)
    doc.modelspace().new_entity("OLE2FRAME", {"layer": "LOGO"})
    with caplog.at_level(logging.WARNING, logger="core.dxf"):
        f = read_dxf(dxf_bytes(doc), "plate.dxf")
    assert f.unreadable == {"LOGO": {"OLE2FRAME": 1}}
    part = f.part({"0", "LOGO"})
    assert part.problems == [] and round(part.outline.width_mm) == 300
    assert [r.getMessage() for r in caplog.records] == [
        "plate.dxf: skipped what can't be read on layer 'LOGO': OLE2FRAME ×1"]
