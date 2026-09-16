"""
core/price_store.py
===================
Supplier registry and persistent storage for parsed price lists.

Each supplier's parsed data is written to its own JSON file so it survives
browser refreshes and server restarts. Re-upload only when the monthly price
list changes.
"""

import datetime
import json
import pathlib
from dataclasses import dataclass
from typing import Callable

import streamlit as st
from core.models import PriceRecord
from core.price_parser import parse_tatasteel_pdf, parse_tibnor_pdf


@dataclass(frozen=True)
class Supplier:
    key:    str                                  # session-state / widget key prefix
    label:  str                                  # shown in the sidebar
    path:   pathlib.Path                         # where parsed data is persisted
    parser: Callable[[bytes], list[PriceRecord]]  # pdf bytes -> price records


SUPPLIERS: list[Supplier] = [
    Supplier(
        key="tatasteel",
        label="Tata Steel",
        path=pathlib.Path("price_data_tatasteel.json"),
        parser=parse_tatasteel_pdf,
    ),
    Supplier(
        key="tibnor",
        label="Tibnor",
        path=pathlib.Path("price_data_tibnor.json"),
        parser=parse_tibnor_pdf,
    ),
]


@st.cache_resource
def _load_cached(path_str: str, mtime: float) -> dict | None:
    """Load JSON keyed by (path, mtime) so the cache auto-invalidates on save."""
    with open(path_str, "r", encoding="utf-8") as f:
        return json.load(f)


def load(supplier: Supplier) -> dict | None:
    """Return the stored payload for a supplier, or None if nothing saved yet.

    The payload carries the price list under "records" (a list of plain dicts,
    one per PriceRecord) plus the source filename and save time. Use
    `records_of()` to turn it back into PriceRecords.
    """
    if not supplier.path.exists():
        return None
    return _load_cached(str(supplier.path), supplier.path.stat().st_mtime)


def records_of(payload: dict | None) -> list[PriceRecord]:
    """Rebuild the PriceRecords from a stored payload.

    Returns [] for nothing stored, and also for a payload saved in the older
    wide-row format (no "records" key) — that price list simply needs a
    one-time re-upload to be saved in the current shape.
    """
    if not payload:
        return []
    return [PriceRecord.from_dict(d) for d in payload.get("records", [])]


def save(supplier: Supplier, records: list[PriceRecord], filename: str) -> dict:
    """Write a supplier's price records to disk and return the saved payload."""
    payload = {
        "records":     [r.to_dict() for r in records],
        "source_file": filename,
        "updated_at":  datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(supplier.path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload

def by_key(key: str) -> Supplier:
    """Look up a supplier by its key. Raises KeyError on an unknown key."""
    for supplier in SUPPLIERS:
        if supplier.key == key:
            return supplier
    raise KeyError(f"Unknown supplier key: {key!r}")


def delete(supplier: Supplier) -> None:
    """Remove a supplier's stored data. Used when correcting a misrouted upload."""
    supplier.path.unlink(missing_ok=True)