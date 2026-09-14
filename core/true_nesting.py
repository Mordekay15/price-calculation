"""
core/true_nesting.py
====================
Shape-aware ("tight") nesting for DXF parts.

Bounding-box packing (core/nesting.py) treats every part as its rectangle, so
concave parts waste the space in their notches. This module packs the *real*
shapes so parts interlock — a rotated cross drops into the gap between its
neighbours, exactly like a proper nesting program.

How it works (a raster bottom-left-fill heuristic):
  1. Each part is rasterised into a boolean mask at a chosen resolution, once per
     allowed rotation angle. A kerf (rankaväli) dilates the mask so parts keep
     their distance.
  2. Pieces are placed largest-first. For each piece we test every rotation and
     drop it into the lowest-then-leftmost free spot on the sheet where its mask
     doesn't collide with anything already placed. Collision is checked for all
     positions at once with an FFT cross-correlation, so a full sheet scan is
     fast.
  3. When nothing fits, a new sheet is started.

It's a deterministic heuristic, not an optimal nester, but it captures the big
win — interlocking — and runs in pure NumPy/Pillow, so it deploys anywhere.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw


# ── Data types ──────────────────────────────────────────────────────────────

@dataclass
class Placed:
    part_idx: int
    angle: float
    x_mm: float
    y_mm: float
    polylines: list[list[tuple[float, float]]]  # rotated + normalised, for drawing
    w_mm: float
    h_mm: float
    area_mm2: float


@dataclass
class TightSheet:
    w: int
    h: int
    placed: list[Placed] = field(default_factory=list)

    @property
    def used_area(self) -> float:
        return sum(p.area_mm2 for p in self.placed)

    @property
    def utilization(self) -> float:
        total = self.w * self.h
        return self.used_area / total if total else 0.0


@dataclass
class TightResult:
    sheets: list[TightSheet]
    failed: int


# ── Rasterisation ───────────────────────────────────────────────────────────

def _rotate(polylines, angle_deg):
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    return [[(x * ca - y * sa, x * sa + y * ca) for x, y in poly] for poly in polylines]


def _normalise(polylines):
    xs = [x for poly in polylines for x, _ in poly]
    ys = [y for poly in polylines for _, y in poly]
    minx, miny = min(xs), min(ys)
    norm = [[(x - minx, y - miny) for x, y in poly] for poly in polylines]
    return norm, (max(xs) - minx), (max(ys) - miny)


def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """Diamond dilation by r cells — grows the mask to enforce a kerf gap."""
    for _ in range(r):
        m = mask.copy()
        m[1:, :]  |= mask[:-1, :]
        m[:-1, :] |= mask[1:, :]
        m[:, 1:]  |= mask[:, :-1]
        m[:, :-1] |= mask[:, 1:]
        mask = m
    return mask


def rasterize(polylines, angle_deg: float, res: float, kerf_mm: float):
    """Return (mask, rotated_normalised_polylines, w_mm, h_mm, area_mm2).

    The mask is a boolean grid at `res` mm per cell, filled solid (enclosed
    holes included — we never nest into a hole), dilated by the kerf.
    """
    rot = _rotate(polylines, angle_deg)
    norm, w_mm, h_mm = _normalise(rot)

    pad = 2
    W = int(math.ceil(w_mm / res)) + 1 + 2 * pad
    H = int(math.ceil(h_mm / res)) + 1 + 2 * pad

    img = Image.new("L", (W, H), 0)
    dr = ImageDraw.Draw(img)
    for poly in norm:
        px = [(x / res + pad, y / res + pad) for x, y in poly]
        if len(px) >= 2:
            dr.line(px, fill=255, width=2)

    # Flood the exterior from a corner; whatever it can't reach is the solid part.
    ImageDraw.floodfill(img, (0, 0), 128)
    arr = np.array(img)
    solid = arr != 128

    kerf_cells = int(round((kerf_mm / 2.0) / res))
    if kerf_cells > 0:
        solid = _dilate(solid, kerf_cells)

    area_mm2 = float(solid.sum()) * res * res
    # Trim the padding so the mask origin aligns with the part origin.
    solid = solid[pad:pad + int(math.ceil(h_mm / res)) + 1,
                  pad:pad + int(math.ceil(w_mm / res)) + 1]
    return solid, norm, w_mm, h_mm, area_mm2


# ── Collision + placement ───────────────────────────────────────────────────

def _first_free(occ: np.ndarray, mask: np.ndarray):
    """Lowest-then-leftmost (row, col) where mask fits with no overlap, or None."""
    H, W = occ.shape
    mh, mw = mask.shape
    if mh > H or mw > W:
        return None
    size = (H + mh - 1, W + mw - 1)
    fa = np.fft.rfft2(occ.astype(np.float64), s=size)
    fb = np.fft.rfft2(mask[::-1, ::-1].astype(np.float64), s=size)
    full = np.fft.irfft2(fa * fb, s=size)
    valid = full[mh - 1:H, mw - 1:W]
    free = np.rint(valid) < 0.5
    if not free.any():
        return None
    rows = np.where(free.any(axis=1))[0]
    r0 = int(rows[0])
    c0 = int(np.where(free[r0])[0][0])
    return r0, c0


def nest(
    parts: list[dict],
    sheet_w: int,
    sheet_h: int,
    res: float = 2.0,
    angles: tuple[float, ...] = (0.0, 90.0),
    kerf_mm: float = 0.0,
    long_side_clamp_mm: int = 0,
) -> TightResult:
    """Nest all copies of every part onto sheets of sheet_w × sheet_h.

    `parts` is a list of {"idx": int, "polylines": [...], "qty": int}. Returns a
    TightResult with one TightSheet per sheet used, each holding the placed
    shapes (with rotation and position for drawing).
    """
    # Pre-rasterise every (part, angle).
    masks: dict[tuple[int, float], tuple] = {}
    pieces: list[tuple[float, int]] = []  # (area, part_idx) — for ordering
    for part in parts:
        pidx = part["idx"]
        base_area = None
        for ang in angles:
            mask, norm, w_mm, h_mm, area = rasterize(part["polylines"], ang, res, kerf_mm)
            masks[(pidx, ang)] = (mask, norm, w_mm, h_mm, area)
            base_area = area if base_area is None else base_area
        for _ in range(int(part["qty"])):
            pieces.append((base_area, pidx))

    # Largest first.
    pieces.sort(key=lambda t: -t[0])

    H = int(math.ceil(sheet_h / res))
    W = int(math.ceil(sheet_w / res))
    clamp_cells = int(round(long_side_clamp_mm / res)) if long_side_clamp_mm else 0

    def new_occ() -> np.ndarray:
        occ = np.zeros((H, W), dtype=bool)
        if clamp_cells > 0:
            if sheet_w >= sheet_h:      # claw on the long (horizontal) edge → eat rows top+...
                occ[H - clamp_cells:, :] = True
            else:
                occ[:, W - clamp_cells:] = True
        return occ

    sheets: list[TightSheet] = []
    occs: list[np.ndarray] = []
    failed = 0

    for _area, pidx in pieces:
        placed_ok = False
        # Try existing sheets first, then a fresh one.
        for si, occ in enumerate(occs):
            spot = _place_on(occ, masks, pidx, angles)
            if spot is not None:
                _commit(sheets[si], occ, masks, pidx, spot, res)
                placed_ok = True
                break
        if placed_ok:
            continue

        occ = new_occ()
        spot = _place_on(occ, masks, pidx, angles)
        if spot is None:
            failed += 1  # doesn't fit even on an empty sheet in any rotation
            continue
        sheet = TightSheet(sheet_w, sheet_h)
        sheets.append(sheet)
        occs.append(occ)
        _commit(sheet, occ, masks, pidx, spot, res)

    return TightResult(sheets=sheets, failed=failed)


def _place_on(occ, masks, pidx, angles):
    """Best (lowest-leftmost across rotations) spot for a part on one sheet."""
    best = None  # (row, col, angle)
    for ang in angles:
        mask = masks[(pidx, ang)][0]
        spot = _first_free(occ, mask)
        if spot is None:
            continue
        r0, c0 = spot
        if best is None or (r0, c0) < (best[0], best[1]):
            best = (r0, c0, ang)
    return best


def _commit(sheet: TightSheet, occ, masks, pidx, spot, res):
    r0, c0, ang = spot
    mask, norm, w_mm, h_mm, area = masks[(pidx, ang)]
    mh, mw = mask.shape
    occ[r0:r0 + mh, c0:c0 + mw] |= mask
    sheet.placed.append(Placed(
        part_idx=pidx,
        angle=ang,
        x_mm=c0 * res,
        y_mm=r0 * res,
        polylines=norm,
        w_mm=w_mm,
        h_mm=h_mm,
        area_mm2=area,
    ))
