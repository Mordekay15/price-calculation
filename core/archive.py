"""Dropped ZIP files, pure (no Streamlit): every file inside, with the folder
it is in. A ZIP of item folders (one folder per part, its DXF, DWG, STEP and
PDF) becomes the same files as if they were dropped one by one, and the
folder helps put a file on the right card (``core.items``).

ZIPs inside the ZIP are opened too (their folder is the inner ZIP's own
path). Folders made by macOS (``__MACOSX``) and hidden files are left out,
and so is anything past the size and count limits, so a broken or hostile
archive can't fill the memory."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_TOTAL_BYTES = 1 << 30         # 1 GiB unpacked, all files together
MAX_FILES = 5000
_MAX_DEPTH = 3                    # ZIPs inside ZIPs


@dataclass(frozen=True)
class Entry:
    folder: str                   # e.g. "3aua0000098622" or "order/ITM-072558"
    name: str                     # the file's own name
    data: bytes

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.name}" if self.folder else self.name


class ArchiveError(Exception):
    pass


def is_zip(name: str) -> bool:
    return name.lower().endswith(".zip")


def unpack(data: bytes) -> list[Entry]:
    """The files in a ZIP, sorted by path (folder by folder). Raises
    ``ArchiveError`` when it is not a readable ZIP."""
    out: list[Entry] = []
    budget = [MAX_TOTAL_BYTES]
    try:
        _unpack(data, "", out, budget, 0)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"ZIP-tiedostoa ei voitu avata ({exc}).") from exc
    return sorted(out, key=lambda e: (e.folder.lower(), e.name.lower()))


def _unpack(data: bytes, prefix: str, out: list[Entry], budget: list[int], depth: int) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir() or len(out) >= MAX_FILES:
                continue
            path = PurePosixPath(_decoded(info))
            if any(p.startswith(".") or p == "__MACOSX" for p in path.parts):
                continue
            if info.file_size > budget[0]:
                continue
            content = zf.read(info)
            budget[0] -= len(content)
            folder = "/".join(p for p in (prefix, *path.parts[:-1]) if p)
            if is_zip(path.name) and depth < _MAX_DEPTH:
                try:
                    inner = "/".join(p for p in (folder, path.stem) if p)
                    _unpack(content, inner, out, budget, depth + 1)
                    continue
                except zipfile.BadZipFile:
                    pass
            out.append(Entry(folder, path.name, content))


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
