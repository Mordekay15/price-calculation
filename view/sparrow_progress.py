"""Progress bar for a long Sparrow run: the work runs in a worker thread that
feeds a ``SparrowProgress`` tracker; the script thread redraws the bar."""

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
    bar = st.progress(0.0, text=f"Lasketaan: {label}…")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(fn, *args, on_progress=progress.event, **kwargs)
        while not wait([future], timeout=0.5).done:
            value, text = progress.snapshot()
            bar.progress(value, text=f"{label} — {text}")
        result = future.result()
    bar.empty()
    return result


class SparrowProgress:
    """Nesting progress, written by the Sparrow worker threads, read by the UI.

    The bar moves per sheet size; within a size it follows the placed parts.
    The per-sheet search (several Sparrow runs before the first sheet is
    settled) places nothing yet, so each run also eases the bar forward a
    little — it never sits still while Sparrow is working. Several sizes may
    be nested at once: the bar counts the finished ones and the ones under way.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._start = time.monotonic()
            self._runs = 0
            self._size_count = 1
            self._done: set[int] = set()
            self._active: dict[int, str] = {}       # index: "w×h", sizes under way
            self._since: dict[int, int] = {}        # index: runs when it started
            self._placed: dict[tuple, tuple[int, int]] = {}

    def run_started(self) -> None:
        with self._lock:
            self._runs += 1

    def event(self, kind: str, **kw) -> None:
        with self._lock:
            if kind == "size":
                self._size_count = kw["count"]
                self._active[kw["index"]] = f"{kw['w']}×{kw['h']}"
                self._since[kw["index"]] = self._runs
            elif kind == "size_done":
                self._done.add(kw["index"])
                self._active.pop(kw["index"], None)
                self._placed.pop((max(kw["w"], kw["h"]), min(kw["w"], kw["h"])), None)
            elif kind == "sheet":
                self._placed[(kw["w"], kw["h"])] = (kw["placed"], kw["total"])

    def snapshot(self) -> tuple[float, str]:
        """``(bar value 0–1, status text)``."""
        with self._lock:
            # Runs are shared by the sizes under way: each gets its share.
            n_active = max(1, len(self._active))
            within = 0.0
            for index in self._active:
                runs = (self._runs - self._since.get(index, 0)) / n_active
                within += min(0.9, 1 - 0.85 ** runs)
            done = sum(p / t for p, t in self._placed.values() if t)
            within = max(within, min(done, len(self._active)))
            value = min(0.99, (len(self._done) + within) / self._size_count)
            sizes = ", ".join(self._active[i] for i in sorted(self._active)) or "—"
            text = (f"levykoko {sizes} ({len(self._done)}/{self._size_count} valmis)"
                    f" · yritys {self._runs}")
            if self._placed:
                placed = min(p for p, _ in self._placed.values())
                total = next(iter(self._placed.values()))[1]
                text += f" · sijoitettu {placed}/{total} kpl"
            text += f" · {time.monotonic() - self._start:.0f} s"
        return value, text
