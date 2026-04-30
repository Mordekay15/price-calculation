"""
core/export.py
==============
File export helpers.

To add a new export format (e.g. Excel):
  - Add a new function here and call it from the relevant view.
"""

import csv
import io


def rows_to_csv_bytes(rows: list[dict]) -> bytes:
    """Convert a list of dicts to a UTF-8 CSV byte string (Excel-safe BOM included)."""
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")
