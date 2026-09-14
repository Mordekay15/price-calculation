"""
core/parser
===========
Supplier PDF parsers — one module per supplier, plus shared helpers.

    common     cell cleaning, number conversion, thickness-range expansion
    tatasteel  Tata Steel monthly price list
    tibnor     Tibnor monthly price list
    detect     works out which supplier a PDF came from

Everything the rest of the app needs is re-exported here, so callers keep
using `from core.parser import parse_tibnor_pdf` and never import submodules
directly (except core.parser.detect, which the sidebar uses by name).
"""

from core.price_parser.helpers import (
    clean,
    expand_range_rows,
    expand_thickness,
    to_float,
)
from core.price_parser.detect import detect_supplier, is_empty
from core.price_parser.tatasteel import parse_tatasteel_pdf
from core.price_parser.tibnor import parse_tibnor_pdf

# Backwards-compatible aliases. Safe to drop once nothing references them.
# parse_stremet_pdf = parse_tatasteel_pdf
# parse_pdf = parse_tatasteel_pdf

__all__ = [
    "parse_tatasteel_pdf",
    "parse_tibnor_pdf",
    "parse_stremet_pdf",
    "parse_pdf",
    "detect_supplier",
    "is_empty",
    "clean",
    "to_float",
    "expand_thickness",
    "expand_range_rows",
]