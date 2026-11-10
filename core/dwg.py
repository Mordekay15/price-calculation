"""DWG drawings converted to DXF, for the DWG reader (``core.readers.read_dwg_file``).

Python can't read DWG (AutoCAD's closed format), so a converter does it, the
first one found of:

1. **ODA File Converter** (free, most reliable): ezdxf's ``odafc`` add-on runs
   it when it is installed (``odafc.is_installed()``).
2. **LibreDWG**'s ``dwg2dxf`` (open source): the ``LIBREDWG_BIN`` environment
   variable, ``bin/dwg2dxf`` (on Windows ``bin/libredwg-win64/dwg2dxf.exe``, the
   official build with its DLLs), or ``dwg2dxf`` on the ``PATH``.

With neither, or when the conversion fails, the file comes back with a
problem that says to save it as DXF.

The converted DXF's OBJECTS section is removed before it is read: it holds
only settings the app never uses (groups, layouts, plot settings, image and
style definitions), and LibreDWG can write it so that ezdxf refuses the whole
file (M3440659-00.dwg: four nameless entries pointing at handle 0). ezdxf
builds default ones in its place."""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

_log = logging.getLogger(__name__)
_TIMEOUT_S = 60
_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

SAVE_AS_DXF = ("Avaa piirustus CAD-ohjelmassa, tallenna se DXF-muodossa (Tallenna "
               "nimellä → DXF) ja lataa se uudelleen.")
# The converter's own reason (English, technical) is logged, not shown.
NOT_CONVERTED = "DWG-tiedostoa ei voitu avata: sen muunnos DXF-muotoon ei onnistunut."


class ConversionError(Exception):
    """The DWG could not be converted; the message says why, in Finnish."""


def _oda_installed() -> bool:
    try:
        from ezdxf.addons import odafc
        return odafc.is_installed()
    except Exception:  # noqa: BLE001 — a broken ODA setup is "not installed"
        return False


def libredwg_executable() -> str | None:
    """The ``dwg2dxf`` to run, or None."""
    env = os.environ.get("LIBREDWG_BIN")
    if env and pathlib.Path(env).is_file():
        return env
    local = ([_BIN / "libredwg-win64" / "dwg2dxf.exe", _BIN / "dwg2dxf.exe"]
             if sys.platform == "win32" else [_BIN / "dwg2dxf"])
    for exe in local:
        if exe.is_file():
            return str(exe)
    return shutil.which("dwg2dxf")


def converter() -> str | None:
    """Which converter would be used: "ODA", "LibreDWG" or None."""
    if _oda_installed():
        return "ODA"
    if libredwg_executable():
        return "LibreDWG"
    return None


def _with_oda(src: pathlib.Path, dst: pathlib.Path) -> None:
    from ezdxf.addons import odafc
    try:
        odafc.convert(src, dst, version="R2018", replace=True)
    except Exception as exc:  # noqa: BLE001
        _log.warning("ODA File Converter failed: %s", exc)
        raise ConversionError(NOT_CONVERTED)


def _with_libredwg(exe: str, src: pathlib.Path, dst: pathlib.Path,
                   minimal: bool = False) -> None:
    args = [exe, "-y", *(["-m"] if minimal else []), "-o", str(dst), str(src)]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ConversionError("DWG-tiedostoa ei voitu avata: sen muunnos DXF-muotoon ei "
                              f"valmistunut {_TIMEOUT_S} sekunnissa.")
    except OSError as exc:
        _log.warning("LibreDWG did not start: %s", exc)
        raise ConversionError("DWG-tiedostoja ei voi nyt avata: muunnin (LibreDWG) ei "
                              "käynnisty tällä palvelimella.")
    # dwg2dxf can exit non-zero on warnings and still write a usable file.
    if not dst.is_file() or dst.stat().st_size == 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        _log.warning("LibreDWG failed: %s", detail[-1] if detail else "no output")
        raise ConversionError(NOT_CONVERTED)


def dwg_to_dxf(data: bytes, *, minimal: bool = False) -> bytes:
    """The DWG ``data`` as DXF bytes. Raises ConversionError. ``minimal`` asks
    LibreDWG for its minimal DXF (the drawing's entities only)."""
    oda = _oda_installed()
    exe = None if oda else libredwg_executable()
    if not oda and exe is None:
        raise ConversionError("DWG-tiedostoja ei voi avata tällä palvelimella: muunninta "
                              "(ODA File Converter tai LibreDWG) ei ole asennettu.")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = pathlib.Path(tmp, "drawing.dwg"), pathlib.Path(tmp, "drawing.dxf")
        src.write_bytes(data)
        if oda:
            _with_oda(src, dst)
        else:
            _with_libredwg(exe, src, dst, minimal)
        if not dst.is_file():
            raise ConversionError(NOT_CONVERTED)
        return _without_objects(_join_broken_values(dst.read_bytes()))


def _join_broken_values(dxf: bytes) -> bytes:
    """LibreDWG writes a text with a line break in it (a title block's long
    disclaimer) over two lines, and the DXF no longer reads (a code line that
    isn't a number). Such a broken-off line is joined back to its value."""
    lines = dxf.replace(b"\r\n", b"\n").split(b"\n")
    out: list[bytes] = []
    code = True    # the next line is a group code
    for line in lines:
        if code and out and not line.strip().lstrip(b"-").isdigit() and line.strip():
            out[-1] += b" " + line     # the value went on: not a code
            continue
        out.append(line)
        code = not code
    return b"\n".join(out)


def _without_objects(dxf: bytes) -> bytes:
    """``dxf`` less its OBJECTS section (see the module's docstring). The
    file is walked as code / value line pairs, so a text that happens to read
    "OBJECTS" is never taken for the section."""
    lines = dxf.split(b"\n")
    out: list[bytes] = []
    skipping = False
    i = 0
    while i + 1 < len(lines):
        code, value = lines[i].strip(), lines[i + 1].strip()
        if (not skipping and code == b"0" and value == b"SECTION" and i + 3 < len(lines)
                and lines[i + 2].strip() == b"2" and lines[i + 3].strip() == b"OBJECTS"):
            skipping = True
        if not skipping:
            out += lines[i:i + 2]
        elif code == b"0" and value == b"ENDSEC":
            skipping = False
        i += 2
    out += lines[i:]
    return b"\n".join(out)


_UNIT_VARS = (b"$INSUNITS", b"$MEASUREMENT")


def _with_units(minimal: bytes, full: bytes) -> bytes:
    """``minimal`` with the unit header variables of ``full`` (a minimal DXF
    has none, so the unit would only be guessed)."""
    lines = full.splitlines()
    found = []
    for i, line in enumerate(lines[:-2]):
        if line.strip() in _UNIT_VARS:
            found += [b"  9", line.strip(), lines[i + 1].rstrip(), lines[i + 2].rstrip()]
    marker = b"HEADER\n"
    at = minimal.replace(b"\r\n", b"\n").find(marker)
    if not found or at < 0:
        return minimal
    text = minimal.replace(b"\r\n", b"\n")
    at += len(marker)
    return text[:at] + b"\n".join(found) + b"\n" + text[at:]
