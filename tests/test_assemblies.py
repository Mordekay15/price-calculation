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
    assert all(v != "" for v in rows.iloc[1, 4:])                   # the assembly is priced
    # Quantities per batch, and the rows add up to the assembly's €/kpl.
    assert list(rows["Määrä"])[1:] == ["1 · 5 · 10", "1 · 5 · 10", "2 · 10 · 20",
                                       "6 · 30 · 60"]
    for col in [c for c in rows.columns if c.endswith("€/kpl")]:
        assert rows[col][1] == pytest.approx(sum(rows[col][2:]), abs=0.02)
    assert rows[rows.columns[-1]][4] == pytest.approx(3.0)            # 6 × 0.5 €
    # The breakdown (batch 1): each part's pieces, and the assembly's steps.
    parts = at.dataframe[1].value
    assert list(parts["Kpl erässä"]) == [1, 2, 1]                  # a, b × 2, c
    steps = at.dataframe[2].value
    assert steps["Vaihe"].iloc[-2] == "6. Kokoonpanon kpl-hinta"
    assert float(steps["Arvo €"].iloc[-2]) == pytest.approx(rows["1 kpl, €/kpl"][1], abs=0.01)


def test_an_assembly_drawing_becomes_an_assembly_awaiting_its_missing_items():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace

        import streamlit as st

        from core.items import NO_BLANK, Choice
        from view import assemblies

        drawing = SimpleNamespace(names={"d": "3axd50001139896.dxf"},
                                  scores={"d": Choice(-1e9, NO_BLANK, valid=False)})
        plate = SimpleNamespace(names={"p": "3AXD50001016005.dxf"},
                                scores={"p": Choice(100, "puhdas aihio")})
        text = "3AXD50001016005\n3AXD50000026889\nTitle 3AXD50001139896"
        assemblies.sync_from_drawings({"d": drawing, "p": plate}, lambda cid: text)
        a = st.session_state[assemblies.ASSEMBLIES]["drawing:d"]
        st.session_state["got"] = (a["name"], a["members"], a["awaited"])
        # A loose plate's own drawing is no assembly.
        assemblies.sync_from_drawings({"p": plate}, lambda cid: text)
        st.session_state["left"] = list(st.session_state[assemblies.ASSEMBLIES])

    at = AppTest.from_function(page).run()
    assert not at.exception
    assert at.session_state["got"] == ("3AXD50001139896", {"p": 1}, ["3AXD50000026889"])
    assert at.session_state["left"] == []


def test_the_drawing_notes_give_each_fastener_line():
    from core.dxf import fastener_lines, fasteners_from_texts

    texts = ["M5-PURISTEMUTTERIT X4KPL", "HUOM: M4-PURISTEMUTTERIT 5KPL"]
    assert fastener_lines(texts) == [("M5-PURISTEMUTTERIT", 4), ("M4-PURISTEMUTTERIT", 5)]
    assert fasteners_from_texts(texts) == 9


def test_a_part_with_fasteners_is_an_assembly_of_it_and_them():
    from streamlit.testing.v1 import AppTest

    def page():
        import streamlit as st

        from view import assemblies

        lines = {"p": [("M5-PURISTEMUTTERIT", 4)], "q": [], "r": [("M4-RUUVIT", 2)]}
        names = {"p": "Levy.dxf", "q": "Kansi.dxf", "r": "Kulma.dxf"}
        cards = {c: c for c in names}
        assemblies.sync_from_fasteners(lines, names, cards, lambda cid: "RST 1.4301")
        store = st.session_state[assemblies.ASSEMBLIES]
        st.session_state["own"] = (sorted(store), store["fasteners:p"]["name"],
                                   store["fasteners:p"]["members"],
                                   store["fasteners:p"]["bought"])
        # Joined to another part by hand, its fasteners come along.
        assemblies.join(["p", "q"], "Runko")
        assemblies.sync_from_fasteners(lines, names, cards)
        hand = next(a for a in store.values() if a.get("hand"))
        st.session_state["joined"] = (sorted(a.get("fasteners", "") for a in store.values()),
                                      hand["members"], [r["Nimike"] for r in hand["bought"]])
        # A part in a STEP assembly puts its fasteners there, × its kpl.
        store["step:x"] = {"name": "X", "members": {"r": 3}, "bought": [], "auto": "x"}
        assemblies.sync_from_fasteners(lines, names, cards)
        assemblies.sync_from_fasteners(lines, names, cards)          # only once
        st.session_state["step"] = ("fasteners:r" in store,
                                    [(r["Nimike"], r["Kpl / kokoonpano"])
                                     for r in store["step:x"]["bought"]])

    at = AppTest.from_function(page).run()
    assert not at.exception
    ids, name, members, bought = at.session_state["own"]
    assert ids == ["fasteners:p", "fasteners:r"]
    assert (name, members) == ("Levy", {"p": 1})
    assert bought == [{"Nimike": "M5-PURISTEMUTTERIT", "Kpl / kokoonpano": 4,
                       "€/kpl": 0.80, "Asennus (€/kpl)": 0.20}]
    assert at.session_state["joined"] == (["", "r"], {"p": 1, "q": 1},
                                          ["M5-PURISTEMUTTERIT"])
    assert at.session_state["step"] == (False, [("M4-RUUVIT", 6)])


def test_on_the_page_a_part_with_fasteners_and_parts_joined_by_hand_are_assemblies():
    from streamlit.testing.v1 import AppTest

    from view.assemblies import ASSEMBLIES

    at = AppTest.from_file("fastener_page.py", default_timeout=60).run()
    assert not at.exception
    store = at.session_state[ASSEMBLIES]
    assert list(store) == ["fasteners:f0"]
    assert [(r["Nimike"], r["Kpl / kokoonpano"]) for r in store["fasteners:f0"]["bought"]] \
        == [("M5-PURISTEMUTTERIT", 4)]
    # The card in it has no fastener inputs: its assembly prices them.
    keys = {n.key for n in at.number_input}
    assert "work_bends_f0" in keys and "work_fasteners_f1" in keys
    assert not {f"work_{k}_f0" for k in ("fasteners", "fastener_eur", "insert_eur")} & keys
    # "Uusi kokoonpano": the press-nutted plate and an angle welded together.
    at.multiselect(key="asm_new_parts").set_value(["f0", "f1"]).run()
    at.text_input(key="asm_new_name").set_value("Runko").run()
    at.button(key="asm_new_go").click().run()
    assert not at.exception
    store = at.session_state[ASSEMBLIES]
    (aid, a), = store.items()
    assert aid.startswith("hand:") and a["name"] == "Runko"
    assert a["members"] == {"f0": 1, "f1": 1}
    assert [r["Nimike"] for r in a["bought"]] == ["M5-PURISTEMUTTERIT"]
    # "Pura" leaves the parts loose, the plate with its fasteners again.
    at.button(key=f"asm_del_{aid}").click().run()
    assert not at.exception
    assert list(at.session_state[ASSEMBLIES]) == ["fasteners:f0"]
    assert sorted(at.session_state["ids"]) == ["f0", "f1", "f2"]
