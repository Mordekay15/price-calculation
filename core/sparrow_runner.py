"""
core/sparrow_runner.py
======================
Sparrow — run the solver from Python (Phase 8).

Given a Sparrow instance dict (see ``core/sparrow_input.py``) this:

  1. creates a temporary job folder,
  2. writes the instance JSON into it,
  3. starts the ``sparrow`` executable (via an argument *list*, never a shell
     string),
  4. passes a time limit and a fixed seed,
  5. waits for completion,
  6. reads the final SVG and solution JSON,
  7. returns a structured result for Streamlit (or any caller).

Sparrow writes ``output/final_<name>.svg`` and ``output/final_<name>.json``
relative to its working directory, so the runner runs it with the job folder as
cwd and then finds the outputs by globbing ``output/final_*`` — robust to
whatever the instance name is.

Every failure mode the checkpoint calls out is turned into a ``RunStatus`` rather
than an exception:

  * missing executable        → ``MISSING_EXECUTABLE``
  * time limit exceeded        → ``TIMEOUT``
  * Sparrow rejected the input → ``INVALID_INPUT``
  * no feasible solution       → ``NO_SOLUTION``
  * output files not written   → ``MISSING_OUTPUT``
  * placed ≠ requested count    → ``QUANTITY_MISMATCH``

so the UI can always show something sensible.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class RunStatus(str, Enum):
    OK = "ok"
    MISSING_EXECUTABLE = "missing_executable"
    TIMEOUT = "timeout"
    INVALID_INPUT = "invalid_input"
    NO_SOLUTION = "no_solution"
    MISSING_OUTPUT = "missing_output"
    QUANTITY_MISMATCH = "quantity_mismatch"
    ERROR = "error"


# Names the runner will look for on PATH when no explicit path is given.
_EXECUTABLE_NAMES = ("sparrow", "sparrow.exe")
# Environment variable that can point directly at the binary.
_ENV_VAR = "SPARROW_BIN"

# Extra wall-clock seconds allowed on top of Sparrow's own time limit before the
# runner force-kills the process (covers startup, I/O, and the compression phase
# that runs after the exploration budget).
_WALL_CLOCK_MARGIN_SEC = 30


@dataclass
class SparrowResult:
    """Outcome of one Sparrow run."""

    status: RunStatus
    ok: bool
    message: str
    requested_counts: dict[int, int] = field(default_factory=dict)
    placed_counts: dict[int, int] = field(default_factory=dict)
    total_requested: int = 0
    total_placed: int = 0
    strip_width: float | None = None
    strip_height: float | None = None
    density: float | None = None
    run_time_sec: float | None = None
    svg: str | None = None
    solution: dict | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    job_dir: str | None = None

    @property
    def quantities_match(self) -> bool:
        return self.requested_counts == self.placed_counts


# ── Executable discovery ─────────────────────────────────────────────────────

def find_executable(executable: str | os.PathLike | None = None) -> str | None:
    """Resolve the Sparrow binary path, or None if it can't be found.

    Order: explicit argument → ``$SPARROW_BIN`` → PATH lookup of ``sparrow`` /
    ``sparrow.exe``.
    """
    candidates: list[str] = []
    if executable:
        candidates.append(str(executable))
    env = os.environ.get(_ENV_VAR)
    if env:
        candidates.append(env)

    for cand in candidates:
        p = Path(cand)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        found = shutil.which(cand)
        if found:
            return found

    for name in _EXECUTABLE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


# ── Main entry point ─────────────────────────────────────────────────────────

def run_sparrow(
    instance: dict,
    *,
    executable: str | os.PathLike | None = None,
    time_limit_sec: int = 10,
    seed: int = 0,
    early_termination: bool = True,
    workers: int | None = None,
    min_item_separation: float | None = None,
    job_dir: str | os.PathLike | None = None,
    keep_job_dir: bool = False,
) -> SparrowResult:
    """Run Sparrow on `instance` and return a structured result.

    `time_limit_sec` is passed to Sparrow (`-t`) and also bounds the runner's own
    wall-clock wait. `seed` is passed as a fixed RNG seed (`-s`) for reproducible
    runs. The process is started with an argument list, never a shell string.
    """
    requested = _requested_counts(instance)

    exe = find_executable(executable)
    if exe is None:
        return SparrowResult(
            status=RunStatus.MISSING_EXECUTABLE,
            ok=False,
            message=(
                "Sparrow-suoritustiedostoa ei löytynyt. Aseta polku "
                f"{_ENV_VAR}-ympäristömuuttujaan tai anna se suoraan."
            ),
            requested_counts=requested,
            total_requested=sum(requested.values()),
        )

    # 1) job folder
    own_dir = job_dir is None
    job_path = Path(job_dir) if job_dir else Path(tempfile.mkdtemp(prefix="sparrow_job_"))
    job_path.mkdir(parents=True, exist_ok=True)

    try:
        # 2) write the instance JSON
        input_path = job_path / "input.json"
        try:
            input_path.write_text(json.dumps(instance), encoding="utf-8")
        except (TypeError, ValueError) as exc:
            return SparrowResult(
                status=RunStatus.INVALID_INPUT,
                ok=False,
                message=f"Instanssia ei voitu sarjallistaa JSONiksi: {exc}",
                requested_counts=requested,
                total_requested=sum(requested.values()),
                job_dir=str(job_path),
            )

        # 3+4) argument list — no shell
        args: list[str] = [
            exe,
            "-i", str(input_path),
            "-t", str(int(time_limit_sec)),
            "-s", str(int(seed)),
        ]
        if early_termination:
            args.append("-x")
        if workers is not None:
            args += ["--workers", str(int(workers))]
        if min_item_separation is not None:
            args += ["--min-item-separation", str(float(min_item_separation))]

        # 5) run and wait
        wall_clock = int(time_limit_sec) + _WALL_CLOCK_MARGIN_SEC
        try:
            proc = subprocess.run(
                args,
                cwd=str(job_path),
                capture_output=True,
                text=True,
                timeout=wall_clock,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return SparrowResult(
                status=RunStatus.TIMEOUT,
                ok=False,
                message=(
                    f"Sparrow ei valmistunut {wall_clock} sekunnissa ja "
                    "keskeytettiin."
                ),
                requested_counts=requested,
                total_requested=sum(requested.values()),
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr),
                job_dir=str(job_path),
            )
        except OSError as exc:
            return SparrowResult(
                status=RunStatus.ERROR,
                ok=False,
                message=f"Sparrowin käynnistys epäonnistui: {exc}",
                requested_counts=requested,
                total_requested=sum(requested.values()),
                job_dir=str(job_path),
            )

        # 6) interpret exit + read outputs
        result = _collect_result(job_path, proc, requested)
        result.job_dir = str(job_path)
        return result
    finally:
        if own_dir and not keep_job_dir:
            shutil.rmtree(job_path, ignore_errors=True)


# ── Result assembly ──────────────────────────────────────────────────────────

def _collect_result(job_path: Path, proc, requested: dict[int, int]) -> SparrowResult:
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    total_req = sum(requested.values())

    if proc.returncode != 0:
        status, msg = _classify_failure(stderr, stdout)
        return SparrowResult(
            status=status,
            ok=False,
            message=msg,
            requested_counts=requested,
            total_requested=total_req,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    out_dir = job_path / "output"
    json_files = sorted(out_dir.glob("final_*.json"))
    svg_files = sorted(out_dir.glob("final_*.svg"))
    if not json_files:
        return SparrowResult(
            status=RunStatus.MISSING_OUTPUT,
            ok=False,
            message="Sparrow ei kirjoittanut final_*.json -tulostiedostoa.",
            requested_counts=requested,
            total_requested=total_req,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    try:
        solution = json.loads(json_files[0].read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return SparrowResult(
            status=RunStatus.MISSING_OUTPUT,
            ok=False,
            message=f"Tulos-JSONia ei voitu lukea: {exc}",
            requested_counts=requested,
            total_requested=total_req,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

    svg = None
    if svg_files:
        try:
            svg = svg_files[0].read_text(encoding="utf-8")
        except OSError:
            svg = None

    placed, sol_meta = _read_solution(solution)
    total_placed = sum(placed.values())

    if not placed:
        return SparrowResult(
            status=RunStatus.NO_SOLUTION,
            ok=False,
            message="Sparrow ei löytänyt ratkaisua (ei sijoitettuja osia).",
            requested_counts=requested,
            placed_counts=placed,
            total_requested=total_req,
            total_placed=total_placed,
            svg=svg,
            solution=solution,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            **sol_meta,
        )

    if placed != requested:
        return SparrowResult(
            status=RunStatus.QUANTITY_MISMATCH,
            ok=False,
            message=(
                f"Sijoitettu määrä ({total_placed}) ei vastaa pyydettyä "
                f"({total_req})."
            ),
            requested_counts=requested,
            placed_counts=placed,
            total_requested=total_req,
            total_placed=total_placed,
            svg=svg,
            solution=solution,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            **sol_meta,
        )

    return SparrowResult(
        status=RunStatus.OK,
        ok=True,
        message="Sparrow suoritettiin onnistuneesti.",
        requested_counts=requested,
        placed_counts=placed,
        total_requested=total_req,
        total_placed=total_placed,
        svg=svg,
        solution=solution,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        **sol_meta,
    )


def _requested_counts(instance: dict) -> dict[int, int]:
    counts: dict[int, int] = {}
    for item in instance.get("items", []):
        try:
            counts[int(item["id"])] = int(item.get("demand", 0))
        except (KeyError, TypeError, ValueError):
            continue
    return counts


def _read_solution(solution: dict) -> tuple[dict[int, int], dict]:
    """Extract placed-item counts and headline stats from a Sparrow output JSON.

    The solver's output is the instance echoed back plus a ``solution`` block:
    ``{"strip_width", "layout": {"placed_items": [...]}, "density", ...}``.
    """
    sol = solution.get("solution", solution)
    layout = sol.get("layout", {}) if isinstance(sol, dict) else {}
    placed_items = layout.get("placed_items", []) if isinstance(layout, dict) else []
    counter: Counter[int] = Counter()
    for pi in placed_items:
        try:
            counter[int(pi["item_id"])] += 1
        except (KeyError, TypeError, ValueError):
            continue

    meta: dict = {}
    if isinstance(sol, dict):
        for key in ("strip_width", "density", "run_time_sec"):
            if key in sol:
                meta[key] = sol[key]
    meta.setdefault("strip_height", solution.get("strip_height"))
    return dict(counter), meta


def _classify_failure(stderr: str, stdout: str) -> tuple[RunStatus, str]:
    """Map Sparrow's error output to a status + human message.

    Sparrow reports failures with anyhow, printing ``Error: <message>`` followed
    by a stack backtrace, so the message (not the backtrace) drives the class.
    """
    err = _error_message(stderr)
    blob = err.lower()
    # Bad geometry / unparseable JSON → the input is at fault.
    parse_markers = (
        "at least 3 points", "simple polygon", "duplicate vert",
        "non-consecutive", "expected", "invalid type", "missing field",
        "unknown variant", "deserialize", "parse", "eof",
        "trailing characters", "could not parse", "invalid input",
    )
    # The packer could not place a part (e.g. it does not fit the strip).
    fit_markers = (
        "could not construct an initial placement", "does not fit",
        "too large", "cannot fit", "infeasible", "no valid placement",
    )
    if any(m in blob for m in parse_markers):
        return RunStatus.INVALID_INPUT, err or "Virheellinen syöte."
    if any(m in blob for m in fit_markers):
        return RunStatus.NO_SOLUTION, err or "Ei ratkaisua."
    return RunStatus.ERROR, err or "Sparrow päättyi virheeseen."


def _error_message(stderr: str) -> str:
    """Pull the anyhow top-level ``Error: <msg>`` out of stderr, past backtraces."""
    lines = (stderr or "").splitlines()
    for line in lines:
        s = line.strip()
        if s.startswith("Error:"):
            return s[len("Error:"):].strip()[:300]
    # Fallback: first meaningful line that is not part of a backtrace.
    for line in lines:
        s = line.strip()
        if not s or s.startswith("Stack backtrace") or _is_backtrace_frame(s):
            continue
        return s[:300]
    return ""


def _is_backtrace_frame(line: str) -> bool:
    """True for a stack-backtrace frame line like ``3: some::function`` or ``at …``."""
    if line.startswith("at "):
        return True
    head = line.split(":", 1)[0].strip()
    return head.isdigit()


def _text(x) -> str:
    if x is None:
        return ""
    return x if isinstance(x, str) else x.decode("utf-8", "replace")


# ── CLI: run Sparrow on an instance file without Streamlit ────────────────────

def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run Sparrow on a Sparrow/jagua-rs instance JSON file."
    )
    parser.add_argument("input", help="Path to the instance JSON file.")
    parser.add_argument("--exe", default=None, help="Path to the sparrow binary.")
    parser.add_argument("-t", "--time-limit", type=int, default=10)
    parser.add_argument("-s", "--seed", type=int, default=0)
    parser.add_argument("--no-early-termination", action="store_true")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--out-dir", default=None,
                        help="Where to copy final_*.svg / final_*.json (default: cwd).")
    parser.add_argument("--keep-job-dir", action="store_true")
    args = parser.parse_args(argv)

    with open(args.input, encoding="utf-8") as fh:
        instance = json.load(fh)

    result = run_sparrow(
        instance,
        executable=args.exe,
        time_limit_sec=args.time_limit,
        seed=args.seed,
        early_termination=not args.no_early_termination,
        workers=args.workers,
        keep_job_dir=args.keep_job_dir,
    )

    print(f"status:    {result.status.value}")
    print(f"message:   {result.message}")
    print(f"requested: {result.requested_counts} (total {result.total_requested})")
    print(f"placed:    {result.placed_counts} (total {result.total_placed})")
    if result.strip_width is not None:
        print(f"strip:     width {result.strip_width}, height {result.strip_height}")
    if result.density is not None:
        print(f"density:   {result.density}")

    # write the outputs out so the checkpoint can see final_*.svg / final_*.json
    out_dir = Path(args.out_dir) if args.out_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = instance.get("name", "instance")
    if result.svg is not None:
        (out_dir / f"final_{name}.svg").write_text(result.svg, encoding="utf-8")
        print(f"wrote:     {out_dir / f'final_{name}.svg'}")
    if result.solution is not None:
        (out_dir / f"final_{name}.json").write_text(
            json.dumps(result.solution, indent=2), encoding="utf-8"
        )
        print(f"wrote:     {out_dir / f'final_{name}.json'}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
