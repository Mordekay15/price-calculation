"""Fasteners a drawing PDF calls out (press nuts, studs, standoffs, rivets…),
read from where the words sit on the page: a callout's count ("6 pcs",
"(4pcs)", "X4KPL") is on its line or just below or above it, not always next
to it in the PDF's text."""

from __future__ import annotations

import re

# A word naming a fastener: PEM-FH-M4-12, KALEI NUT, STUD M8-12, STANDOFF,
# POP RIVET, PURISTEMUTTERIT, RUUVIT…
_NAME = re.compile(r"^(?:PEM\b|PEM[-_]|KALEI|NUTS?$|STUDS?$|STANDOFFS?$|RIVETS?$|SCREWS?$|"
                   r"BOLTS?$|\w*MUTTER|\w*RUUV|\w*PULT|\w*NIIT|\w*HOLK)", re.IGNORECASE)
_COUNT = re.compile(r"(\d+)\s*(?:PCS|PC|KPL)\b", re.IGNORECASE)
# Not a fastener to buy: its hole, or a line of the revision table.
_NOT = re.compile(r"\bHOLES?\b|DRAWHOLE|\bFOR\b|\d{1,2}-[A-Z][a-z]{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}"
                  r"|\b(?:ADDED|REPLACED|CHANGED?|MOVED|REMOVED|MODIFIED|WAS|SIZE)\b", re.IGNORECASE)
_TAIL = re.compile(r"[\s,]*(?:OR EQUAL\b.*|\(?\d+\s*(?:PCS|PC|KPL)\b.*|\([^)]*|X)$", re.IGNORECASE)


def _clusters(words: list[dict]) -> list[dict]:
    """Words on one baseline, close to each other: one piece of text on the
    sheet ``{"text", "x0", "x1", "top", "bottom"}``."""
    out: list[dict] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        h = w["bottom"] - w["top"]
        last = out[-1] if out else None
        if (last and abs(w["top"] - last["top"]) < h * 0.4
                and 0 <= w["x0"] - last["x1"] < h * 1.5):
            last["text"] += " " + w["text"]
            last["x1"] = w["x1"]
        else:
            out.append({k: w[k] for k in ("text", "x0", "x1", "top", "bottom")})
    return out


def _count_near(c: dict, clusters: list[dict]) -> int | None:
    """The "N pcs" on the line just below the callout, else just above, that
    lines up with it."""
    h = c["bottom"] - c["top"]
    for below in (True, False):
        for o in clusters:
            gap = o["top"] - c["bottom"] if below else c["top"] - o["bottom"]
            if (-h * 0.3 <= gap < h * 0.8 and o["x0"] < c["x1"] and o["x1"] > c["x0"]
                    and (m := _COUNT.search(o["text"])) and not _NOT.search(o["text"])):
                return int(m.group(1))
    return None


def fastener_callouts(words: list[dict]) -> list[tuple[str, int]]:
    """``[(name, kpl)]`` of the fasteners among a page's words (pdfplumber's
    ``extract_words``), the same name added up. A callout with no count is
    1 kpl each time it is written."""
    clusters = _clusters(words)
    out: dict[str, int] = {}
    for c in clusters:
        toks = c["text"].split()
        hit = next((i for i, t in enumerate(toks) if _NAME.search(t)), None)
        if hit is None or _NOT.search(c["text"]):
            continue
        # The name: from a size before it (M10 KALEI NUT) to the end, less "or equal, 2 pcs".
        start = hit - 1 if hit and re.match(r"^M\d", toks[hit - 1], re.IGNORECASE) else hit
        name = " ".join(toks[start:])
        while (cut := _TAIL.sub("", name)) != name:     # "(3 or 5 pcs)": both ends
            name = cut
        name = name.strip(" ,").upper()
        m = _COUNT.search(c["text"])
        n = int(m.group(1)) if m else _count_near(c, clusters) or 1
        out[name] = out.get(name, 0) + n
    return list(out.items())


def pdf_fasteners(data: bytes, max_pages: int = 3) -> list[tuple[str, int]]:
    """``[(name, kpl)]`` the drawing PDF calls out; [] when unreadable."""
    import io

    import pdfplumber

    out: dict[str, int] = {}
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:max_pages]:
                for name, n in fastener_callouts(page.extract_words()):
                    out[name] = out.get(name, 0) + n
    except Exception:  # noqa: BLE001 - any unreadable PDF just gives none
        return []
    return list(out.items())
