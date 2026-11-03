"""Ostetut osat in the UI: the one table of bought parts (fasteners, pins,
machined parts…) that a part card (its own, per piece) and an assembly (per
assembly) both show and edit.

A row is a dict with stable keys (``NAME``, ``KPL``, ``EUR``, ``FIT``,
``FROM``); the table's headers are labels only, so "Kpl / osa" on a card and
"Kpl / kokoonpano" on an assembly are the same column."""

from __future__ import annotations

import re

from core.work import FASTENER_INSERT_EUR, BoughtPart, fastener_price
from view.ui import is_headless, st

NAME, KPL, EUR, FIT, FROM = "name", "kpl", "eur", "fit", "from"
_COLUMNS = (NAME, KPL, EUR, FIT, FROM)

# Bought parts pressed or fitted into a sheet part: their putting in is priced
# at FASTENER_INSERT_EUR each by default.
_FASTENER = re.compile(r"PEM|MUTTER|RUUV|PULT|NIIT|HOLK|TAPP|NUT|SCREW|STUD|BOLT|RIVET|PIN",
                       re.IGNORECASE)

HELP = ("Valmiina ostettavat osat (puristemutterit, ruuvit, tapit, koneistetut osat…): "
        "(€/kpl + asennus) × kpl. €/kpl on ostohinta, Asennus (€/kpl) yhden kappaleen "
        "kiinnitys, esim. puristemutterin puristus. Lisää rivi tarvittaessa käsin.")


def fastener_rows(lines: list[tuple[str, int]], material: str | None, source: str = "",
                  per: int = 1) -> list[dict]:
    """Rows for the fasteners a drawing calls out (``(name, kpl)``), × ``per``,
    priced by the part's material."""
    return [{NAME: name, KPL: n * per, EUR: fastener_price(material), FIT: FASTENER_INSERT_EUR,
             FROM: source} for name, n in lines]


def body_row(name: str, kpl: int, source: str = "") -> dict:
    """A row for a STEP body that isn't sheet: no price yet, its putting in
    priced when it is a fastener or a pin."""
    return {NAME: name, KPL: kpl, EUR: 0.0,
            FIT: FASTENER_INSERT_EUR if _FASTENER.search(name) else 0.0, FROM: source}


def parts(rows: list[dict]) -> tuple[BoughtPart, ...]:
    """The rows as priced (empty rows left out)."""
    return tuple(BoughtPart(str(r.get(NAME) or ""), int(r.get(KPL) or 0),
                            float(r.get(EUR) or 0.0), float(r.get(FIT) or 0.0))
                 for r in rows if r.get(NAME) or r.get(EUR) or r.get(FIT))


def editor(own: dict, key: str, kpl_label: str) -> None:
    """The table, editing ``own["rows"]`` in place. ``own["seed"]`` is what the
    editor starts from: Streamlit keeps the edits as changes to it, so it is
    only replaced together with the widget's state (``reset``). Headless the
    editor isn't drawn and forgets its edits, so it starts from them."""
    if is_headless():
        own["seed"] = own.get("rows", own.get("seed", []))
        return
    edited = st.data_editor(
        own.get("seed", []), key=key, num_rows="dynamic", hide_index=True, width="stretch",
        column_order=_COLUMNS,
        column_config={
            NAME: st.column_config.TextColumn("Nimike"),
            KPL: st.column_config.NumberColumn(kpl_label, min_value=0, step=1),
            EUR: st.column_config.NumberColumn("€/kpl", min_value=0.0, step=0.05, format="%.2f"),
            FIT: st.column_config.NumberColumn("Asennus (€/kpl)", min_value=0.0, step=0.05,
                                               format="%.2f"),
            FROM: st.column_config.TextColumn("Mistä", disabled=True)})
    own["rows"] = [dict(r) for r in edited]


def reset(own: dict, rows: list[dict], key: str) -> None:
    """Start the table again from ``rows``, dropping the editor's edits."""
    own["rows"] = own["seed"] = rows
    st.session_state.pop(key, None)
