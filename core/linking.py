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


def link_files(part_names: dict[str, str], file_names: dict[str, str]) -> dict[str, list[str]]:
    """Which files belong to which part. ``part_names`` and ``file_names``
    map an id to a file name; returns ``{part id: [file id, …]}`` for the
    parts that got any. A file goes to every part with its part number (two
    cards of one drawing both get it); unmatched files are left out. The
    same file stem wins over a match without revisions, so ``bracket_L.pdf``
    goes to ``bracket_L.dxf``, not to ``bracket_R.dxf`` as well."""
    by_stem: dict[str, list[str]] = {}
    by_key: dict[str, list[str]] = {}
    by_base: dict[str, list[str]] = {}
    for pid, name in part_names.items():
        by_stem.setdefault(_stem(name), []).append(pid)
        by_key.setdefault(part_key(name), []).append(pid)
        by_base.setdefault(base_key(name), []).append(pid)
    out: dict[str, list[str]] = {}
    for fid, name in file_names.items():
        key = part_key(name)
        for pid in by_stem.get(_stem(name)) or by_key.get(key) or by_base.get(key, []):
            out.setdefault(pid, []).append(fid)
    return out


def base_key(file_name: str) -> str:
    """The part number without a numbered piece or sheet at its end:
    ``3aua0000098622_1.dxf`` and ``_2.dxf`` are both ``3aua0000098622``, so
    ``3AUA0000098622.pdf`` links to both."""
    key = part_key(file_name)
    return re.sub(r"[\s_\-.]+\d{1,2}$", "", key) or key


def _patterns(part_names) -> dict[str, re.Pattern]:
    """A pattern per part number (``base_key``) that finds it in a text, its
    separators optional: ``ITM-072558`` finds ``ITM-072558.A`` and
    ``ITM072558``. Part numbers shorter than 4 characters or without a digit
    are left out."""
    patterns: dict[str, re.Pattern] = {}
    for name in part_names:
        key = base_key(name)
        if len(key) >= 4 and any(c.isdigit() for c in key) and key not in patterns:
            body = r"[\s_\-.]?".join(re.escape(p) for p in re.split(r"[\s_\-.]+", key) if p)
            patterns[key] = re.compile(rf"(?<![a-z0-9]){body}(?![0-9])", re.IGNORECASE)
    return patterns


# A quantity on a BOM row: "2 pcs", "4 kpl", "1 st".
_QTY = re.compile(r"(?<![\w.,])(\d{1,5})\s*(?:pcs|pc|pce|kpl|st|stk|ea)\b", re.IGNORECASE)


def bom_quantities(part_names: dict[str, list[str]], texts: dict[str, str]
                   ) -> dict[str, tuple[int, list[str]]]:
    """How many of each part the BOMs ask for: ``part_names`` maps a card to
    its file names, ``texts`` a PDF's name to its text. A BOM row names one
    card's part number and a quantity with its unit (``1 ITM-064555.A Plate
    … 2 pcs``). Returns ``{card: (kpl, [BOM names])}``, the kpl of all the
    BOMs that list it added up (one BOM's first row for it)."""
    patterns = _patterns(n for ns in part_names.values() for n in ns)
    card_of = {base_key(n): cid for cid, ns in part_names.items() for n in ns}
    out: dict[str, tuple[int, list[str]]] = {}
    for doc, text in texts.items():
        seen = set()
        for line in (text or "").splitlines():
            qty = _QTY.findall(line)
            if not qty:
                continue
            cards = {card_of[k] for k, pat in patterns.items() if pat.search(line)}
            if len(cards) != 1 or (cid := cards.pop()) in seen:
                continue
            seen.add(cid)
            n, docs = out.get(cid, (0, []))
            out[cid] = (n + int(qty[-1]), [*docs, doc])
    return out


def link_by_text(part_names: dict[str, str], texts: dict[str, str],
                 group=None) -> dict[str, list[str]]:
    """Files whose name says nothing, linked by the part number written in
    them: a drawing ``DRW-023179.pdf`` whose title block reads
    ``ITM-072558.A`` goes to ``ITM-072558_A.dxf``. ``texts`` maps a file id
    to its text. A file goes to a part only when its text names exactly one
    part number on the cards (numbered pieces of one, ``_1`` and ``_2``, count
    as one) (an assembly drawing listing several is left for
    linking by hand) or, with ``group`` (part id -> card), the part numbers of
    exactly one card (see ``_patterns``)."""
    patterns = _patterns(part_names.values())
    group = group or (lambda pid: pid)
    out: dict[str, list[str]] = {}
    for fid, text in texts.items():
        found = {k for k, pat in patterns.items() if pat.search(text or "")}
        pids = [pid for pid, name in part_names.items() if base_key(name) in found]
        if len({group(pid) for pid in pids}) != 1 and len(found) != 1:
            continue
        for pid in pids:
            out.setdefault(pid, []).append(fid)
    return out


def _stem(file_name: str) -> str:
    return PurePath(file_name).stem.strip().lower()



# A part number in a drawing: six or more letters and digits with at least
# five digits in a row (3AXD50001016005, ITM-072558, 100325).
_PART_NUMBER = re.compile(r"(?<![\w.\-])(?=[a-z0-9\-]*\d{5})[a-z0-9]+(?:-[a-z0-9]+)?(?![\w.])",
                          re.IGNORECASE)


def callouts(text: str) -> list[str]:
    """The part numbers an assembly drawing calls out, as ``base_key``s, in
    order: a number alone on its line, or after only figures (``1:2 12
    3AXD50000018805``). A number among words (``ASSEMBLY KIT: …``, ``DMS
    Number …``, the title block) is the drawing's own data, not an item."""
    out: list[str] = []
    for line in (text or "").splitlines():
        found = _PART_NUMBER.findall(line)
        if len(found) != 1 or re.search(r"[a-z]", _PART_NUMBER.sub("", line), re.IGNORECASE):
            continue
        key = base_key(found[0])
        if key not in out:
            out.append(key)
    return out
