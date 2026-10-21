"""Dropped ZIP files, pure (no Streamlit): every file inside, with the folder
it is in. A ZIP of item folders (one folder per part, its DXF, DWG, STEP and
PDF) becomes the same files as if they were dropped one by one, and the
folder helps put a file on the right card (``core.items``).

ZIPs inside the ZIP are opened too (their folder is the inner ZIP's own
path). Folders made by macOS (``__MACOSX``) and hidden files are left out,
and so is anything past the size and count limits, so a broken or hostile
archive can't fill the memory. A file that ``keep`` turns down (a Creo
``.prt.1``, a spreadsheet) is never read, only named in ``skipped``."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable

MAX_TOTAL_BYTES = 1 << 30         # 1 GiB unpacked, all files together
MAX_FILES = 5000
_MAX_DEPTH = 3                    # ZIPs inside ZIPs


@dataclass(frozen=True)
class Entry:
    """One file. Its bytes are read when asked for (``data``), one file at a
    time, so a large ZIP is never in memory whole."""

    folder: str                   # e.g. "3aua0000098622" or "order/ITM-072558"
    name: str                     # the file's own name
    load: Callable[[], bytes] = field(repr=False, compare=False)

    @property
    def data(self) -> bytes:
        return self.load()

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.name}" if self.folder else self.name


class ArchiveError(Exception):
    pass


def is_zip(name: str) -> bool:
    return name.lower().endswith(".zip")


def unpack(data: bytes, keep=lambda name: True,
           skipped: list[str] | None = None) -> list[Entry]:
    """The files in a ZIP, sorted by path (folder by folder). Raises
    ``ArchiveError`` when it is not a readable ZIP."""
    out: list[Entry] = []
    budget = [MAX_TOTAL_BYTES]
    skipped = [] if skipped is None else skipped
    try:
        _unpack(io.BytesIO(data), "", out, budget, 0, keep, skipped)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"ZIP-tiedostoa ei voitu avata ({exc}).") from exc
    return _sorted(out)


def _sorted(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (e.folder.lower(), e.name.lower()))


def _unpack(source, prefix: str, out: list[Entry], budget: list[int], depth: int,
            keep, skipped: list[str]) -> None:
    # Left open: its entries are read later, one by one (``Entry.load``).
    zf = zipfile.ZipFile(source)
    for info in zf.infolist():
        if info.is_dir() or len(out) >= MAX_FILES:
            continue
        path = PurePosixPath(_decoded(info))
        if any(p.startswith(".") or p == "__MACOSX" for p in path.parts):
            continue
        folder = "/".join(p for p in (prefix, *path.parts[:-1]) if p)
        nested = is_zip(path.name) and depth < _MAX_DEPTH
        if not nested and not keep(path.name):
            skipped.append(path.name)
            continue
        if info.file_size > budget[0]:
            continue
        budget[0] -= info.file_size
        if nested:
            try:
                inner = "/".join(p for p in (folder, path.stem) if p)
                _unpack(io.BytesIO(zf.read(info)), inner, out, budget, depth + 1, keep,
                        skipped)
                continue
            except zipfile.BadZipFile:
                pass
        out.append(Entry(folder, path.name, lambda zf=zf, info=info: zf.read(info)))


def _decoded(info: zipfile.ZipInfo) -> str:
    """The entry's path. Without the UTF-8 flag Python reads the name as
    CP437, so "ä" comes out wrong: the bytes are read as UTF-8 when they are
    (macOS, 7-Zip), else as CP850 (Windows' own ZIPs on Finnish Windows)."""
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    for encoding in ("utf-8", "cp850"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return name
