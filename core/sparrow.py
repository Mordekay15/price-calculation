"""Sparrow shape nesting, from a DXF part to a priced sheet size: parts, running
the solver, fixed-sheet packing (Sparrow only knows an endless strip; the part
mixes per sheet come from ``core.patterns``) and costing. The solver is passed
in as ``run_fn``, so tests need no binary."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.dxf import DxfReport
from core.geometry import bbox, bbox_wh, net_area, rotate_translate, signed_area
from core.patterns import CoverItem, Library, Mix, Pattern, cheapest_cover, search_patterns, within
from core.rect_nesting import pack as box_pack
from core.sheet_cost import (
    GroupCost,
    Packing,
    SheetOption,
    combo_option,
    compute_options,
    effective_sheet,
)

Point = tuple[float, float]


# ── 1. Parts ──────────────────────────────────────────────────────────────────

# Default rotations offered to the packer (degrees). Four quadrant orientations
# suit rectangular-ish sheet-metal parts.
DEFAULT_ORIENTATIONS: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)

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
    """

    part_id: str
    quantity: int
    outer: list[Point]
    holes: list[list[Point]] = field(default_factory=list)
    allowed_orientations: tuple[float, ...] = DEFAULT_ORIENTATIONS
    width_mm: float = 0.0
    height_mm: float = 0.0
    construction: list[list[Point]] = field(default_factory=list)

    def shape_dict(self) -> dict:
        """The jagua-rs ``shape`` object for this part."""
        outer = [list(p) for p in _clean_ring(self.outer)]
        inner = [[list(p) for p in ring] for ring in map(_clean_ring, self.holes)
                 if len(ring) >= 3]
        if not inner:
            return {"type": "simple_polygon", "data": outer}
        return {"type": "polygon", "data": {"outer": outer, "inner": inner}}


def part_from_report(
    report: DxfReport,
    quantity: int = 1,
    *,
    allowed_orientations: tuple[float, ...] = DEFAULT_ORIENTATIONS,
) -> SparrowPart:
    """The SparrowPart for a priceable DXF part, with its holes attached.

    Outer rings come out counter-clockwise and holes clockwise (standard
    convention; the solver re-derives winding, but this keeps the JSON tidy).
    """
    outline = report.outline
    return SparrowPart(
        part_id=_stem(report.name),
        quantity=int(quantity),
        outer=_oriented(outline.points, ccw=True),
        holes=[_oriented(h.points, ccw=False) for h in report.holes],
        allowed_orientations=tuple(float(a) for a in allowed_orientations),
        width_mm=outline.width_mm,
        height_mm=outline.height_mm,
        construction=list(report.reference_lines),
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
    time_limit_sec: int = 10,
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
# W×H sheet. A mix of parts *fits* a sheet when Sparrow nests it in a strip as
# high as the sheet and every part lands within the first sheet_w of length.
#
#   0. a mix whose bounding boxes already fit (core.rect_nesting) needs no
#      Sparrow run at all — Sparrow is only asked about tighter, interlocking mixes;
#   1. one strip run of the whole order seeds the search: every sheet_w window
#      of that strip is a layout known to fit;
#   2. core.patterns.search_patterns asks Sparrow about a few more mixes (e.g.
#      4 big + 4 small), each answer settling many other mixes;
#   3. core.patterns.cheapest_cover repeats the best layouts until the order
#      is covered (100 parts at 25 per sheet → one layout ×4).
#
# SheetNester keeps every answer for the whole group, so the next sheet size
# or orientation reuses it instead of running Sparrow again.

# Tolerance (mm) for the "inside the sheet" test — absorbs solver float round-off
# and the tiny overhang a separation gap can introduce.
_TOL = 0.5

# Sparrow runs per sheet size after the seeding strip run.
_PROBE_BUDGET = 8

# Every part fits alone, yet nothing fits: usually the separation gap makes
# even one part overflow.
_NO_FIT = "osat eivät mahtuneet levylle annetulla rankavälillä"


@dataclass(frozen=True)
class NestSettings:
    """How every Sparrow run is made."""

    seed: int = 0
    time_limit_sec: int = 8
    separation: float | None = None


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


class SheetNester:
    """Nests one group's parts onto sheets of any size.

    Every answer is remembered and reused for the other sheet sizes and
    orientations: a mix that fit a W×H sheet fits any sheet at least as wide
    and high, a mix that missed misses on any sheet no larger, and sheets as
    high share one strip run of the whole order.
    """

    def __init__(self, parts: list[SparrowPart], run_fn, settings: NestSettings = NestSettings()):
        self.parts = parts
        self.run_fn = run_fn
        self.settings = settings
        self._lock = threading.Lock()
        self._fits: list[tuple[float, float, Mix, list[Placed]]] = []
        self._misses: list[tuple[float, float, Mix]] = []
        self._strips: dict[float, tuple] = {}

    def pack(self, sheet_w: float, sheet_h: float) -> tuple[list[PackedSheet], str]:
        """Every part's ``quantity`` on ``sheet_w`` × ``sheet_h`` sheets:
        ``(sheets, reason)``, the reason empty on success, else why not
        (a part too large, a solver error…)."""
        patterns, reason = self.library(sheet_w, sheet_h)
        if reason:
            return [], reason
        cover = cheapest_cover(self.demand(), {"sheet": Library(1.0, patterns)},
                               shares=_areas(self.parts))
        if cover is None:
            return [], _NO_FIT
        return [_packed_sheet(item, sheet_w, sheet_h) for item in cover], ""

    def library(self, sheet_w: float, sheet_h: float) -> tuple[tuple[Pattern, ...], str]:
        """The largest part mixes found to fit one sheet, each with its layout;
        ``(patterns, reason)`` with a reason instead when nothing can be nested."""
        for p in self.parts:
            w, h = bbox_wh(p.outer)
            if p.quantity > 0 and not _fits(w, h, sheet_w, sheet_h, p.allowed_orientations):
                return (), f"osa {p.part_id} ei mahdu levylle ({w:.0f}×{h:.0f} mm)"
        demand = self.demand()
        fitted, layout = self._without_sparrow(sheet_w, sheet_h, demand) or (None, None)
        if fitted == demand:
            return (Pattern(demand, layout),), ""
        err, seeds = self._seeds(sheet_w, sheet_h)
        if err:
            return (), err
        shares = tuple(a / (sheet_w * sheet_h) for a in _areas(self.parts))
        patterns = search_patterns(demand, lambda mix: self._fit(sheet_w, sheet_h, mix),
                                   shares=shares, budget=_PROBE_BUDGET, seeds=seeds)
        return (tuple(patterns), "") if patterns else ((), _NO_FIT)

    def demand(self) -> Mix:
        return tuple(int(p.quantity) for p in self.parts)

    def _seeds(self, sheet_w: float, sheet_h: float):
        """``(error, patterns)`` from one strip run of the whole order, shared by
        every sheet as high: each ``sheet_w`` window of the strip fits one sheet."""
        with self._lock:
            strip = self._strips.get(sheet_h)
        if strip is None:
            strip = _strip(self.parts, self.demand(), sheet_h, self.run_fn, self.settings)
            with self._lock:
                self._strips[sheet_h] = strip
        err, placed, strip_len = strip
        windows = [_window(placed, k * sheet_w, sheet_w, sheet_h)
                   for k in range(math.ceil((strip_len or 0) / sheet_w))]
        return err, tuple(Pattern(_mix_of(w, len(self.parts)), w) for w in windows)

    def _fit(self, sheet_w: float, sheet_h: float, mix: Mix) -> tuple[Mix, list[Placed]]:
        """The oracle of ``search_patterns``: what of ``mix`` fits one sheet.
        Sparrow runs only when ``_without_sparrow`` cannot tell."""
        answer = self._without_sparrow(sheet_w, sheet_h, mix)
        if answer is not None:
            return answer
        err, placed, _ = _strip(self.parts, mix, sheet_h, self.run_fn, self.settings)
        layout = [] if err else _window(placed, 0.0, sheet_w, sheet_h)
        fitted = _mix_of(layout, len(self.parts))
        with self._lock:
            if any(fitted):
                self._fits.append((sheet_w, sheet_h, fitted, layout))
            if fitted != mix:
                self._misses.append((sheet_w, sheet_h, mix))
        return fitted, layout

    def _without_sparrow(self, sheet_w: float, sheet_h: float, mix: Mix):
        """``(fitted, layout)`` from an earlier answer or the bounding boxes
        alone; None when only Sparrow can tell."""
        known = self._known(sheet_w, sheet_h, mix)
        if known is not None:
            return known
        layout = _box_layout(self.parts, mix, sheet_w, sheet_h, self.settings.separation or 0.0)
        return None if layout is None else (mix, layout)

    def _known(self, sheet_w: float, sheet_h: float, mix: Mix):
        """``(fitted, layout)`` when an earlier answer settles ``mix``, else None."""
        with self._lock:
            smaller = [(fitted, layout) for w, h, fitted, layout in self._fits
                       if w <= sheet_w + _TOL and h <= sheet_h + _TOL]
            missed = any(w >= sheet_w - _TOL and h >= sheet_h - _TOL and within(m, mix)
                         for w, h, m in self._misses)
        for fitted, layout in smaller:
            if within(mix, fitted):
                return mix, _cut(layout, mix)
        if not missed:
            return None
        # A known miss: answer with the largest known fit inside it, as Sparrow did.
        fitted, layout = max(((f, lay) for f, lay in smaller if within(f, mix)),
                             key=lambda fl: sum(fl[0]), default=((0,) * len(mix), []))
        return fitted, _cut(layout, fitted)


def _strip(parts, mix, strip_h: float, run_fn, settings: NestSettings):
    """Nest ``mix`` in a strip ``strip_h`` high: ``(error, placed, strip_len)``,
    error None on success."""
    active = [i for i, n in enumerate(mix) if n > 0]
    instance = _build_instance(parts, {i: mix[i] for i in active}, strip_h)
    res = run_fn(instance, seed=settings.seed, time_limit_sec=settings.time_limit_sec,
                 separation=settings.separation)
    if not res.ok:
        return res.message or "Sparrow-ajo epäonnistui", [], None
    placed = [_placed(parts, active[local_id], rot, trans)
              for local_id, rot, trans in res.placements if 0 <= local_id < len(active)]
    return None, placed, res.strip_width


def _box_layout(parts, mix, sheet_w: float, sheet_h: float, gap: float) -> list[Placed] | None:
    """``mix`` on one sheet by bounding boxes alone (0° / 90°), no Sparrow run;
    None when the boxes do not fit. Boxes grow by ``gap``, so the sheet does
    too: the last part in a row needs no gap after it."""
    pieces = [(i, c, *(d + gap for d in bbox_wh(parts[i].outer)))
              for i, n in enumerate(mix) for c in range(n)]
    sheets, failed = box_pack(pieces, sheet_w + gap, sheet_h + gap, allow_rotation=True)
    if failed or len(sheets) != 1:
        return None
    layout = []
    for box in sheets[0].placements:
        rot = 90.0 if box.rotated else 0.0
        minx, miny, _, _ = bbox(rotate_translate(parts[box.product_idx].outer, rot, (0.0, 0.0)))
        layout.append(_placed(parts, box.product_idx, rot, (box.x - minx, box.y - miny)))
    return layout


def _placed(parts, index: int, rot: float, trans: tuple[float, float]) -> Placed:
    part = parts[index]
    return Placed(index, part.part_id, rot, trans, rotate_translate(part.outer, rot, trans),
                  [rotate_translate(h, rot, trans) for h in part.holes],
                  [rotate_translate(c, rot, trans) for c in part.construction])


def _window(placed: list[Placed], x0: float, sheet_w: float, sheet_h: float) -> list[Placed]:
    """The parts lying fully in ``[x0, x0 + sheet_w]`` of a strip, moved to x = 0."""
    kept = []
    for pl in placed:
        minx, miny, maxx, maxy = bbox(pl.outer)
        if minx >= x0 - _TOL and maxx <= x0 + sheet_w + _TOL and miny >= -_TOL \
                and maxy <= sheet_h + _TOL:
            kept.append(_shifted(pl, -x0))
    return kept


def _shifted(pl: Placed, dx: float) -> Placed:
    if dx == 0:
        return pl
    move = lambda ring: [(x + dx, y) for x, y in ring]  # noqa: E731
    tx, ty = pl.translation
    return Placed(pl.part_index, pl.part_id, pl.rotation_deg, (tx + dx, ty), move(pl.outer),
                  [move(h) for h in pl.holes], [move(c) for c in pl.construction])


def _cut(layout: list[Placed], mix: Mix) -> list[Placed]:
    """The first ``mix[i]`` parts of each type in ``layout`` (dropping parts
    from a valid layout keeps it valid)."""
    left = list(mix)
    kept = []
    for pl in layout:
        if left[pl.part_index] > 0:
            left[pl.part_index] -= 1
            kept.append(pl)
    return kept


def _mix_of(placed: list[Placed], n_parts: int) -> tuple[int, ...]:
    counts = Counter(pl.part_index for pl in placed)
    return tuple(counts[i] for i in range(n_parts))


def _areas(parts) -> tuple[float, ...]:
    return tuple(net_area(p.outer, p.holes) for p in parts)


def _packed_sheet(item: CoverItem, sheet_w: float, sheet_h: float) -> PackedSheet:
    """A cover item as a sheet: the pattern's layout cut down to the mix used."""
    placements = _cut(item.pattern.layout, item.mix)
    used = sum(net_area(pl.outer, pl.holes) for pl in placements)
    return PackedSheet(placements, sheet_w, sheet_h, used, item.count)


def _build_instance(parts, demand: dict[int, int], strip_height: float) -> dict:
    """A jagua-rs strip instance for ``demand``. Items are 0-based in ``demand``
    order, so a solution's ``item_id`` indexes straight back into ``list(demand)``."""
    items = []
    for local_id, (orig_i, n) in enumerate(demand.items()):
        part = parts[orig_i]
        item = {"id": local_id, "demand": int(n), "part_id": part.part_id}
        if part.allowed_orientations:
            item["allowed_orientations"] = [float(a) for a in part.allowed_orientations]
        item["shape"] = part.shape_dict()
        items.append(item)
    return {"name": "pack", "strip_height": float(strip_height), "items": items}


def _fits(w: float, h: float, sw: float, sh: float, orients) -> bool:
    """True if a w×h part fits an sw×sh sheet in some allowed orientation."""
    can_swap = not orients or any(round(float(a) / 90.0) % 2 == 1 for a in orients)
    return (w <= sw + _TOL and h <= sh + _TOL) or (can_swap and h <= sw + _TOL and w <= sh + _TOL)


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
    long_side_clamp_mm: int = 0,
    rankavali_mm: int = 0,
    seed: int = 0,
    time_limit_sec: int = 8,
    on_progress=None,
) -> GroupCost | None:
    """``core.sheet_cost.compute_options`` with Sparrow shape nesting.

    ``on_progress``, if given, receives ``("size", index=, count=, w=, h=)``
    before each sheet size and ``("sheet", w=, h=, placed=, total=)`` once a
    sheet size is nested in one orientation.
    """
    n_pieces = sum(p.quantity for p in parts)
    part_area_mm2 = sum(net_area(p.outer, p.holes) * p.quantity for p in parts)
    settings = NestSettings(seed, time_limit_sec, float(rankavali_mm) if rankavali_mm else None)

    nester = SheetNester(parts, run_fn, settings)

    def pack_fn(sw: int, sh: int) -> Packing:
        best, alt = _pack_best_orientation(nester, sw, sh, long_side_clamp_mm, on_progress)
        best.alt = alt
        return best

    result = compute_options(
        lookup, material, thickness, thickness_mm,
        n_pieces=n_pieces, part_area_mm2=part_area_mm2, pack=pack_fn,
        margin_pct=margin_pct, on_progress=on_progress, skip_dearer=True,
    )
    combo = _combo(nester, result, part_area_mm2) if result is not None else None
    if combo is not None:
        result.options.append(combo)
    return result


def _combo(nester: SheetNester, result: GroupCost, part_area_mm2: float) -> SheetOption | None:
    """Sheets of several sizes (e.g. the leftover on a smaller sheet) when that
    beats every single size; built from the sizes already nested, so it needs
    no new Sparrow run."""
    singles = [o for o in result.options if o.ok]
    libraries = {}
    for i, o in enumerate(singles):
        patterns, reason = nester.library(o.packing.eff_w, o.packing.eff_h)
        if not reason:
            libraries[i] = Library(o.total_eur / o.sheets_needed, patterns, o.sw * o.sh)
    cover = cheapest_cover(nester.demand(), libraries, shares=_areas(nester.parts))
    used = sorted({item.key for item in cover or []},
                  key=lambda i: -singles[i].sw * singles[i].sh)      # biggest sheet first
    if len(used) < 2:
        return None
    parts = []
    for i in used:
        o = singles[i]
        sheets = [_packed_sheet(item, o.packing.eff_w, o.packing.eff_h)
                  for item in cover if item.key == i]
        packing = replace(o.packing, sheets=sheets, alt=None,
                          sheets_needed=sum(s.count for s in sheets))
        parts.append((o, packing))
    combo = combo_option(parts, part_area_mm2=part_area_mm2, pieces_kg=result.pieces_kg)
    return combo if combo.total_eur < min(o.total_eur for o in singles) - 1e-6 else None


def _pack_best_orientation(nester: SheetNester, sw, sh, clamp, on_progress=None) -> tuple[Packing, Packing | None]:
    """Pack the sheet both ways round (portrait / landscape); return ``(best, alt)``.

    ``best`` has the fewest sheets (on a tie the long-side strip) and drives
    the price; ``alt`` is the other orientation's real re-nest, for the "turn
    the sheet" view (None when square, skipped or not fitting). When neither
    fits, ``(first_attempt, None)`` is returned to surface the failure.

    The turned sheet is skipped when every part may turn a quarter: its layout
    would just be the first one rotated. Otherwise both nest at the same time —
    Sparrow's time limit is wall-clock, so the pair takes about as long as one.
    """
    def attempt(cw, ch) -> Packing:
        ew, eh = effective_sheet(cw, ch, clamp)
        sheets, reason = nester.pack(ew, eh)
        if on_progress is not None:
            total = sum(p.quantity for p in nester.parts)
            on_progress("sheet", w=cw, h=ch, placed=0 if reason else total, total=total)
        return Packing(sheets=sheets, sheets_needed=sum(s.count for s in sheets),
                       eff_w=ew, eff_h=eh, draw_w=cw, draw_h=ch,
                       failed=1 if reason else 0, reason=reason)

    # Sparrow's fixed strip height is the sheet's short side; the strip runs
    # along the long side (listed first, so it also wins a tie).
    long_side, short_side = max(sw, sh), min(sw, sh)
    if sw == sh or _quarter_turn_free(nester.parts):
        options = [attempt(long_side, short_side)]
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            options = list(pool.map(lambda d: attempt(*d),
                                    [(long_side, short_side), (short_side, long_side)]))

    ok = [o for o in options if not o.failed]
    if not ok:
        return options[0], None
    best = min(ok, key=lambda o: o.sheets_needed)
    return best, next((o for o in ok if o is not best), None)


def _quarter_turn_free(parts) -> bool:
    """True if every part's allowed rotations are closed under +90° (empty =
    free rotation) — then any layout turned 90° is still valid."""
    for p in parts:
        if not p.allowed_orientations:
            continue
        angles = {round(float(a)) % 360 for a in p.allowed_orientations}
        if any((a + 90) % 360 not in angles for a in angles):
            return False
    return True
