"""Assemblies (kokoonpanot), pure (no Streamlit): parts that are sold
together as one item. An assembly is its parts, each with how many go into
one assembly, the bought parts it needs (screws, pins… with a price each) and
the work of putting it together.

On the quote an assembly is one line and a batch size ``q`` means ``q``
assemblies: each of its parts is priced at ``q × kpl / kokoonpano`` pieces
(``core.batches``), and one assembly costs

    Σ parts' €/kpl × kpl/kokoonpano + Σ bought parts + assembly work,

the work being ``(setup min / q + fitting min) / 60 × €/h``, plus welding
at the shop's welding rate and anything else per assembly."""

from __future__ import annotations

from dataclasses import dataclass

from core.work import WorkRates


@dataclass(frozen=True)
class AssemblyWork:
    """Putting one assembly together."""

    fit_min: float = 0.0          # fitting, minutes per assembly
    weld_m: float = 0.0           # welding, metres per assembly
    setup_min: float = 0.0        # minutes per batch
    eur_per_h: float = 60.0       # fitting and setup
    other_eur: float = 0.0        # per assembly (packing, painting the whole…)


@dataclass(frozen=True)
class BoughtPart:
    name: str
    qty: int                      # per assembly
    eur: float = 0.0              # one piece


@dataclass(frozen=True)
class Assembly:
    id: str
    name: str
    members: tuple[tuple[str, int], ...] = ()      # (part id, kpl / kokoonpano)
    bought: tuple[BoughtPart, ...] = ()
    work: AssemblyWork = AssemblyWork()


def work_eur(work: AssemblyWork, q: int, rates: WorkRates) -> float:
    """One assembly's work at a batch of ``q`` assemblies."""
    q = max(int(q), 1)
    minutes = work.setup_min / q + work.fit_min
    welding = (work.weld_m / rates.weld_m_per_h * rates.weld_eur_per_h
               if rates.weld_m_per_h else 0.0)
    return minutes / 60 * work.eur_per_h + welding + work.other_eur


@dataclass(frozen=True)
class AssemblyPrice:
    """One assembly's price at a batch of ``qty`` assemblies: its parts,
    bought parts and work, € per assembly. ``parts`` is None when one of its
    parts is unpriced."""

    qty: int
    parts: float | None
    bought: float = 0.0
    work: float = 0.0

    @property
    def unit(self) -> float | None:
        return None if self.parts is None else self.parts + self.bought + self.work

    @property
    def total(self) -> float | None:
        unit = self.unit
        return None if unit is None else unit * self.qty


def per_unit(assemblies: list[Assembly]) -> dict[str, int]:
    """Pieces of each part in one of every assembly (a part in two assemblies
    counts in both): what a batch size multiplies."""
    out: dict[str, int] = {}
    for a in assemblies:
        for pid, n in a.members:
            out[pid] = out.get(pid, 0) + int(n)
    return out


def assembly_prices(assembly: Assembly, part_units: dict[str, list[float | None]],
                    sizes: list[int], rates: WorkRates) -> list[AssemblyPrice]:
    """The assembly's price at each batch size. ``part_units`` is each part's
    €/kpl at each size (``BatchPrice.unit``), priced at ``q × kpl/kokoonpano``."""
    bought = sum(b.qty * b.eur for b in assembly.bought)
    out = []
    for i, q in enumerate(sizes):
        units = [(part_units.get(pid, [None] * len(sizes))[i], n)
                 for pid, n in assembly.members]
        parts = (None if any(u is None for u, _ in units)
                 else sum(u * n for u, n in units))
        out.append(AssemblyPrice(q, parts, bought, work_eur(assembly.work, q, rates)))
    return out
