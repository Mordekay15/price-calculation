"""Assemblies: parts priced at batch × kpl/kokoonpano, bought parts and
assembly work, one quote line."""

import io

import openpyxl
import pytest

from core.assemblies import (
    Assembly,
    AssemblyWork,
    BoughtPart,
    assembly_prices,
    per_unit,
    work_eur,
)
from core.batches import BatchPart, GroupSheets, batch_prices
from core.quote import QuoteInfo, QuoteLine, quote_pdf, quote_xlsx
from core.work import LaserSpeed, WorkInputs, WorkRates

RATES = WorkRates()          # welding 1.5 m/h at 60 €/h: 40 €/m


def test_a_part_in_an_assembly_is_priced_at_batch_times_its_count():
    work = WorkInputs(cut_length_m=1.0, laser=LaserSpeed(10, 0.05), setup_eur=40.0)
    group = {("S235", "2"): GroupSheets(total_eur=80.0, sheets=1, pieces_kg=40.0)}
    alone = batch_prices([BatchPart("a", ("S235", "2"), 1.0, work)], group, [10], RATES)
    two = batch_prices([BatchPart("a", ("S235", "2"), 1.0, work, per_unit=2)], group, [5],
                       RATES)
    # 5 assemblies of 2 are the same 10 pieces as 10 parts on their own.
    assert two["a"][0].qty == 10
    assert two["a"][0].unit == pytest.approx(alone["a"][0].unit)


def test_an_assembly_adds_its_parts_bought_parts_and_work():
    a = Assembly("x", "Runko", (("p1", 1), ("p2", 2)),
                 (BoughtPart("Tappi", 6, 0.5),),
                 AssemblyWork(fit_min=30, weld_m=0.5, setup_min=60, eur_per_h=60,
                              other_eur=2.0))
    assert per_unit([a, Assembly("y", "Toinen", (("p2", 1),))]) == {"p1": 1, "p2": 3}
    # Batch 1: setup 60 + fitting 30 min = 90 €, welding 0.5 m = 20 €, other 2 €.
    assert work_eur(a.work, 1, RATES) == pytest.approx(112.0)
    assert work_eur(a.work, 10, RATES) == pytest.approx(6 + 30 + 20 + 2)
    one, ten = assembly_prices(a, {"p1": [10.0, 8.0], "p2": [4.0, 3.0]}, [1, 10], RATES)
    assert one.parts == pytest.approx(18.0) and one.bought == pytest.approx(3.0)
    assert one.unit == pytest.approx(18 + 3 + 112)
    assert ten.unit == pytest.approx(14 + 3 + 58) and ten.total == pytest.approx(750)
    # Putting a bought part in is priced with it.
    pem = Assembly("z", "Levy", (), (BoughtPart("PEM", 4, 0.10, 0.20),))
    assert assembly_prices(pem, {}, [1], RATES)[0].bought == pytest.approx(1.2)
    # A part without a price leaves the assembly unpriced.
    assert assembly_prices(a, {"p1": [None]}, [1], RATES)[0].unit is None


def test_an_assembly_is_one_line_in_the_pdf_and_excel():
    a = Assembly("x", "Runko", (("p1", 1),), (BoughtPart("Tappi", 6, 0.5),))
    line = QuoteLine("Runko", prices=tuple(assembly_prices(a, {"p1": [10.0]}, [1], RATES)),
                     parts=("Pohja.dxf × 1", "Tappi × 6"))
    info = QuoteInfo(number="T-2")
    assert quote_pdf(info, [line], [1], [13.0]).startswith(b"%PDF")
    wb = openpyxl.load_workbook(io.BytesIO(quote_xlsx(info, [line], [1])))
    assert wb.sheetnames == ["Tarjous", "Erittely", "Kokoonpanot"]
    row = [c.value for c in wb["Kokoonpanot"][2]]
    assert row == ["Runko", 1, 10.0, 3.0, 0.0, 13.0, "Pohja.dxf × 1, Tappi × 6"]


def test_the_quote_table_shows_an_assembly_line_with_its_parts():
    from streamlit.testing.v1 import AppTest

    def page():
        import streamlit as st

        from core.assemblies import Assembly, BoughtPart
        from core.work import LaserSpeed, WorkInputs
        from view.quote import render_batch_prices

        work = WorkInputs(cut_length_m=1.0, laser=LaserSpeed(10, 0.05), setup_eur=0.0)
        products = [{"id": i, "name": f"{i}.dxf", "material": "S235", "thickness": "2",
                     "width": 100.0, "height": 50.0, "qty": q, "work": work}
                    for i, q in (("a", 2), ("b", 4), ("c", 1))]
        group = ("S235", "2")
        render_batch_prices(
            products, {p["id"]: 1000.0 for p in products}, {group: products},
            {group: 1}, {group: 50.0}, lead="Osa", key="t",
            assemblies=[Assembly("x", "Runko", (("a", 1), ("b", 2)),
                                 (BoughtPart("Tappi", 6, 0.5),))])

    at = AppTest.from_function(page, default_timeout=30).run()
    assert not at.exception
    rows = at.dataframe[0].value
    assert list(rows["Osa"]) == ["c.dxf", "🧩 Runko", " └ a.dxf × 1",
                                 " └ b.dxf × 2", " └ Tappi × 6 (ostettu)"]
    assert all(v != "" for v in rows.iloc[1, 3:])                   # the assembly is priced
