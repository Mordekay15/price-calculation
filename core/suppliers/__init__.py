"""Supplier registry: which PDF parser handles which price list, supplier
detection, and the JSON files the parsed prices are saved to."""

import datetime
import io
import json
import pathlib
from dataclasses import dataclass
from typing import Callable

import pdfplumber

from core.suppliers.tatasteel import parse_tatasteel_pdf
from core.suppliers.tibnor import parse_tibnor_pdf


@dataclass(frozen=True)
class Supplier:
    key:    str                      # session-state / widget key prefix
    label:  str                      # shown in the sidebar
    marker: str                      # identifies the supplier's own PDF (see detect_supplier)
    path:   pathlib.Path             # where parsed data is persisted
    parser: Callable[[bytes], dict]  # pdf bytes -> price data


SUPPLIERS: list[Supplier] = [
    Supplier(
        key="tatasteel",
        label="Tata Steel",
        marker="tatasteel",
        path=pathlib.Path("price_data_tatasteel.json"),
        parser=parse_tatasteel_pdf,
    ),
    Supplier(
        key="tibnor",
        label="Tibnor",
        marker="tibnor",
        path=pathlib.Path("price_data_tibnor.json"),
        parser=parse_tibnor_pdf,
    ),
]


def by_key(key: str) -> Supplier:
    """Look up a supplier by its key. Raises KeyError on an unknown key."""
    for supplier in SUPPLIERS:
        if supplier.key == key:
            return supplier
    raise KeyError(f"Unknown supplier key: {key!r}")


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_supplier(file_bytes: bytes) -> str | None:
    """Return the key of the supplier whose marker is in the first two pages.

    Text is lowercased with whitespace removed, so 'Tata Steel' matches
    'tatasteel'. "Stremet" is in both lists (it is the customer), so it is no
    marker. None when zero or both match: an ambiguous PDF is routed by the
    user, because prices saved under the wrong supplier corrupt quotes silently.
    """
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        raw = " ".join((page.extract_text() or "") for page in pdf.pages[:2])
    text = "".join(raw.lower().split())
    hits = [s.key for s in SUPPLIERS if s.marker in text]
    return hits[0] if len(hits) == 1 else None


def is_empty(parsed: dict) -> bool:
    """True when a parser found no rows: the wrong parser, or a changed layout."""
    return not any(rows for rows in parsed.values())


# ── Storage ───────────────────────────────────────────────────────────────────

# {path: (mtime, payload)} — the page reruns on every click, so a file is only
# re-read when it changes on disk.
_cache: dict[pathlib.Path, tuple[float, dict]] = {}


def load(supplier: Supplier) -> dict | None:
    """Return the stored payload for a supplier, or None if nothing saved yet."""
    if not supplier.path.exists():
        return None
    mtime = supplier.path.stat().st_mtime
    cached = _cache.get(supplier.path)
    if cached is None or cached[0] != mtime:
        with open(supplier.path, "r", encoding="utf-8") as f:
            cached = (mtime, json.load(f))
        _cache[supplier.path] = cached
    return cached[1]


def save(supplier: Supplier, data: dict, filename: str) -> dict:
    """Write parsed data to disk and return the payload that was written."""
    payload = {
        "data":        data,
        "source_file": filename,
        "updated_at":  datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(supplier.path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload
