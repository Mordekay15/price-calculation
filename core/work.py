"""Work pricing, pure (no Streamlit): cutting, bending, threads, fasteners, welding,
subcontracting and setup, priced per piece.

A piece's price is ``c + F / kpl``: ``c`` is what every piece costs (its
material and its work), ``F`` what the batch costs once (setup and
programming), shared over the quantity. Work carries no margin: the rates here are
selling prices. Cutting is at least ``min_cut_eur_per_sheet`` per sheet used
(see ``apply_sheet_minimum``)."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, replace

LASER, PUNCH = "laser", "punch"

# ── Laser cutting speeds ──────────────────────────────────────────────────────
#
# Stremet's table (Platino 6 kW and Genius 6 kW): seconds to cut 1 m, the mean
# of three cutting technologies, and the price of one laser second. Each
# material family has one or two sections (nitrogen and oxygen cutting); a
# section is (€/s, [(thickness mm, s/m), …]).

_STEEL = (                  # KY-VA, KU-SI, KU-VA, SÄ-SI, ALUZ, KROMI
    (0.045, [(1, 7), (2, 7), (3, 12), (4, 16), (5, 19), (6, 22)]),      # typpi
    (0.05,  [(8, 37), (10, 45), (12, 50), (15, 77), (20, 85)]),         # happi
)
_STAINLESS = (              # RST, HST
    (0.045, [(1, 8), (2, 13), (3, 17), (4, 17), (5, 35), (6, 35), (8, 46), (10, 85)]),
)
_ALUMINIUM = (
    (0.05, [(1, 4), (2, 11), (3, 20), (4, 22), (5, 24), (6, 50), (8, 50), (10, 65)]),
)
_COPPER = (                 # kupari, messinki
    (0.05, [(1, 5), (2, 6), (3, 8), (4, 21), (5, 68), (6, 68)]),
)


FASTENER_INSERT_EUR = 0.20


def fastener_price(material: str | None) -> float:
    """Default price of one fastener (press nut, bolt…), without putting it in.
    With ``FASTENER_INSERT_EUR`` it makes 1.00 € in stainless (RST / HST) and
    0.25 € otherwise, Stremet's usual minimums."""
    return 0.80 if re.search(r"\b(rst|hst)\b", (material or "").lower()) else 0.05


def laser_family(material: str | None) -> tuple[str, tuple]:
    """``(name, sections)`` of the laser table for a material name."""
    name = (material or "").lower()
    if "alumiini" in name:
        return "alumiini", _ALUMINIUM
    if "kupari" in name or "messinki" in name:
        return "kupari / messinki", _COPPER
    if re.search(r"\b(rst|hst)\b", name):
        return "RST / HST", _STAINLESS
    return "teräs", _STEEL


@dataclass(frozen=True)
class LaserSpeed:
    s_per_m: float
    eur_per_s: float


def laser_speed(material: str | None, thickness_mm: float | None) -> LaserSpeed | None:
    """Seconds per metre and €/s from the table, None when the thickness is
    thicker than the table goes. Between two rows of one section the time is
    interpolated; between two sections (nitrogen ends, oxygen starts) the
    thicker one's row is used, and a sheet thinner than the table takes its
    first row."""
    if thickness_mm is None:
        return None
    rows = [(t, s, rate) for rate, section in laser_family(material)[1]
            for t, s in section]
    thicknesses = [t for t, _, _ in rows]
    i = bisect.bisect_left(thicknesses, thickness_mm - 1e-9)
    if i == len(rows):
        return None
    t1, s1, rate1 = rows[i]
    if i == 0 or abs(t1 - thickness_mm) < 1e-9:
        return LaserSpeed(s1, rate1)
    t0, s0, rate0 = rows[i - 1]
    if rate0 != rate1:
        return LaserSpeed(s1, rate1)
    return LaserSpeed(s0 + (s1 - s0) * (thickness_mm - t0) / (t1 - t0), rate1)


# ── Rates and inputs ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WorkRates:
    """The shop's rates, set in the sidebar."""

    eur_per_hit: float = 0.008            # levari, one hit
    min_cut_eur_per_sheet: float = 7.0    # cutting, at least this per sheet
    weld_m_per_h: float = 1.5
    weld_eur_per_h: float = 60.0
    # Setup per batch by the number of work phases: 1, 2, 3 or more.
    setup_eur: tuple[float, float, float] = (40.0, 60.0, 80.0)

    def weld_eur(self, metres: float) -> float:
        """Welding ``metres`` at the shop's speed and hourly rate."""
        return metres / self.weld_m_per_h * self.weld_eur_per_h if self.weld_m_per_h else 0.0


@dataclass(frozen=True)
class WorkInputs:
    """One part's work, as set on its card. Lengths are per piece;
    ``programming_eur`` and ``setup_eur`` are per batch. ``setup_eur`` None
    means the default by phases (``default_setup``)."""

    method: str = LASER
    cut_length_m: float = 0.0
    laser: LaserSpeed | None = None       # None: no speed known, laser unpriced
    hits: int = 0
    bends: int = 0
    eur_per_bend: float = 0.20
    threads: int = 0
    eur_per_thread: float = 0.20
    fasteners: int = 0                    # press nuts, bolts… (kiinnikkeet)
    eur_per_fastener: float = 0.05        # one fastener
    eur_per_insert: float = FASTENER_INSERT_EUR   # putting one in
    weld_m: float = 0.0
    subcontract_eur: float = 0.0          # per piece
    programming_eur: float = 0.0
    setup_eur: float | None = None


def phases(inp: WorkInputs) -> int:
    """Work phases the part goes through: cutting, plus each of bending,
    threading, fastening, welding and subcontracting that it has."""
    return 1 + sum(bool(x) for x in (inp.bends, inp.threads, inp.fasteners, inp.weld_m,
                                      inp.subcontract_eur))


def default_setup(inp: WorkInputs, rates: WorkRates) -> float:
    return rates.setup_eur[min(phases(inp), 3) - 1]


# ── Costs ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WorkCost:
    """One part's work. The first five are € per piece; ``setup``
    and ``programming`` € per batch. ``cut_minimum`` is what the
    per-sheet minimum added to the cutting, per piece."""

    qty: int
    cutting: float = 0.0
    bending: float = 0.0
    threads: float = 0.0
    fasteners: float = 0.0
    welding: float = 0.0
    subcontract: float = 0.0
    setup: float = 0.0
    programming: float = 0.0
    cut_minimum: float = 0.0
    cut_seconds: float = 0.0              # laser, one piece
    priced: bool = True                   # False: laser speed unknown

    @property
    def batch_fixed(self) -> float:
        """F: what the batch costs once."""
        return self.setup + self.programming

    @property
    def per_piece(self) -> float:
        """Work per piece, the batch costs shared over the quantity."""
        return (self.cutting + self.cut_minimum + self.bending + self.threads + self.fasteners
                + self.welding + self.subcontract
                + self.batch_fixed / self.qty)

    @property
    def batch(self) -> float:
        return self.per_piece * self.qty

    def breakdown(self) -> dict[str, float]:
        """The work per piece by phase (``BREAKDOWN`` names them), the batch
        costs left out; the cutting includes the per-sheet minimum."""
        return dict(zip(BREAKDOWN, (
            self.cutting + self.cut_minimum, self.bending, self.threads, self.fasteners,
            self.welding, self.subcontract)))


# The phases of ``WorkCost.breakdown``, as the tables and exports name them.
BREAKDOWN = ("Leikkaus", "Taivutus", "Kierteet", "Kiinnikkeet", "Hitsaus", "Alihankinta")


def work_cost(inp: WorkInputs, qty: int, rates: WorkRates) -> WorkCost:
    """What ``qty`` pieces' work costs, before the per-sheet cutting minimum."""
    qty = max(int(qty), 1)
    laser = inp.laser if inp.method == LASER else None
    setup = default_setup(inp, rates) if inp.setup_eur is None else inp.setup_eur
    return WorkCost(
        qty=qty,
        cutting=cutting_eur(inp, inp.method, rates) or 0.0,
        bending=inp.bends * inp.eur_per_bend,
        threads=inp.threads * inp.eur_per_thread,
        fasteners=inp.fasteners * (inp.eur_per_fastener + inp.eur_per_insert),
        welding=rates.weld_eur(inp.weld_m),
        subcontract=inp.subcontract_eur,
        setup=setup,
        programming=inp.programming_eur,
        cut_seconds=inp.cut_length_m * laser.s_per_m if laser else 0.0,
        priced=not (inp.method == LASER and inp.laser is None and inp.cut_length_m),
    )


def apply_sheet_minimum(costs: list[WorkCost], sheets: int,
                        rates: WorkRates) -> list[WorkCost]:
    """``costs`` of the parts cut from the same ``sheets``, with the cutting
    raised to at least ``min_cut_eur_per_sheet`` per sheet. What is missing is
    shared in proportion to each part's cutting cost (by piece count when
    none has any)."""
    floor = rates.min_cut_eur_per_sheet * sheets
    total = sum(c.cutting * c.qty for c in costs)
    if not costs or total >= floor:
        return list(costs)
    missing = floor - total
    if total > 0:
        return [replace(c, cut_minimum=missing * c.cutting / total) for c in costs]
    pieces = sum(c.qty for c in costs)
    return [replace(c, cut_minimum=missing / pieces) for c in costs]


def rect_cut_length_m(width_mm: float, height_mm: float) -> float:
    """A rectangle's outline in metres."""
    return 2 * (width_mm + height_mm) / 1000


def cutting_eur(inp: WorkInputs, method: str, rates: WorkRates) -> float | None:
    """One piece's cutting by ``method``, None when it can't be priced that
    way: laser needs a speed and a length, the turret punch a number of hits."""
    if method == PUNCH:
        return inp.hits * rates.eur_per_hit if inp.hits else None
    if inp.laser is None or not inp.cut_length_m:
        return None
    return inp.cut_length_m * inp.laser.s_per_m * inp.laser.eur_per_s


def cheaper_method(inp: WorkInputs, rates: WorkRates) -> tuple[str, float] | None:
    """``(method, saving €/kpl)`` when the other cutting method prices lower
    than the chosen one, else None (also when either can't be priced)."""
    other = LASER if inp.method == PUNCH else PUNCH
    chosen, alt = cutting_eur(inp, inp.method, rates), cutting_eur(inp, other, rates)
    if chosen is None or alt is None or alt >= chosen - 1e-9:
        return None
    return other, chosen - alt
