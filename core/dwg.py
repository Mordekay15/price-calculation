"""DWG drawings: converted to DXF, then read like any DXF (``core.dxf``).

Python can't read DWG (AutoCAD's closed format), so a converter does it, the
first one found of:

1. **ODA File Converter** (free, most reliable): ezdxf's ``odafc`` add-on runs
   it when it is installed (``odafc.is_installed()``).
2. **LibreDWG**'s ``dwg2dxf`` (open source): the ``LIBREDWG_BIN`` environment
   variable, ``bin/dwg2dxf`` (``bin/dwg2dxf.exe`` on Windows), or ``dwg2dxf`` on
   the ``PATH``.

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
    local = _BIN / ("dwg2dxf.exe" if sys.platform == "win32" else "dwg2dxf")
    if local.is_file():
        return str(local)
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


def _with_libredwg(exe: str, src: pathlib.Path, dst: pathlib.Path) -> None:
    try:
        done = subprocess.run([exe, "-y", "-o", str(dst), str(src)], capture_output=True,
                              text=True, timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"LibreDWG:n muunnos ei valmistunut {_TIMEOUT_S} sekunnissa.")
    except OSError as exc:
        raise ConversionError(f"LibreDWG:tä ei voitu käynnistää ({exc}).")
    # dwg2dxf can exit non-zero on warnings and still write a usable file.
    if not dst.is_file() or dst.stat().st_size == 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise ConversionError("LibreDWG ei osannut muuntaa tiedostoa"
                              + (f" ({detail[-1]})." if detail else "."))


def dwg_to_dxf(data: bytes) -> bytes:
    """The DWG ``data`` as DXF bytes. Raises ConversionError."""
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
            _with_libredwg(exe, src, dst)
        if not dst.is_file():
            raise ConversionError("Muunnin ei tuottanut DXF-tiedostoa.")
        return dst.read_bytes()


def read_drawing(data: bytes, name: str) -> DxfFile:
    """Read a DXF, or a DWG converted to DXF. Never raises: a file that can't be
    read comes back with problems."""
    if not is_dwg(name):
        return read_dxf(data, name)
    try:
        dxf = dwg_to_dxf(data)
    except ConversionError as exc:
        return DxfFile(name=name, problems=[f"{exc} {SAVE_AS_DXF}"])
    return read_dxf(dxf, name)
