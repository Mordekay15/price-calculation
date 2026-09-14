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
from core.parser import parse_stremet_pdf, parse_tibnor_pdf


@dataclass(frozen=True)
class Supplier:
    key:    str                      # session-state / widget key prefix
    label:  str                      # shown in the sidebar
    path:   pathlib.Path             # where parsed data is persisted
    parser: Callable[[bytes], dict]  # pdf bytes -> price data


SUPPLIERS: list[Supplier] = [
    Supplier(
        key="stremet",
        label="Tata Steel",
        path=pathlib.Path("price_data.json"),
        parser=parse_stremet_pdf,
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
    """Return the stored payload for a supplier, or None if nothing saved yet."""
    if not supplier.path.exists():
        return None
    return _load_cached(str(supplier.path), supplier.path.stat().st_mtime)


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