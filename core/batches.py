"""Prices for several batch sizes on one quote (eräkoot), pure (no Streamlit).

The parts are nested once, at the quantities on their cards. Each batch size
``q`` (every part ordered ``q`` times) is then priced from that nesting:

- **Material.** A group needs ``sheets(q) = ceil(sheets × kg(q) / kg)`` whole
  sheets (at least one), where ``kg`` is the weight of the nested pieces and
  ``kg(q)`` that of ``q`` of each part. The group's sheets cost as much each as
  in the nesting, and the cost is shared by piece weight, as the summary does.
  At the nested quantities this gives exactly the nested price; at others it is
  an estimate with the nesting's scrap rate.
- **Work.** ``core.work.work_cost`` at ``q`` (the batch costs shared over
  ``q``), the cutting raised to the per-sheet minimum over ``sheets(q)``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from core.work import WorkCost, WorkInputs, WorkRates, apply_sheet_minimum, work_cost

DEFAULT_BATCH_SIZES = (1, 5, 10)


def parse_batch_sizes(text: str | None) -> list[int]:
    """``"1 5 10"``, ``"1, 5, 10"`` or ``"1·5·10"`` → ``[1, 5, 10]``: whole
    positive numbers, duplicates dropped, in increasing order."""
    return sorted({int(n) for n in re.findall(r"\d+", text or "") if int(n) > 0})


@dataclass(frozen=True)
class GroupSheets:
    """One material group as nested: its material € (margin included), the
    whole sheets it uses and the weight of the nested pieces."""

    total_eur: float
    sheets: int
    pieces_kg: float

    def sheets_for(self, pieces_kg: float) -> int:
        """Whole sheets for ``pieces_kg`` of pieces at the nesting's scrap rate."""
        if self.pieces_kg <= 0 or self.sheets <= 0:
            return 0
        return max(1, math.ceil(self.sheets * pieces_kg / self.pieces_kg - 1e-9))

    def eur_for(self, pieces_kg: float) -> float:
        """Material € for ``pieces_kg`` of pieces."""
        if self.sheets <= 0:
            return 0.0
        return self.total_eur * self.sheets_for(pieces_kg) / self.sheets


@dataclass(frozen=True)
class BatchPart:
    """One part as the batch prices need it: its group, one piece's weight
    and its work inputs (None: no work priced). ``per_unit`` is how many
    pieces one ordered item takes: 1 for a part sold on its own, its
    kpl/kokoonpano in an assembly (``core.assemblies``)."""

    id: str
    group: tuple
    kg: float
    work: WorkInputs | None = None
    per_unit: int = 1


@dataclass(frozen=True)
class BatchPrice:
    """One part's price at one batch size: ``qty`` pieces (the batch size ×
    the part's ``per_unit``). ``material`` and ``work`` are None when
    unpriced (no nesting for the group, or no laser speed)."""

    qty: int
    material: float | None        # € per piece
    work: WorkCost | None
    sheets: int = 0               # the group's sheets at this batch size

    @property
    def unit(self) -> float | None:
        """Price per piece: material + work."""
        if self.material is None or self.work is None or not self.work.priced:
            return None
        return self.material + self.work.per_piece

    @property
    def total(self) -> float | None:
        unit = self.unit
        return None if unit is None else unit * self.qty


def batch_prices(parts: list[BatchPart], groups: dict[tuple, GroupSheets],
                 sizes: list[int], rates: WorkRates) -> dict[str, list[BatchPrice]]:
    """Every part's price at every batch size, by part id, in ``sizes`` order.
    A part whose group was not nested gets no material price."""
    out: dict[str, list[BatchPrice]] = {p.id: [] for p in parts}
    by_group: dict[tuple, list[BatchPart]] = {}
    for p in parts:
        by_group.setdefault(p.group, []).append(p)

    for q in sizes:
        for key, members in by_group.items():
            group = groups.get(key)
            kg = sum(p.kg * p.per_unit for p in members) * q
            sheets = group.sheets_for(kg) if group else 0
            rate = group.eur_for(kg) / kg if group and kg > 0 else None
            works = [work_cost(p.work, q * p.per_unit, rates)
                     for p in members if p.work is not None]
            if group and works:
                works = apply_sheet_minimum(works, sheets, rates)
            work_of = dict(zip([p.id for p in members if p.work is not None], works))
            for p in members:
                material = None if rate is None else p.kg * rate
                out[p.id].append(BatchPrice(q * p.per_unit, material, work_of.get(p.id),
                                            sheets))
    return out

