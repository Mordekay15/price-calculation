"""The shop's calculators: safe formulas, step tables, and the bending
calculator of Offeri's demo (4 bends, 3.87 kg: 18 min setup, 1.67 min run)."""

import pytest

from core import calculators
from core.calculators import CalculatorError, evaluate, parse
from core.work import Operation, WorkInputs, WorkRates, phases, work_cost


def test_formulas_allow_arithmetic_and_a_few_functions():
    assert evaluate("10 + 2 * bends", {"bends": 4}) == 18
    assert evaluate("ceil(x / 3) if x > 0 else 0", {"x": 7}) == 3
    assert evaluate(55, {}) == 55


@pytest.mark.parametrize("formula", ["__import__('os')", "x.real", "open('f')", "[1][0]"])
def test_anything_else_is_refused(formula):
    with pytest.raises(CalculatorError):
        evaluate(formula, {"x": 1})


def test_the_default_bending_calculator_matches_the_demo():
    calc = calculators.parse(calculators.DEFAULT_FILE.read_text())
    sarmays = calc.calculators["sarmays"]
    times = sarmays.times({"weight_kg": 3.868, "bends": 4})
    assert (times.setup_min, times.run_min, times.eur_per_h) == pytest.approx((18, 5 / 3, 60))
    assert [round(times.minutes_per_piece(q), 2) for q in (1, 5, 10)] == [19.67, 5.27, 3.47]
    # Heavier parts take longer per bend and two people (120 €/h).
    assert sarmays.times({"weight_kg": 25, "bends": 1}).eur_per_h == 120
    assert calc.chains["Laser-Särmäys-Maalaus"] == ("sarmays", "maalaus")


def test_a_calculator_replacing_bending_drops_the_per_bend_price():
    op = Operation("Särmäys", per_piece_eur=1.0, batch_eur=18.0, replaces="bending")
    inp = WorkInputs(bends=4, eur_per_bend=0.2, operations=(op,))
    cost = work_cost(inp, 10, WorkRates())
    assert cost.bending == 0 and cost.operations == 1.0 and cost.operations_setup == 18.0
    assert cost.per_piece == pytest.approx(1.0 + (60 + 18) / 10)   # 2 phases: 60 €
    paint = Operation("Maalaus", 2.0, 15.0)
    assert phases(WorkInputs(bends=4, operations=(op, paint))) == 3


@pytest.mark.parametrize("text, message", [
    ("calculators: [{id: x}]", "puuttuva"),
    ("calculators: [{id: x, setup_min: 1, run_min: 'y', eur_per_h: 1}]", "Tuntematon"),
    ("chains: {A: [nope]}", "tuntematon laskuri"),
    ("calculators: [{id: x, setup_min: 1, run_min: 1, eur_per_h: 1, "
     "inputs: {w: {auto: colour}}}]", "auto-syöte"),
])
def test_a_broken_file_says_why(text, message):
    with pytest.raises(CalculatorError, match=message):
        parse(text)


def test_an_edited_copy_wins_and_can_be_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(calculators, "LOCAL_FILE", tmp_path / "local.yaml")
    calculators.save_local("calculators: [{id: x, name: X, setup_min: 1, run_min: 2, "
                           "eur_per_h: 60}]")
    assert list(calculators.load().calculators) == ["x"]
    calculators.reset_local()
    assert "sarmays" in calculators.load().calculators
