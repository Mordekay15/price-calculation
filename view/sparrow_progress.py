"""
view/sparrow_progress.py
========================
Progress bar for a long Sparrow nesting computation.

``run_with_progress`` runs the computation in a worker thread and, on the
Streamlit script thread, redraws a progress bar every half second from a
``SparrowProgress`` tracker: the sheet size being nested (i/n), the Sparrow run
count, parts placed so far and the elapsed time. The worker only feeds the
tracker (``run_started`` from the solver wrapper, ``event`` as the
computation's ``on_progress`` callback); every Streamlit call stays on the
script thread.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

import streamlit as st


def run_with_progress(progress: "SparrowProgress", label: str, fn, *args, **kwargs):
    """Call ``fn(*args, on_progress=progress.event, **kwargs)`` behind a progress bar.

    Returns ``fn``'s result (re-raising its exception); the bar is removed when
    it finishes.
    """
    progress.reset()
    bar = st.progress(0.0, text=f"Sparrow laskee: {label}…")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, *args, on_progress=progress.event, **kwargs)
        while not wait([future], timeout=0.5).done:
            value, text = progress.snapshot()
            bar.progress(value, text=f"{label} — {text}")
        result = future.result()
    bar.empty()
    return result


class SparrowProgress:
    """Nesting progress, written by the Sparrow worker thread, read by the UI.

    The bar moves per sheet size; within a size it follows the placed parts.
    The per-sheet search (several Sparrow runs before the first sheet is
    settled) places nothing yet, so each run also eases the bar forward a
    little — it never sits still while Sparrow is working.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._start = time.monotonic()
            self._runs = 0
            self._size_index, self._size_count, self._size = 0, 1, ""
            self._size_runs = 0
            self._placed: dict[tuple, tuple[int, int]] = {}

    def run_started(self) -> None:
        with self._lock:
            self._runs += 1
            self._size_runs += 1

    def event(self, kind: str, **kw) -> None:
        with self._lock:
            if kind == "size":
                self._size_index, self._size_count = kw["index"], kw["count"]
                self._size = f"{kw['w']}×{kw['h']}"
                self._size_runs = 0
                self._placed = {}
            elif kind == "sheet":
                self._placed[(kw["w"], kw["h"])] = (kw["placed"], kw["total"])

    def snapshot(self) -> tuple[float, str]:
        """``(bar value 0–1, status text)``."""
        with self._lock:
            done = min((p / t for p, t in self._placed.values() if t), default=0.0)
            within = max(done, min(0.9, 1 - 0.85 ** self._size_runs))
            value = min(0.99, (self._size_index + within) / self._size_count)
            text = (
                f"levykoko {self._size} ({self._size_index + 1}/{self._size_count})"
                f" · Sparrow-ajo {self._runs}"
            )
            if self._placed:
                placed = min(p for p, _ in self._placed.values())
                total = next(iter(self._placed.values()))[1]
                text += f" · sijoitettu {placed}/{total} kpl"
            text += f" · {time.monotonic() - self._start:.0f} s"
        return value, text
