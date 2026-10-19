"""The shop's own work-phase calculators, pure (no Streamlit).

A calculator prices one work phase from a few inputs with the shop's own
formulas, kept in ``calculators.yaml`` (edited copy: ``calculators.local.yaml``,
which wins). Each one has:

- ``inputs``: numbers it needs. ``auto`` takes one from the part (see
  ``AUTO_INPUTS``); ``default`` is typed on the card.
- ``tables``: step tables looked up by an input, ``rows: [[from, value], …]``:
  the value of the last row whose ``from`` is at most the input (the first
  row below that).
- ``setup_min``, ``run_min`` and ``eur_per_h``: formulas over the inputs and
  tables. Setup is per batch, run time per piece.
- ``replaces`` (optional): the built-in phase it takes over (``bending``).

``chains`` are named lists of calculators that a card can add in one go
(e.g. ``Laser-Särmäys-Maalaus``). Formulas are evaluated with a small safe
evaluator (numbers, + - * / // % **, comparisons, ``a if c else b`` and
``min max ceil floor round abs``), never ``eval``.
"""

from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.work import Operation

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "calculators.yaml"
LOCAL_FILE = ROOT / "calculators.local.yaml"

# What a calculator's ``auto`` input can take from the part.
AUTO_INPUTS = {
    "weight_kg":    "Osan paino (kg)",
    "area_m2":      "Osan pinta-ala (m², yksi puoli)",
    "thickness_mm": "Paksuus (mm)",
    "cut_length_m": "Leikkuupituus (m)",
    "bends":        "Taivutukset (kpl)",
    "threads":      "Kierteet (kpl)",
    "fasteners":    "Kiinnikkeet (kpl)",
}


class CalculatorError(ValueError):
    """A calculator file or formula that can't be used; the message says why."""


# ── Safe formulas ─────────────────────────────────────────────────────────────

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMPOPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
           ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCS = {"min": min, "max": max, "ceil": math.ceil, "floor": math.floor,
          "round": round, "abs": abs}


def evaluate(formula: str | float | int, names: dict[str, float]) -> float:
    """The value of ``formula`` with ``names`` (a plain number is itself)."""
    if isinstance(formula, (int, float)):
        return float(formula)
    try:
        tree = ast.parse(str(formula), mode="eval")
    except SyntaxError as e:
        raise CalculatorError(f"Kaava ei ole kelvollinen: {formula!r}") from e

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise CalculatorError(f"Tuntematon nimi kaavassa {formula!r}: {node.id}")
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            return _BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = ev(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.Compare) and all(type(o) in _CMPOPS for o in node.ops):
            left = ev(node.left)
            for op, right_node in zip(node.ops, node.comparators):
                right = ev(right_node)
                if not _CMPOPS[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return ev(node.body) if ev(node.test) else ev(node.orelse)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCS and not node.keywords):
            return _FUNCS[node.func.id](*(ev(a) for a in node.args))
        raise CalculatorError(f"Kaavassa {formula!r} on jotain, mitä ei sallita.")

    try:
        return float(ev(tree))
    except ZeroDivisionError as e:
        raise CalculatorError(f"Nollalla jako kaavassa {formula!r}.") from e


# ── Calculators ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Input:
    name: str
    label: str
    auto: str | None = None       # a key of AUTO_INPUTS
    default: float = 0.0


@dataclass(frozen=True)
class Table:
    by: str
    rows: tuple[tuple[float, float], ...]

    def lookup(self, x: float) -> float:
        value = self.rows[0][1]
        for start, v in self.rows:
            if x + 1e-12 >= start:
                value = v
        return value


@dataclass(frozen=True)
class Times:
    """One calculator's result for one part: setup minutes per batch, run
    minutes per piece and the hourly rate."""

    setup_min: float
    run_min: float
    eur_per_h: float

    def minutes_per_piece(self, qty: int) -> float:
        """Setup shared over ``qty`` plus the run time."""
        return self.setup_min / max(qty, 1) + self.run_min

    def eur_per_piece(self, qty: int) -> float:
        return self.minutes_per_piece(qty) / 60 * self.eur_per_h


@dataclass(frozen=True)
class Calculator:
    id: str
    name: str
    inputs: tuple[Input, ...]
    tables: dict[str, Table]
    setup_min: str | float
    run_min: str | float
    eur_per_h: str | float
    replaces: str | None = None
    note: str = ""

    def times(self, values: dict[str, float]) -> Times:
        """Evaluate the formulas with the input ``values`` (by input name)."""
        names = {i.name: float(values.get(i.name, i.default)) for i in self.inputs}
        for name, table in self.tables.items():
            if table.by not in names:
                raise CalculatorError(f"{self.name}: taulukko {name} hakee tuntemattomalla "
                                      f"syötteellä {table.by}.")
            names[name] = table.lookup(names[table.by])
        return Times(evaluate(self.setup_min, names), evaluate(self.run_min, names),
                     evaluate(self.eur_per_h, names))

    def operation(self, values: dict[str, float]) -> Operation:
        """The calculator's work as ``core.work`` prices it."""
        t = self.times(values)
        return Operation(self.name, per_piece_eur=t.run_min / 60 * t.eur_per_h,
                         batch_eur=t.setup_min / 60 * t.eur_per_h, replaces=self.replaces)


@dataclass(frozen=True)
class CalculatorSet:
    calculators: dict[str, Calculator] = field(default_factory=dict)
    chains: dict[str, tuple[str, ...]] = field(default_factory=dict)


def parse(text: str) -> CalculatorSet:
    """Read a calculator file's text; ``CalculatorError`` says what is wrong."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise CalculatorError(f"Tiedosto ei ole kelvollista YAMLia: {e}") from e
    if not isinstance(data, dict):
        raise CalculatorError("Tiedoston pitää alkaa avaimilla calculators ja chains.")

    calcs: dict[str, Calculator] = {}
    for raw in data.get("calculators") or []:
        try:
            cid = str(raw["id"])
            inputs = tuple(
                Input(name, str(spec.get("label", name)), spec.get("auto"),
                      float(spec.get("default", 0)))
                for name, spec in (raw.get("inputs") or {}).items())
            tables = {name: Table(str(spec["by"]),
                                  tuple(sorted((float(a), float(b)) for a, b in spec["rows"])))
                      for name, spec in (raw.get("tables") or {}).items()}
            calc = Calculator(cid, str(raw.get("name", cid)), inputs, tables,
                              raw["setup_min"], raw["run_min"], raw["eur_per_h"],
                              raw.get("replaces"), str(raw.get("note", "")))
        except (KeyError, TypeError, ValueError) as e:
            raise CalculatorError(f"Laskuri {raw!r:.60}: puuttuva tai väärä kenttä ({e}).") from e
        for i in inputs:
            if i.auto is not None and i.auto not in AUTO_INPUTS:
                raise CalculatorError(f"{calc.name}: tuntematon auto-syöte {i.auto}.")
        if any(not t.rows for t in tables.values()):
            raise CalculatorError(f"{calc.name}: tyhjä taulukko.")
        calc.times({i.name: i.default or 1.0 for i in inputs})  # formulas must evaluate
        calcs[cid] = calc

    chains = {str(name): tuple(str(c) for c in ids)
              for name, ids in (data.get("chains") or {}).items()}
    for name, ids in chains.items():
        missing = [c for c in ids if c not in calcs]
        if missing:
            raise CalculatorError(f"Ketju {name}: tuntematon laskuri {', '.join(missing)}.")
    return CalculatorSet(calcs, chains)


def load_text() -> str:
    """The calculator file in use: the edited copy if there is one."""
    path = LOCAL_FILE if LOCAL_FILE.exists() else DEFAULT_FILE
    return path.read_text(encoding="utf-8") if path.exists() else ""


def load() -> CalculatorSet:
    return parse(load_text())


def save_local(text: str) -> CalculatorSet:
    """Check ``text`` and save it as the edited copy."""
    parsed = parse(text)
    LOCAL_FILE.write_text(text, encoding="utf-8")
    return parsed


def reset_local() -> None:
    """Go back to the default file."""
    LOCAL_FILE.unlink(missing_ok=True)
