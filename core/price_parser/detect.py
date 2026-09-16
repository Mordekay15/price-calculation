"""
core/parser/detect.py
=====================
Works out which supplier a price-list PDF came from, so the sidebar can offer
a single upload slot instead of one per supplier.

Detection reads the first couple of pages as text and looks for the supplier's
own name. Note that "Stremet" appears in BOTH price lists — it is the customer,
not the supplier — so it is deliberately not used as a marker.
"""

import io

import pdfplumber

# Supplier key -> keywords that identify that supplier's own price list.
# Keywords are matched against whitespace-normalised lowercase page text.
_MARKERS: dict[str, tuple[str, ...]] = {
    "tatasteel": ("tatasteel",),
    "tibnor":    ("tibnor",),
}

_PAGES_TO_SCAN = 2


def _pdf_text(file_bytes: bytes, pages: int = _PAGES_TO_SCAN) -> str:
    """Return the first N pages as lowercase text with all whitespace removed.

    Stripping whitespace makes matching robust to 'Tata Steel', 'TATA STEEL'
    and line breaks landing mid-name.
    """
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        raw = " ".join(
            (page.extract_text() or "") for page in pdf.pages[:pages]
        )
    return "".join(raw.lower().split())


def detect_supplier(file_bytes: bytes) -> str | None:
    """Return a supplier key, or None if the PDF matches zero or both.

    None is a meaningful answer, not a failure: an ambiguous PDF must be
    routed by the user rather than guessed at, because saving a price list
    under the wrong supplier corrupts quotes silently.
    """
    text = _pdf_text(file_bytes)
    hits = [
        key for key, keywords in _MARKERS.items()
        if any(kw in text for kw in keywords)
    ]
    return hits[0] if len(hits) == 1 else None


def is_empty(parsed) -> bool:
    """True when a parser produced nothing at all.

    Signals that detection picked the wrong parser, or that the supplier
    changed their PDF layout. Either way the result should not be saved.

    Accepts either the parsers' current output — a list of price records — or
    a legacy wide-row dict, so it stays correct through the migration.
    """
    if isinstance(parsed, dict):
        return not any(rows for rows in parsed.values())
    return not parsed