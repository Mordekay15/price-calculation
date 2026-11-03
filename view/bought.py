"""Ostetut osat in the UI: the one table of bought parts (fasteners, pins,
machined parts…) that a part card (its own, per piece) and an assembly (per
assembly) both show and edit.

The rows are the owner's ``core.model.BoughtPart``s (``Part.bought``,
``Assembly.bought``): the table is drawn from them and its edits are written
back as BoughtParts. In the table a row is a dict with stable keys (``NAME``,
``KPL``, ``EUR``, ``FIT``, ``FROM``); the headers are labels only, so "Kpl /
osa" on a card and "Kpl / kokoonpano" on an assembly are the same column."""

from __future__ import annotations

import re

from core.model import BoughtPart
from core.work import FASTENER_INSERT_EUR, fastener_price
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


SEEDS = "bought_seeds"     # {table key: the rows its editor started from}


def fastener_parts(lines: list[tuple[str, int]], material: str | None, source: str = "",
                   per: int = 1) -> list[BoughtPart]:
    """The fasteners a drawing calls out (``(name, kpl)``), × ``per``, priced
    by the part's material."""
    return [BoughtPart(name, n * per, fastener_price(material), FASTENER_INSERT_EUR, source)
            for name, n in lines]


def body_part(name: str, kpl: int, source: str = "") -> BoughtPart:
    """A STEP body that isn't sheet: no price yet, its putting in priced
    when it is a fastener or a pin."""
    return BoughtPart(name, kpl, 0.0, FASTENER_INSERT_EUR if _FASTENER.search(name) else 0.0,
                      source)


def _rows(parts: list[BoughtPart]) -> list[dict]:
    return [{NAME: b.name, KPL: b.qty, EUR: b.eur, FIT: b.fit_eur, FROM: b.source}
            for b in parts]


def _parts(rows: list[dict]) -> list[BoughtPart]:
    """The table's rows as BoughtParts (empty rows left out)."""
    return [BoughtPart(str(r.get(NAME) or ""), int(r.get(KPL) or 0), float(r.get(EUR) or 0.0),
                       float(r.get(FIT) or 0.0), str(r.get(FROM) or ""))
            for r in rows if r.get(NAME) or r.get(EUR) or r.get(FIT)]


def editor(parts: list[BoughtPart], key: str, kpl_label: str) -> list[BoughtPart]:
    """The table of ``parts``; returns them as edited. The editor starts from
    a seed kept per table: Streamlit keeps the edits as changes to it, so it
    is only replaced together with the widget's state (``reset``). Headless
    the editor isn't drawn and forgets its edits, so it starts from them."""
    seeds = st.session_state.setdefault(SEEDS, {})
    if is_headless():
        seeds[key] = _rows(parts)
        return parts
    edited = st.data_editor(
        seeds.setdefault(key, _rows(parts)), key=key, num_rows="dynamic", hide_index=True,
        width="stretch", column_order=_COLUMNS,
        column_config={
            NAME: st.column_config.TextColumn("Nimike"),
            KPL: st.column_config.NumberColumn(kpl_label, min_value=0, step=1),
            EUR: st.column_config.NumberColumn("€/kpl", min_value=0.0, step=0.05, format="%.2f"),
            FIT: st.column_config.NumberColumn("Asennus (€/kpl)", min_value=0.0, step=0.05,
                                               format="%.2f"),
            FROM: st.column_config.TextColumn("Mistä", disabled=True)})
    return _parts(edited)


def reset(key: str) -> None:
    """The table starts again from its owner's BoughtParts, its edits
    dropped (rows were read from a file, or the owner is gone)."""
    st.session_state.get(SEEDS, {}).pop(key, None)
    st.session_state.pop(key, None)
