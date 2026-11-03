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


def test_the_quote_table_shows_a_subassembly_inside_its_parent_only():
    from streamlit.testing.v1 import AppTest

    def page():
        from core.assemblies import Assembly, BoughtPart
        from core.work import LaserSpeed, WorkInputs
        from view.quote import render_batch_prices

        work = WorkInputs(cut_length_m=1.0, laser=LaserSpeed(10, 0.05), setup_eur=0.0)
        products = [{"id": i, "name": f"{i}.dxf", "material": "S235", "thickness": "2",
                     "width": 100.0, "height": 50.0, "qty": 1, "work": work} for i in "ab"]
        group = ("S235", "2")
        render_batch_prices(
            products, {p["id"]: 1000.0 for p in products}, {group: products},
            {group: 1}, {group: 50.0}, lead="Osa", key="t",
            assemblies=[Assembly("s", "Kehys", (("a", 2),), (BoughtPart("Tappi", 1, 0.5),)),
                        Assembly("t", "Runko", (("b", 1),), children=(("s", 3),))])

    at = AppTest.from_function(page, default_timeout=30).run()
    assert not at.exception
    rows = at.dataframe[0].value
    assert list(rows["Osa"]) == ["🧩 Runko", "\u2003└ b.dxf × 1", "\u2003└ 🧩 Kehys × 3",
                                 "\u2003\u2003└ a.dxf × 2", "\u2003\u2003└ Tappi × 1 (ostettu)"]
    # Per batch: 3 Kehys and 6 a.dxf in each Runko.
    assert list(rows["Määrä"])[2:4] == ["3 · 15 · 30", "6 · 30 · 60"]
    col = rows.columns[-1]
    assert rows[col][0] == pytest.approx(rows[col][1] + rows[col][2], abs=0.02)
    assert rows[col][2] == pytest.approx(rows[col][3] + rows[col][4], abs=0.02)


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
        a = st.session_state[assemblies.ASSEMBLIES]["3axd50001139896"]
        st.session_state["got"] = (a["name"], a["drawn"], a["awaited"])
        # A loose plate's own drawing is no assembly.
        assemblies.sync_from_drawings({"p": plate}, lambda cid: text)
        st.session_state["left"] = list(st.session_state[assemblies.ASSEMBLIES])

    at = AppTest.from_function(page).run()
    assert not at.exception
    assert at.session_state["got"] == ("3AXD50001139896", {"p": 1}, ["3AXD50000026889"])
    assert at.session_state["left"] == []


def test_one_assembly_per_part_number_with_its_subassemblies():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace

        import streamlit as st

        from core.items import NO_BLANK, Choice
        from view import assemblies

        def drawing(cid, name):
            return SimpleNamespace(names={cid: name},
                                   scores={cid: Choice(-1e9, NO_BLANK, valid=False)})

        def plate(cid, name):
            return SimpleNamespace(names={cid: name}, scores={cid: Choice(100, "puhdas aihio")})

        items = {"top": drawing("top", "ASM-070000.pdf"), "sub": drawing("sub", "ASM-070100.dxf"),
                 "a": plate("a", "ITM-064555.dxf"), "b": plate("b", "ITM-064556.dxf"),
                 "c": plate("c", "ITM-064557.dxf")}
        texts = {  # the top: the subassembly × 2 (BOM) and a plate; the sub: two plates
            "top": "1 ASM-070100 Kehys 2 pcs\nASM-070100\nITM-064555\n2 ITM-064555 Levy 3 pcs",
            "sub": "ITM-064556\nITM-064557\nITM-064555"}
        # The STEP model of the sub has the same part number: one assembly.
        store = st.session_state.setdefault(assemblies.ASSEMBLIES, {})
        store["asm-070100"] = {"name": "ASM-070100", "members": {"b": 4}, "auto": "s1",
                               "bought": {"rows": [], "seed": []}}
        for _ in range(2):
            assemblies.sync_from_drawings(items, texts.get)
        cards = {c: c for c in items}
        got = {a.id: (a.members, a.children) for a in assemblies.current(cards)}
        st.session_state["got"] = (sorted(store), got)

    at = AppTest.from_function(page).run()
    assert not at.exception
    ids, got = at.session_state["got"]
    assert ids == ["asm-070000", "asm-070100"]
    # The STEP's kpl (4) beats the drawing's callout (1); the top's plate
    # 064555 is also called out on the sub's drawing, but sits in the sub only.
    assert got["asm-070100"] == ((("b", 4), ("c", 1), ("a", 1)), ())
    assert got["asm-070000"] == ((), (("asm-070100", 2),))


def test_a_subassembly_is_priced_inside_its_parent():
    from core.assemblies import counts, tops, tree_prices

    sub = Assembly("s", "Kehys", (("p", 2),), (BoughtPart("Tappi", 1, 1.0),))
    top = Assembly("t", "Runko", (("q", 1),), children=(("s", 3),))
    assert [a.id for a in tops([sub, top])] == ["t"]
    assert counts([sub, top]) == {"t": 1, "s": 3}
    assert per_unit([sub, top]) == {"p": 6, "q": 1}
    prices = tree_prices([sub, top], {"p": [2.0], "q": [5.0]}, [10], RATES)
    assert prices["s"][0].qty == 30 and prices["s"][0].unit == pytest.approx(5.0)
    assert prices["t"][0].parts == pytest.approx(5.0 + 3 * 5.0)
    # A loop is cut: neither is the other's subassembly.
    loop = Assembly("s", "Kehys", children=(("t", 1),))
    assert sorted(a.id for a in tops([loop, top])) == ["s", "t"]


def test_the_drawing_notes_give_each_fastener_line():
    from core.dxf import fastener_lines, fasteners_from_texts

    texts = ["M5-PURISTEMUTTERIT X4KPL", "HUOM: M4-PURISTEMUTTERIT 5KPL"]
    assert fastener_lines(texts) == [("M5-PURISTEMUTTERIT", 4), ("M4-PURISTEMUTTERIT", 5)]
    assert fasteners_from_texts(texts) == 9


def test_a_drawing_pdf_gives_each_fastener_with_its_pcs_count():
    from core.fasteners import pdf_fasteners
    from tests.fastener_pdf import drawing_pdf

    assert sorted(pdf_fasteners(drawing_pdf())) == [
        ("M5 KALEI NUT", 4), ("M6 KALEI NUT", 6), ("PEM-FH-M4-12", 25),
        ("PEM-FH-M4-8", 2), ("PEM-FH-M6-12", 2)]


def test_only_an_assembly_drawings_own_notes_are_its_bought_parts():
    from streamlit.testing.v1 import AppTest

    def page():
        import streamlit as st

        from view import assemblies

        store = st.session_state.setdefault(assemblies.ASSEMBLIES, {})
        store["drawing:d"] = {"name": "Runko", "members": {"p": 1},
                              "bought": {"rows": [], "seed": []}, "drawing": "d"}
        lines = {"d": [("M6-RUUVIT", 4)], "p": [("M5-PURISTEMUTTERIT", 4)]}
        assemblies.sync_drawing_fasteners(lines, lambda cid: "RST 1.4301")
        assemblies.sync_drawing_fasteners(lines)                    # only once
        st.session_state["drawn"] = [(r["name"], r["kpl"], r["eur"])
                                     for r in store["drawing:d"]["bought"]["rows"]]
        # Joined by hand: the parts' fasteners stay on their cards.
        assemblies.join(["p", "q"], "Kehys")
        hand = next(a for a in store.values() if a.get("hand"))
        st.session_state["hand"] = (sorted(store), hand["members"], hand["bought"]["rows"])

    at = AppTest.from_function(page).run()
    assert not at.exception
    assert at.session_state["drawn"] == [("M6-RUUVIT", 4, 0.80)]
    ids, members, bought = at.session_state["hand"]
    assert ids[0] == "drawing:d" and ids[1].startswith("hand:") and len(ids) == 2
    assert (members, bought) == ({"p": 1, "q": 1}, [])


def test_on_the_page_a_parts_fasteners_are_its_own_bought_parts_wherever_it_goes():
    from streamlit.testing.v1 import AppTest

    from view.assemblies import ASSEMBLIES
    from view.work import PART_BOUGHT

    at = AppTest.from_file("fastener_page.py", default_timeout=60).run()
    assert not at.exception
    assert at.session_state[ASSEMBLIES] == {}          # fasteners make no assembly
    own = at.session_state[PART_BOUGHT]
    assert [(r["name"], r["kpl"], r["from"]) for r in own["f0"]["rows"]] \
        == [("M5-PURISTEMUTTERIT", 4, "levy.dxf")]
    assert sorted((r["name"], r["kpl"]) for r in own["f3"]["rows"]) == [
        ("M5 KALEI NUT", 4), ("M6 KALEI NUT", 6), ("PEM-FH-M4-12", 25),
        ("PEM-FH-M4-8", 2), ("PEM-FH-M6-12", 2)]
    assert {r["from"] for r in own["f3"]["rows"]} == {"kotelo.pdf"}
    assert own["f1"]["rows"] == []
    # Priced on the card: 4 × (0.05 + 0.20) per piece, the count gone.
    work = at.session_state["work"]
    assert work["f0"].fasteners == 4
    assert sum(b.unit * b.qty for b in work["f0"].bought) == pytest.approx(1.00)
    keys = {n.key for n in at.number_input}
    assert not {f"work_{k}_f0" for k in ("fasteners", "fastener_eur", "insert_eur")} & keys
    assert "work_weld_f0" in keys
    # "Uusi kokoonpano": the press-nutted plate and an angle welded together.
    at.multiselect(key="asm_new_parts").set_value(["f0", "f1"]).run()
    at.text_input(key="asm_new_name").set_value("Runko").run()
    at.button(key="asm_new_go").click().run()
    assert not at.exception
    store = at.session_state[ASSEMBLIES]
    (aid, a), = store.items()
    assert aid.startswith("hand:") and a["name"] == "Runko"
    assert a["members"] == {"f0": 1, "f1": 1} and a["bought"]["rows"] == []
    # The plate keeps its nuts in the assembly; the assembly welds.
    assert at.session_state["work"]["f0"].fasteners == 4
    assert "work_weld_f0" not in {n.key for n in at.number_input}
    # "Pura" leaves the parts loose.
    at.button(key=f"asm_del_{aid}").click().run()
    assert not at.exception
    assert at.session_state[ASSEMBLIES] == {}
    assert sorted(at.session_state["ids"]) == ["f0", "f1", "f2", "f3"]
    # A removed card leaves no Ostetut osat behind.
    at.button(key="dxf_del_f0").click().run()
    assert not at.exception
    assert "f0" not in at.session_state[PART_BOUGHT] and "f3" in at.session_state[PART_BOUGHT]


def test_a_parts_own_bought_parts_are_rows_under_it_in_the_price_table():
    from core.work import BoughtPart
    from view.quote import _bought_rows

    work = WorkInputs(fasteners=4, bought=(BoughtPart("M5-PURISTEMUTTERIT", 4, 0.05, 0.20),))
    assert _bought_rows(work, 2, 2, 3) == [
        {"name": "  └ M5-PURISTEMUTTERIT × 4 (ostettu)", "per": 8,
         "units": [2.0, 2.0, 2.0]}]
    assert _bought_rows(WorkInputs(fasteners=4), 1, 1, 3) == []
    assert _bought_rows(None, 1, 1, 3) == []
