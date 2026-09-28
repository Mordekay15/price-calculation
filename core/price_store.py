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

from core.price_parser import parse_tatasteel_pdf, parse_tibnor_pdf


@dataclass(frozen=True)
class Supplier:
    key:    str                      # session-state / widget key prefix
    label:  str                      # shown in the sidebar
    path:   pathlib.Path             # where parsed data is persisted
    parser: Callable[[bytes], dict]  # pdf bytes -> price data


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

def by_key(key: str) -> Supplier:
    """Look up a supplier by its key. Raises KeyError on an unknown key."""
    for supplier in SUPPLIERS:
        if supplier.key == key:
            return supplier
    raise KeyError(f"Unknown supplier key: {key!r}")
