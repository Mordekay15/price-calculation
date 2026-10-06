"""Sparrow shape nesting, from a DXF part to a priced sheet size: parts, running
the solver, fixed-sheet packing (Sparrow only knows an endless strip), the
repeatable-program plan (``core.programs``) and costing. The solver is passed in as ``run_fn``, so tests need no binary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.dxf import DxfReport
from core.geometry import bbox, bbox_wh, net_area, rotate_translate, signed_area
from core.programs import choose_plans, kit_plan
from core.sheet_cost import (
    EdgeGaps,
    GroupCost,
    Packing,
    compute_options,
    too_big,
    usable_area,
)

Point = tuple[float, float]


# ── 1. Parts ──────────────────────────────────────────────────────────────────

# The rotations a part may take (degrees): the four quarter turns, unless its
# nesting angle is fixed (see ``part_from_report``).
ORIENTATIONS: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)

# The nesting angles a part card offers, counted from the part as drawn: 0
# (0/180) keeps the drawing's X axis along the sheet's long side, which is the
# rolling direction; 90 (90/270) lays it across.
NESTING_ANGLES: tuple[int, ...] = (0, 90)

# Points closer than this (mm) are treated as the same vertex when cleaning a
# ring. Guards against jagua-rs bailing on duplicate vertices, including f32
# round-off once the JSON is parsed by the Rust solver.
_MIN_VERTEX_GAP_MM = 1e-4


@dataclass
class SparrowPart:
    """One part ready to become a Sparrow item (millimetres).

    ``outer`` is the placement outline; ``holes`` are kept for the drawing and
    the real part area (Sparrow places a part by its outer boundary).
    ``construction`` holds the bend / reference lines drawn on the layout.
    ``orientations`` are the rotations Sparrow may give it.
    """

    part_id: str
    quantity: int
    outer: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    width_mm: float = 0.0
    height_mm: float = 0.0
    construction: list[list[Point]] = field(default_factory=list)
    orientations: tuple[float, ...] = ORIENTATIONS

    @property
    def turns(self) -> bool:
        """May the part take any quarter turn (its nesting angle is free)?"""
        return self.orientations == ORIENTATIONS

    def sheet_size(self) -> tuple[float, float]:
        """Its bounding box at its first allowed rotation, as it lies on the
        sheet (the other allowed rotation is half a turn: the same box)."""
        return bbox_wh(rotate_translate(self.outer, self.orientations[0], (0.0, 0.0)))

    def shape_dict(self) -> dict:
        """The jagua-rs ``shape`` object for this part."""
        outer = [list(p) for p in _clean_ring(self.outer)]
        inner = [[list(p) for p in ring] for ring in map(_clean_ring, self.holes)
                 if len(ring) >= 3]
        if not inner:
            return {"type": "simple_polygon", "data": outer}
        return {"type": "polygon", "data": {"outer": outer, "inner": inner}}


def part_from_report(report: DxfReport, quantity: int = 1,
                     angles: tuple[int, ...] = NESTING_ANGLES) -> SparrowPart:
    """The SparrowPart for a priceable DXF part, with its holes attached.

    ``angles`` are the allowed nesting angles (see ``NESTING_ANGLES``). With
    both the part takes any quarter turn; with one, only that angle and half a
    turn more, counted from the drawing: the turn that laid the part along its
    length is undone. Outer rings come out counter-clockwise and holes
    clockwise (standard convention; the solver re-derives winding, but this
    keeps the JSON tidy).
    """
    outline = report.outline
    orientations = ORIENTATIONS
    if set(angles) != set(NESTING_ANGLES):
        orientations = tuple(sorted({round((a + k - report.turned_deg) % 360.0, 6)
                                     for a in angles for k in (0, 180)}))
    return SparrowPart(
        part_id=_stem(report.name),
        quantity=int(quantity),
        outer=_oriented(outline.points, ccw=True),
        holes=[_oriented(h.points, ccw=False) for h in report.holes],
        width_mm=outline.width_mm,
        height_mm=outline.height_mm,
        construction=list(report.reference_lines),
        orientations=orientations,
    )


def _clean_ring(points: list[Point]) -> list[Point]:
    """A ring safe for jagua-rs: no repeated closing vertex, no vertex within
    ``_MIN_VERTEX_GAP_MM`` of the previous one. Order is preserved."""
    if not points:
        return []
    pts = [points[0]]
    for x, y in points[1:]:
        px, py = pts[-1]
        if abs(x - px) > _MIN_VERTEX_GAP_MM or abs(y - py) > _MIN_VERTEX_GAP_MM:
            pts.append((x, y))
    while len(pts) > 1:
        (x0, y0), (xn, yn) = pts[0], pts[-1]
        if abs(xn - x0) > _MIN_VERTEX_GAP_MM or abs(yn - y0) > _MIN_VERTEX_GAP_MM:
            break
        pts.pop()
    return pts


def _oriented(points: list[Point], *, ccw: bool) -> list[Point]:
    pts = _clean_ring(points)
    wrong_way = signed_area(pts) < 0 if ccw else signed_area(pts) > 0
    return pts[::-1] if wrong_way else pts


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.lower().endswith(".dxf"):
        base = base[:-4]
    return base or "part"


# ── 2. Running the solver ─────────────────────────────────────────────────────

# On Windows only the .exe is considered: bin/ also ships the Linux build as
# `bin/sparrow`, and Windows reports any existing file as "executable".
_EXECUTABLE_NAMES = ("sparrow.exe",) if os.name == "nt" else ("sparrow", "sparrow.exe")
_ENV_VAR = "SPARROW_BIN"
_BUNDLED_DIR = Path(__file__).resolve().parent.parent / "bin"

# Extra wall-clock seconds on top of Sparrow's own time limit before the
# process is killed (startup, I/O and the compression phase after exploration).
_WALL_CLOCK_MARGIN_SEC = 30


@dataclass
class SparrowResult:
    """Outcome of one Sparrow run."""

    ok: bool
    message: str = ""
    # (item_id, rotation_deg, (tx, ty)) for every placed item
    placements: list[tuple[int, float, tuple[float, float]]] = field(default_factory=list)
    strip_width: float | None = None     # length of the nested strip


def find_executable() -> str | None:
    """The Sparrow binary: ``$SPARROW_BIN``, then bundled ``bin/``, then PATH."""
    candidates = [os.environ.get(_ENV_VAR), *(str(_BUNDLED_DIR / n) for n in _EXECUTABLE_NAMES),
                  *_EXECUTABLE_NAMES]
    for cand in filter(None, candidates):
        if Path(cand).is_file() and os.access(cand, os.X_OK):
            return cand
        if found := shutil.which(cand):
            return found
    return None


def run_sparrow(
    instance: dict,
    *,
    executable: str,
    time_limit_sec: int = 4,
    seed: int = 0,
    min_item_separation: float | None = None,
) -> SparrowResult:
    """Run Sparrow on one strip instance in a temporary folder.

    The process gets an argument list (never a shell string), a time limit and
    a fixed seed. Sparrow writes ``output/final_<name>.json`` next to its input.
    """
    job = Path(tempfile.mkdtemp(prefix="sparrow_job_"))
    try:
        (job / "input.json").write_text(json.dumps(instance), encoding="utf-8")
        args = [executable, "-i", str(job / "input.json"),
                "-t", str(int(time_limit_sec)), "-s", str(int(seed)), "-x"]
        if min_item_separation is not None:
            args += ["--min-item-separation", str(float(min_item_separation))]
        wall_clock = int(time_limit_sec) + _WALL_CLOCK_MARGIN_SEC
        try:
            proc = subprocess.run(args, cwd=job, capture_output=True, text=True,
                                  timeout=wall_clock, check=False)
        except subprocess.TimeoutExpired:
            return SparrowResult(False, f"Sparrow ei valmistunut {wall_clock} sekunnissa ja keskeytettiin.")
        except OSError as exc:
            return SparrowResult(False, f"Sparrowin käynnistys epäonnistui: {exc}")
        if proc.returncode != 0:
            return SparrowResult(False, _error_message(proc.stderr) or "Sparrow päättyi virheeseen.")
        outputs = sorted((job / "output").glob("final_*.json"))
        if not outputs:
            return SparrowResult(False, "Sparrow ei kirjoittanut final_*.json -tulostiedostoa.")
        try:
            solution = json.loads(outputs[0].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return SparrowResult(False, f"Tulos-JSONia ei voitu lukea: {exc}")
        return _result(instance, solution)
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _result(instance: dict, solution: dict) -> SparrowResult:
    """Read a Sparrow output JSON; not ok unless every requested item was placed.

    The output is the instance echoed back plus a ``solution`` block:
    ``{"strip_width", "layout": {"placed_items": [...]}, ...}``.
    """
    sol = solution.get("solution", solution)
    layout = sol.get("layout", {}) if isinstance(sol, dict) else {}
    placements = []
    for pi in layout.get("placed_items", []) if isinstance(layout, dict) else []:
        transf = pi.get("transformation", {}) or {}
        tx, ty = transf.get("translation", [0.0, 0.0])
        placements.append((int(pi["item_id"]), float(transf.get("rotation", 0.0)),
                           (float(tx), float(ty))))
    if not placements:
        return SparrowResult(False, "Sparrow ei löytänyt ratkaisua (ei sijoitettuja osia).")
    requested = {int(i["id"]): int(i.get("demand", 0)) for i in instance.get("items", [])}
    placed = Counter(p[0] for p in placements)
    if placed != requested:
        return SparrowResult(False, f"Sijoitettu määrä ({sum(placed.values())}) ei vastaa "
                                    f"pyydettyä ({sum(requested.values())}).")
    return SparrowResult(True, "", placements, sol.get("strip_width"))


def _error_message(stderr: str) -> str:
    """The ``Error: <msg>`` line Sparrow prints before its backtrace, or the first
    line that is not part of a backtrace."""
    lines = [s.strip() for s in (stderr or "").splitlines()]
    for s in lines:
        if s.startswith("Error:"):
            return s[len("Error:"):].strip()[:300]
    for s in lines:
        if s and not s.startswith(("Stack backtrace", "at ")) and not s.split(":", 1)[0].isdigit():
            return s[:300]
    return ""


# ── 3. Fixed-sheet packing ────────────────────────────────────────────────────
#
# Sparrow minimises the length of an open strip; it does not pack into a fixed
# W×H sheet. greedy_fixed_sheets fills one sheet at a time. Each round:
#
#   1. search for the most parts one sheet holds. A probe nests n parts in a
#      strip as high as the sheet and keeps the parts that land fully inside
#      the first sheet_w of length (no part straddles the cut). The first probe
#      nests the whole remaining order: a layout known to work, but often loose
#      — Sparrow spreads its effort over the long strip (400 parts: 18 on the
#      sheet where 26 fit). The strip's length tells how densely Sparrow packed
#      overall, so the next probe asks for that many on a single sheet; if they
#      fit it keeps stepping up (+1, +2, +4 …), after the first miss it bisects;
#   2. repeat that sheet layout for as many sheets as the remaining demand still
#      covers (100 parts at 25 per sheet → one layout ×4);
#   3. subtract those and start the next sheet with the smaller leftover.

# Tolerance (mm) for the "inside the sheet" test — absorbs solver float round-off
# and the tiny overhang a separation gap can introduce.
_TOL = 0.5


@dataclass
class Placed:
    """One part placed on a sheet, with its transformed geometry (mm)."""

    part_index: int                 # index into the parts list passed in
    part_id: str
    rotation_deg: float
    translation: tuple[float, float]
    outer: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    construction: list[list[Point]] = field(default_factory=list)


@dataclass
class PackedSheet:
    """One sheet layout; ``count`` identical sheets use it. ``used_area`` is the
    real part area on one sheet (holes subtracted)."""

    placements: list[Placed]
    sheet_w: float
    sheet_h: float
    used_area: float
    count: int = 1


@dataclass
class PackResult:
    """Outcome of packing one sheet size."""

    ok: bool
    sheets: list[PackedSheet] = field(default_factory=list)
    reason: str = ""

    @property
    def sheets_needed(self) -> int:
        return sum(s.count for s in self.sheets)

    @property
    def used_area(self) -> float:
        """Real part area over every physical sheet."""
        return sum(s.used_area * s.count for s in self.sheets)


def greedy_fixed_sheets(
    parts: list[SparrowPart],
    sheet_w: float,
    sheet_h: float,
    *,
    run_fn,
    seed: int = 0,
    time_limit_sec: int = 4,
    separation: float | None = None,
    max_sheets: int = 400,
    on_sheet=None,
) -> PackResult:
    """Nest every part's ``quantity`` onto fixed ``sheet_w`` × ``sheet_h`` sheets.

    ``on_sheet(placed, total)``, if given, is called after each sheet layout is
    settled — for a progress display. Not ok (with a reason) when a part is
    too large for the sheet, the solver fails, or the sheets exceed ``max_sheets``.
    """
    if sheet_w <= 0 or sheet_h <= 0:
        return PackResult(ok=False, reason="virheellinen levykoko")

    remaining = [int(p.quantity) for p in parts]
    reason = too_big([(p.part_id, *p.sheet_size(), p.turns) for p in parts if p.quantity > 0],
                     sheet_w, sheet_h, _TOL)
    if reason:
        return PackResult(ok=False, reason=reason)

    n_total = sum(remaining)
    sheets: list[PackedSheet] = []
    while any(q > 0 for q in remaining):
        if sum(s.count for s in sheets) >= max_sheets:
            return PackResult(ok=False, sheets=sheets, reason="liian monta levyä")

        def probe(n: int):
            return _probe(parts, _mix(remaining, n), sheet_w, sheet_h, run_fn=run_fn,
                          seed=seed, time_limit_sec=time_limit_sec, separation=separation)

        total = sum(remaining)
        err, best, strip_len = probe(total)
        if err is not None:
            return PackResult(ok=False, sheets=sheets, reason=err)
        if not best:
            # Nothing fit within the sheet although each part fits in isolation —
            # usually the separation gap makes even one part overflow.
            return PackResult(ok=False, sheets=sheets,
                              reason="osat eivät mahtuneet levylle annetulla rankavälillä")

        # lo: most parts seen on one sheet; hi: smallest probe that didn't fit
        # entirely (None until one misses). Try the strip-density estimate,
        # step up from there, then bisect.
        lo, hi, step = len(best), None, 1
        guess = int(total * sheet_w / strip_len) if strip_len else 0
        while lo < total and (hi is None or hi - lo > 1):
            stepping = False
            if guess > lo:
                n, guess = min(total, guess), 0
            elif hi is None:
                n, stepping = min(total, lo + step), True
            else:
                n = (lo + hi) // 2
            err, kept, _ = probe(n)
            if err is not None:
                break  # keep the best sheet found so far
            if len(kept) > lo:
                lo, best = len(kept), kept
            if len(kept) < n:
                hi = n
            elif stepping:
                step *= 2

        per_sheet = Counter(pl.part_index for pl in best)
        # Reuse this layout while the remaining demand still covers its part mix.
        count = min(remaining[i] // n for i, n in per_sheet.items())
        for i, n in per_sheet.items():
            remaining[i] -= count * n
        sheets.append(_sheet(best, sheet_w, sheet_h, count))
        if on_sheet is not None:
            on_sheet(n_total - sum(remaining), n_total)

    return PackResult(ok=True, sheets=sheets)


def _sheet(placements: list[Placed], sheet_w: float, sheet_h: float,
           count: int = 1) -> PackedSheet:
    return PackedSheet(placements, sheet_w, sheet_h,
                       sum(net_area(pl.outer, pl.holes) for pl in placements), count)


def _mix(remaining: list[int], n: int) -> dict[int, int]:
    """Pick ``n`` parts from the remaining demand, in proportion to it, as
    ``{part_index: count}`` — a proportional mix keeps a layout repeatable."""
    total = sum(remaining)
    if n >= total:
        return {i: q for i, q in enumerate(remaining) if q > 0}
    raw = {i: q * n / total for i, q in enumerate(remaining) if q > 0}
    mix = {i: int(r) for i, r in raw.items()}
    short = n - sum(mix.values())
    for i in sorted(raw, key=lambda i: raw[i] - mix[i], reverse=True)[:short]:
        mix[i] += 1
    return {i: c for i, c in mix.items() if c > 0}


def _probe(parts, demand: dict[int, int], sheet_w: float, sheet_h: float,
           *, run_fn, seed, time_limit_sec, separation):
    """Nest ``demand`` in a strip ``sheet_h`` high.

    Returns ``(error, placed_on_sheet, strip_len)``: error is None on success;
    the list holds the parts that land fully inside the first ``sheet_w``.

    Sparrow keeps ``separation`` from the strip's edges as well as between
    parts. The gap is meant only between parts, so the strip is padded by it
    on every side and the result moved back: a part may touch the sheet edge.
    """
    pad = separation or 0.0
    active = list(demand)
    instance = _build_instance(parts, demand, sheet_h + 2 * pad)
    res = run_fn(instance, seed=seed, time_limit_sec=time_limit_sec, separation=separation)
    if not res.ok:
        return res.message or "Sparrow-ajo epäonnistui", [], None

    left = dict(demand)
    kept: list[Placed] = []
    for local_id, rot, trans in res.placements:
        if local_id < 0 or local_id >= len(active):
            continue
        orig_i = active[local_id]
        part = parts[orig_i]
        trans = (trans[0] - pad, trans[1] - pad)
        outer = rotate_translate(part.outer, rot, trans)
        minx, miny, maxx, maxy = bbox(outer)
        if minx < -_TOL or miny < -_TOL or maxx > sheet_w + _TOL or maxy > sheet_h + _TOL:
            continue  # spills past this sheet
        if left[orig_i] <= 0:
            continue
        kept.append(Placed(orig_i, part.part_id, rot, trans, outer,
                           [rotate_translate(h, rot, trans) for h in part.holes],
                           [rotate_translate(c, rot, trans) for c in part.construction]))
        left[orig_i] -= 1
    return None, kept, res.strip_width - 2 * pad


def _build_instance(parts, demand: dict[int, int], strip_height: float) -> dict:
    """A jagua-rs strip instance for ``demand``. Items are 0-based in ``demand``
    order, so a solution's ``item_id`` indexes straight back into ``list(demand)``."""
    items = []
    for local_id, (orig_i, n) in enumerate(demand.items()):
        part = parts[orig_i]
        item = {"id": local_id, "demand": int(n), "part_id": part.part_id,
                "allowed_orientations": list(part.orientations)}
        item["shape"] = part.shape_dict()
        items.append(item)
    return {"name": "pack", "strip_height": float(strip_height), "items": items}


# ── 4. Costing ────────────────────────────────────────────────────────────────

def sparrow_options(
    lookup: dict,
    material: str,
    thickness: str,
    thickness_mm: float,
    parts: list[SparrowPart],
    *,
    run_fn,
    margin_pct: float = 0.0,
    edges: EdgeGaps = EdgeGaps(),
    rankavali_mm: int = 0,
    seed: int = 0,
    time_limit_sec: int = 4,
    on_progress=None,
) -> GroupCost | None:
    """``core.sheet_cost.compute_options`` with Sparrow shape nesting.

    ``on_progress``, if given, receives ``("size", index=, count=, w=, h=)``
    before each sheet size and ``("sheet", w=, h=, placed=, total=)`` each time
    a sheet layout is settled.
    """
    n_pieces = sum(p.quantity for p in parts)
    part_area_mm2 = sum(net_area(p.outer, p.holes) * p.quantity for p in parts)
    separation = float(rankavali_mm) if rankavali_mm else None

    def pack_fn(sw: int, sh: int) -> list[Packing]:
        return _pack_on_short_side(
            parts, sw, sh, edges,
            run_fn=run_fn, seed=seed, time_limit_sec=time_limit_sec,
            separation=separation, on_progress=on_progress,
        )

    return compute_options(
        lookup, material, thickness, thickness_mm,
        n_pieces=n_pieces, part_area_mm2=part_area_mm2, pack=pack_fn,
        margin_pct=margin_pct, on_progress=on_progress,
    )


def _pack_on_short_side(parts, sw, sh, edges, *, run_fn, seed, time_limit_sec,
                        separation, on_progress=None) -> list[Packing]:
    """Pack the sheet one way only: Sparrow's fixed strip height is the sheet's
    short side and the strip runs along the long side (e.g. 1000 high, up to
    2000 long on a 1000 × 2000 sheet), both less the edge gaps.

    Returns the fewest-sheets packing and, when it is worth showing, the
    repeatable-program one (see ``core.programs``)."""
    long_side, short_side = max(sw, sh), min(sw, sh)
    x0, y0, ew, eh = usable_area(sw, sh, edges)
    on_sheet = None
    if on_progress is not None:
        on_sheet = lambda placed, total: on_progress(  # noqa: E731
            "sheet", w=long_side, h=short_side, placed=placed, total=total)
    pack = greedy_fixed_sheets(parts, ew, eh, run_fn=run_fn, seed=seed,
                               time_limit_sec=time_limit_sec, separation=separation,
                               on_sheet=on_sheet)

    def packing(sheets: list[PackedSheet]) -> Packing:
        return Packing(sheets=sheets, sheets_needed=sum(s.count for s in sheets),
                       eff_w=ew, eff_h=eh, draw_w=long_side, draw_h=short_side,
                       failed=0 if pack.ok else 1, reason=pack.reason, x0=x0, y0=y0)

    if not pack.ok:
        return [packing(pack.sheets)]
    kit = _kit(parts, pack.sheets, ew, eh, run_fn=run_fn, seed=seed,
               time_limit_sec=time_limit_sec, separation=separation)
    return [packing(sheets) for sheets in choose_plans(pack.sheets, kit)]


# Sparrow runs spent looking for a one-program kit on one sheet size; each run
# takes the search time.
_KIT_BUDGET = 4


def _kit(parts, greedy_sheets, sheet_w, sheet_h, *, run_fn, seed, time_limit_sec,
         separation) -> list[PackedSheet] | None:
    """``core.programs.kit_plan`` with Sparrow: a kit fits when one probe puts
    every piece of it on the sheet; the remainder is packed greedily."""
    def fits_kit(kit):
        demand = {i: n for i, n in enumerate(kit) if n > 0}
        err, kept, _ = _probe(parts, demand, sheet_w, sheet_h, run_fn=run_fn, seed=seed,
                              time_limit_sec=time_limit_sec, separation=separation)
        if err is not None or len(kept) < sum(kit):
            return None
        return _sheet(kept, sheet_w, sheet_h)

    def pack_rest(rest):
        pack = greedy_fixed_sheets(
            [replace(p, quantity=q) for p, q in zip(parts, rest)], sheet_w, sheet_h,
            run_fn=run_fn, seed=seed, time_limit_sec=time_limit_sec, separation=separation)
        return pack.sheets if pack.ok else None

    return kit_plan([p.quantity for p in parts], greedy_sheets, fits_kit, pack_rest,
                    budget=_KIT_BUDGET)
