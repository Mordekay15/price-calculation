"""Assemblies (kokoonpanot), pure (no Streamlit): parts that are sold
together as one item. An assembly is its parts, each with how many go into
one assembly, the bought parts it needs (screws, pins… with a price each) and
the work of putting it together.

On the quote an assembly is one line and a batch size ``q`` means ``q``
assemblies: each of its parts is priced at ``q × kpl / kokoonpano`` pieces
(``core.batches``), and one assembly costs

    Σ parts' €/kpl × kpl/kokoonpano + Σ bought parts (price + putting in)
    + assembly work,

the work being ``(setup min / q + fitting min) / 60 × €/h``, plus welding
at the shop's welding rate and anything else per assembly.

Assemblies nest: an assembly's ``children`` are subassemblies (by id, × kpl),
priced first and added to it like parts. Only an assembly that is no other
one's child is a quote line; a subassembly's parts and work are priced at
``q × how many of it go into one top assembly``."""

from __future__ import annotations

from dataclasses import dataclass

from core.work import BoughtPart, WorkRates  # noqa: F401  (BoughtPart: re-exported)


@dataclass(frozen=True)
class AssemblyWork:
    """Putting one assembly together."""

    fit_min: float = 0.0          # fitting, minutes per assembly
    weld_m: float = 0.0           # welding, metres per assembly
    setup_min: float = 0.0        # minutes per batch
    eur_per_h: float = 0.0        # fitting and setup
    other_eur: float = 0.0        # per assembly (packing, painting the whole…)


@dataclass(frozen=True)
class Assembly:
    id: str
    name: str
    members: tuple[tuple[str, int], ...] = ()      # (part id, kpl / kokoonpano)
    bought: tuple[BoughtPart, ...] = ()
    work: AssemblyWork = AssemblyWork()
    children: tuple[tuple[str, int], ...] = ()     # (subassembly id, kpl / kokoonpano)


def fit_eur(work: AssemblyWork, q: int) -> float:
    """Fitting one assembly, its batch's setup shared over ``q`` assemblies."""
    return (work.setup_min / max(int(q), 1) + work.fit_min) / 60 * work.eur_per_h


def work_eur(work: AssemblyWork, q: int, rates: WorkRates) -> float:
    """One assembly's work at a batch of ``q`` assemblies."""
    return fit_eur(work, q) + rates.weld_eur(work.weld_m) + work.other_eur


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


def _children(assemblies: list[Assembly]) -> dict[str, list[tuple[str, int]]]:
    """Each assembly's subassemblies, those making a loop (or not there) left
    out, so the tree can be walked."""
    ids = {a.id for a in assemblies}
    kids = {a.id: [(c, int(n)) for c, n in a.children if c in ids and n > 0]
            for a in assemblies}

    def reaches(start: str, goal: str, seen: set[str]) -> bool:
        if start == goal:
            return True
        seen.add(start)
        return any(reaches(c, goal, seen) for c, _ in kids[start] if c not in seen)

    out: dict[str, list[tuple[str, int]]] = {aid: [] for aid in kids}
    for a in assemblies:
        for c, n in kids[a.id]:
            if not reaches(c, a.id, set()) and not _below(out, c, a.id):
                out[a.id].append((c, n))
    return out


def _below(kids: dict[str, list[tuple[str, int]]], top: str, aid: str) -> bool:
    return any(c == aid or _below(kids, c, aid) for c, _ in kids.get(top, []))


def tops(assemblies: list[Assembly]) -> list[Assembly]:
    """The assemblies that are no other one's subassembly: the quote lines."""
    kids = _children(assemblies)
    inner = {c for cs in kids.values() for c, _ in cs}
    return [a for a in assemblies if a.id not in inner]


def counts(assemblies: list[Assembly]) -> dict[str, int]:
    """How many of each assembly go into one of every top assembly (1 for a
    top one; a subassembly in two counts in both)."""
    kids = _children(assemblies)
    out: dict[str, int] = {}

    def walk(aid: str, n: int) -> None:
        out[aid] = out.get(aid, 0) + n
        for c, k in kids[aid]:
            walk(c, n * k)
    for a in tops(assemblies):
        walk(a.id, 1)
    return out


def per_unit(assemblies: list[Assembly]) -> dict[str, int]:
    """Pieces of each part in one of every top assembly, through its
    subassemblies (a part in two assemblies counts in both): what a batch
    size multiplies."""
    n_of = counts(assemblies)
    out: dict[str, int] = {}
    for a in assemblies:
        for pid, n in a.members:
            out[pid] = out.get(pid, 0) + int(n) * n_of.get(a.id, 0)
    return out


def tree_prices(assemblies: list[Assembly], part_units: dict[str, list[float | None]],
                sizes: list[int], rates: WorkRates) -> dict[str, list[AssemblyPrice]]:
    """Every assembly's price at each batch size of top assemblies,
    subassemblies first: one of them at ``q × counts`` is a part of its
    parent. ``part_units`` as for ``assembly_prices``."""
    kids, n_of = _children(assemblies), counts(assemblies)
    by_id = {a.id: a for a in assemblies}
    out: dict[str, list[AssemblyPrice]] = {}

    def price(aid: str) -> list[AssemblyPrice]:
        if aid not in out:
            subs = {c: [p.unit for p in price(c)] for c, _ in kids[aid]}
            out[aid] = assembly_prices(by_id[aid], {**part_units, **subs},
                                       [q * max(n_of.get(aid, 1), 1) for q in sizes], rates,
                                       children=kids[aid])
        return out[aid]
    for a in assemblies:
        price(a.id)
    return out


def assembly_prices(assembly: Assembly, part_units: dict[str, list[float | None]],
                    sizes: list[int], rates: WorkRates,
                    children: list[tuple[str, int]] | None = None) -> list[AssemblyPrice]:
    """The assembly's price at each batch size. ``part_units`` is each part's
    €/kpl at each size (``BatchPrice.unit``), priced at ``q × kpl/kokoonpano``,
    and each subassembly's (``children``, by default the assembly's own)."""
    bought = sum(b.qty * b.unit for b in assembly.bought)
    rows = [*assembly.members, *(assembly.children if children is None else children)]
    out = []
    for i, q in enumerate(sizes):
        units = [(part_units.get(pid, [None] * len(sizes))[i], n) for pid, n in rows]
        parts = (None if any(u is None for u, _ in units)
                 else sum(u * n for u, n in units))
        out.append(AssemblyPrice(q, parts, bought, work_eur(assembly.work, q, rates)))
    return out
