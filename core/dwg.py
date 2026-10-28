"""DWG drawings: converted to DXF, then read like any DXF (``core.dxf``).

Python can't read DWG (AutoCAD's closed format), so a converter does it, the
first one found of:

1. **ODA File Converter** (free, most reliable): ezdxf's ``odafc`` add-on runs
   it when it is installed (``odafc.is_installed()``).
2. **LibreDWG**'s ``dwg2dxf`` (open source): the ``LIBREDWG_BIN`` environment
   variable, ``bin/dwg2dxf`` (on Windows ``bin/libredwg-win64/dwg2dxf.exe``, the
   official build with its DLLs), or ``dwg2dxf`` on the ``PATH``.

With neither, or when the conversion fails, the file comes back with a
problem that says to save it as DXF."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from core.dxf import DxfFile, read_dxf

_TIMEOUT_S = 60
_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

SAVE_AS_DXF = "Tallenna piirustus DXF-muodossa ja lataa se uudelleen."


class ConversionError(Exception):
    """The DWG could not be converted; the message says why, in Finnish."""


def is_dwg(name: str) -> bool:
    return name.lower().endswith(".dwg")


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
        raise ConversionError(f"ODA File Converter ei osannut muuntaa tiedostoa ({exc}).")


def _with_libredwg(exe: str, src: pathlib.Path, dst: pathlib.Path,
                   minimal: bool = False) -> None:
    args = [exe, "-y", *(["-m"] if minimal else []), "-o", str(dst), str(src)]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"LibreDWG:n muunnos ei valmistunut {_TIMEOUT_S} sekunnissa.")
    except OSError as exc:
        raise ConversionError(f"LibreDWG:tä ei voitu käynnistää ({exc}).")
    # dwg2dxf can exit non-zero on warnings and still write a usable file.
    if not dst.is_file() or dst.stat().st_size == 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ConversionError("LibreDWG ei osannut muuntaa tiedostoa"
                              + (f" ({detail[-1]})." if detail else "."))


def dwg_to_dxf(data: bytes, *, minimal: bool = False) -> bytes:
    """The DWG ``data`` as DXF bytes. Raises ConversionError. ``minimal`` asks
    LibreDWG for its minimal DXF (the drawing's entities only)."""
    oda = _oda_installed()
    exe = None if oda else libredwg_executable()
    if not oda and exe is None:
        raise ConversionError("DWG-tiedostoja ei voi lukea: muunninta (ODA File Converter "
                              "tai LibreDWG) ei ole asennettu.")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = pathlib.Path(tmp, "drawing.dwg"), pathlib.Path(tmp, "drawing.dxf")
        src.write_bytes(data)
        if oda:
            _with_oda(src, dst)
        else:
            _with_libredwg(exe, src, dst, minimal)
        if not dst.is_file():
            raise ConversionError("Muunnin ei tuottanut DXF-tiedostoa.")
        return _join_broken_values(dst.read_bytes())


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


def read_drawing(data: bytes, name: str) -> DxfFile:
    """Read a DXF, or a DWG converted to DXF. Never raises: a file that can't be
    read comes back with problems."""
    if not is_dwg(name):
        return read_dxf(data, name)
    try:
        full = dwg_to_dxf(data)
        f = read_dxf(full, name)
        if f.problems and not f.pieces and converter() == "LibreDWG":
            # LibreDWG's full DXF can carry tables ezdxf can't read; its
            # minimal DXF holds just the entities, which is all we need, plus
            # the drawing's unit, taken from the full one.
            minimal = read_dxf(_with_units(dwg_to_dxf(data, minimal=True), full), name)
            if minimal.pieces:
                return minimal
    except ConversionError as exc:
        return DxfFile(name=name, problems=[f"{exc} {SAVE_AS_DXF}"])
    if f.problems and not f.pieces:
        f.problems = [f"{p} {SAVE_AS_DXF}" for p in f.problems]
    return f
