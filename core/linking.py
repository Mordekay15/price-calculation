"""Linking supporting files (PDF drawings, STEP models) to parts by part
number, pure (no Streamlit). ``100325.pdf``, ``100325-revA.pdf`` and
``100325_A.pdf`` all link to the part drawn in ``100325.dxf``."""

from __future__ import annotations

import re
from pathlib import PurePath

# A revision at the end of a name: -revA, _rev2, " rev B", -A, _B1 (one letter,
# optionally followed by digits, after a separator).
_REVISION = re.compile(r"(?:[\s_\-.]*rev[\s_\-.]*[a-z0-9]{1,3}|[\s_\-.]+[a-z][0-9]?)$",
                       re.IGNORECASE)


def part_key(file_name: str) -> str:
    """The part number a file name stands for: the stem, lower case, without
    a revision suffix. A stem that is only a revision-like letter keeps it."""
    stem = PurePath(file_name).stem.strip().lower()
    stripped = _REVISION.sub("", stem)
    return stripped or stem


def revision(file_name: str) -> str:
    """The revision suffix of a file name (``revA``, ``A``), "" when none."""
    stem = PurePath(file_name).stem.strip()
    m = _REVISION.search(stem)
    return re.sub(r"^[\s_\-.]+", "", m.group(0)) if m and _REVISION.sub("", stem) else ""


def link_files(part_names: dict[str, str], file_names: dict[str, str]) -> dict[str, list[str]]:
    """Which files belong to which part. ``part_names`` and ``file_names``
    map an id to a file name; returns ``{part id: [file id, …]}`` for the
    parts that got any. A file goes to every part with its part number (two
    cards of one drawing both get it); unmatched files are left out. The
    same file stem wins over a match without revisions, so ``bracket_L.pdf``
    goes to ``bracket_L.dxf``, not to ``bracket_R.dxf`` as well."""
    by_stem: dict[str, list[str]] = {}
    by_key: dict[str, list[str]] = {}
    for pid, name in part_names.items():
        by_stem.setdefault(_stem(name), []).append(pid)
        by_key.setdefault(part_key(name), []).append(pid)
    out: dict[str, list[str]] = {}
    for fid, name in file_names.items():
        for pid in by_stem.get(_stem(name)) or by_key.get(part_key(name), []):
            out.setdefault(pid, []).append(fid)
    return out


def _stem(file_name: str) -> str:
    return PurePath(file_name).stem.strip().lower()


def unlinked(links: dict[str, list[str]], file_names: dict[str, str]) -> list[str]:
    """The file ids no part got."""
    linked = {f for files in links.values() for f in files}
    return [f for f in file_names if f not in linked]
