"""Smoke test: the whole page renders without an exception."""

from streamlit.testing.v1 import AppTest


FILES, MANUAL = "Tarjous tiedostoista", "Pikalaskuri (käsin)"
BOM, NESTING, PRICING = "1 · Osat ja tiedostot", "2 · Nestaus", "3 · Hinnoittelu"


def test_app_renders_the_menu_and_each_screen():
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception
    assert sorted(t.label for t in at.tabs) == sorted([FILES, MANUAL, BOM, NESTING, PRICING])
    for screen in (NESTING, PRICING, BOM):
        at.session_state["dxf_menu"] = screen
        at.run()
        assert not at.exception
    at.session_state["dxf_menu"] = NESTING
    at.run()
    at.session_state["menu"] = MANUAL
    at.run()
    assert not at.exception and len(at.tabs) == 2
    at.session_state["menu"] = FILES
    at.run()
    assert at.session_state["dxf_menu"] == NESTING          # back on the same screen


def test_a_missing_solver_is_said_on_the_page(monkeypatch):
    # Without scipy the fewest-sheets and mixed-size plans can't be made; the
    # page must not quietly show only the old ones.
    import view.common

    def page():
        from view.common import render_groups
        render_groups({}, lambda key, products: None)

    monkeypatch.setattr(view.common, "have_solver", lambda: False)
    at = AppTest.from_function(page).run()
    assert any("scipy" in w.value for w in at.warning)
    monkeypatch.setattr(view.common, "have_solver", lambda: True)
    assert not AppTest.from_function(page).run().warning


def test_each_screen_keeps_the_others_inputs_and_prices_on_hinnoittelu(tmp_path, monkeypatch):
    # Only the open screen is drawn; what was typed on the others stays, and
    # Hinnoittelu prices the sheets nested on Nestaus.
    import json

    import ezdxf

    from core import suppliers
    from core.dxf import read_dxf
    from tests.conftest import dxf_bytes

    monkeypatch.chdir(tmp_path)
    rows = [{"Paksuus (mm)": "2", "S235 | 1000x2000": 900.0}]
    json.dump({"data": {"levyt": rows}, "source_file": "t.pdf", "updated_at": "x"},
              open(tmp_path / suppliers.SUPPLIERS[0].path, "w"))
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.session_state["dxf_files"] = {"f0": read_dxf(dxf_bytes(doc), "p.dxf")}
    at.session_state["menu"] = FILES
    at.run()
    at.selectbox(key="dxf_mat_f0").select("S235").run()
    at.selectbox(key="dxf_th_f0").select("2").run()
    at.number_input(key="work_bends_f0").set_value(3.0).run()     # work is on the card
    at.session_state["dxf_menu"] = PRICING
    at.run()
    assert any("ei ole vielä nestattu" in i.value for i in at.info)
    at.session_state["dxf_menu"] = NESTING
    at.run()
    at.number_input(key="dxf_sparrow_t").set_value(1).run()
    at.button(key="dxf_sparrow_run").click().run()
    assert not at.exception
    assert not any(n.key == "work_bends_f0" for n in at.number_input)   # not drawn here
    assert not any("kpl, €/kpl" in "".join(d.value.columns) for d in at.dataframe)
    at.session_state["dxf_menu"] = PRICING
    at.run()
    assert not at.exception
    # The batch-size switchable breakdown, and the quote as the customer sees it.
    [table] = [d.value for d in at.dataframe if "Taivutus" in d.value.columns]
    assert len(table) == 1 and table["Taivutus"].iloc[0] > 0     # the card's 3 bends
    at.session_state["dxf_menu"] = BOM
    at.run()
    assert at.number_input(key="work_bends_f0").value == 3.0
    assert at.selectbox(key="dxf_mat_f0").value == "S235"
    assert at.number_input(key="dxf_q_f0").value == 10   # the largest batch
    # The bar has the total on every step, here too.
    assert [m.value for m in at.metric if m.label == "10 kpl / nimike"][0] not in ("", "–")
    at.session_state["dxf_menu"] = NESTING
    at.run()
    assert at.number_input(key="dxf_sparrow_t").value == 1
    assert at.button(key="dxf_sparrow_run").label.endswith("kaikki laskettu")
    # A new quantity waits for the button: nothing is nested by itself.
    at.session_state["dxf_menu"] = BOM
    at.run()
    at.number_input(key="dxf_q_f0").set_value(12).run()
    at.session_state["dxf_menu"] = NESTING
    at.run()
    assert not at.exception
    assert not at.button(key="dxf_sparrow_run").disabled


def test_a_dxf_part_is_nested_for_the_largest_batch_size_unless_typed():
    import ezdxf

    from core.dxf import read_dxf
    from tests.conftest import dxf_bytes

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.session_state["dxf_files"] = {"f0": read_dxf(dxf_bytes(doc), "p.dxf")}
    at.session_state["menu"] = FILES
    at.run()
    assert at.number_input(key="dxf_q_f0").value == 10
    at.text_input(key="quote_batch_sizes").set_value("2 25").run()
    assert at.number_input(key="dxf_q_f0").value == 25
    # A typed quantity wins, and stays when the screen changes.
    at.number_input(key="dxf_q_f0").set_value(7).run()
    at.session_state["dxf_menu"] = NESTING
    at.run()
    at.session_state["dxf_menu"] = BOM
    at.run()
    assert at.number_input(key="dxf_q_f0").value == 7
    at.text_input(key="quote_batch_sizes").set_value("30").run()
    assert at.number_input(key="dxf_q_f0").value == 7
    # Clearing it brings the automatic quantity back, which then follows again.
    at.number_input(key="dxf_q_f0").set_value(None).run()
    assert at.number_input(key="dxf_q_f0").value == 30
    at.text_input(key="quote_batch_sizes").set_value("40").run()
    assert at.number_input(key="dxf_q_f0").value == 40


def test_an_unpriced_part_leaves_the_money_columns_numeric():
    # A part with no €/tn yet (one priced, one not): its cells are empty, not
    # "", so the columns stay numbers (a mixed column fails Arrow, then warns).
    import pandas as pd

    def page():
        from core.work import WorkInputs, WorkRates, work_cost
        from view.common import render_pieces_summary

        RATES = WorkRates()

        prods = [{"id": i, "name": f"p{i}", "material": "S235", "thickness": "2",
                  "width": 100.0, "height": 50.0, "qty": 2, "work": WorkInputs()}
                 for i in ("a", "b")]
        render_pieces_summary(prods, {"a": 900.0}, weight_label="kg", lead="Osa",
                              work={"a": work_cost(WorkInputs(), 2, RATES),
                                    "b": work_cost(WorkInputs(), 2, RATES)})

    at = AppTest.from_function(page).run()
    assert not at.exception
    assert len(at.dataframe) == 2
    for frame in at.dataframe:
        for col in frame.value.columns:
            if "€" in col:
                assert pd.api.types.is_numeric_dtype(frame.value[col]), col


def test_clear_all_empties_the_quote_and_keeps_the_rates():
    import ezdxf

    from core.dxf import read_dxf
    from tests.conftest import dxf_bytes

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.session_state["dxf_files"] = {"f0": read_dxf(dxf_bytes(doc), "p.dxf")}
    at.run()
    at.text_input(key="quote_customer").set_value("Asiakas Oy").run()
    at.number_input(key="work_weld_eur_per_h").set_value(99.0).run()
    at.button(key="ui_clear_all").click().run()
    assert not at.exception
    assert not at.session_state["dxf_files"]
    assert at.text_input(key="quote_customer").value == ""
    assert at.number_input(key="work_weld_eur_per_h").value == 99.0     # the sidebar stays
    assert not any(n.key == "dxf_q_f0" for n in at.number_input)
    assert not any(b.key == "ui_undo_do" for b in at.button)       # nothing more to answer


def test_nestaus_choices_stay_when_going_to_another_step_and_back(tmp_path, monkeypatch):
    import json

    import ezdxf

    from core import suppliers
    from core.dxf import read_dxf
    from tests.conftest import dxf_bytes

    monkeypatch.chdir(tmp_path)
    rows = [{"Paksuus (mm)": "2", "S235 | 1000x2000": 900.0}]
    json.dump({"data": {"levyt": rows}, "source_file": "t.pdf", "updated_at": "x"},
              open(tmp_path / suppliers.SUPPLIERS[0].path, "w"))
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
    data = dxf_bytes(doc)
    at = AppTest.from_file("../app.py", default_timeout=120)
    at.session_state["dxf_files"] = {f"f{i}": read_dxf(data, f"p{i}.dxf") for i in range(2)}
    at.session_state["menu"] = FILES
    at.run()
    for i in range(2):
        at.selectbox(key=f"dxf_mat_f{i}").select("S235").run()
        at.selectbox(key=f"dxf_th_f{i}").select("2").run()
    at.session_state["dxf_menu"] = NESTING
    at.run()
    at.pills(key="dxf_turn").set_value([0]).run()
    at.toggle(key="dxf_angle_per_part").set_value(True).run()
    at.pills(key="dxf_turn_f1").set_value([90]).run()
    for screen in (BOM, PRICING, NESTING):
        at.session_state["dxf_menu"] = screen
        at.run()
        assert not at.exception
    assert at.toggle(key="dxf_angle_per_part").value
    assert at.pills(key="dxf_turn_f1").value == [90]
    at.toggle(key="dxf_angle_per_part").set_value(False).run()
    assert at.pills(key="dxf_turn").value == [0]


def test_every_read_only_widget_is_left_out_of_keeping():
    # Every input is kept between steps (``view.dxf_tab.keep_inputs``), but a
    # button's, download's, upload's or editor's value can't be set: their
    # keys must start with one of ``view.ui._BUTTONS``.
    import ast
    import pathlib

    from view.ui import _BUTTONS

    read_only = {"button", "download_button", "file_uploader", "data_editor"}
    for path in pathlib.Path(__file__).parent.parent.joinpath("view").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in read_only):
                continue
            key = next((k.value for k in node.keywords if k.arg == "key"), None)
            if isinstance(key, ast.JoinedStr):
                key = key.values[0]
            if isinstance(key, ast.Constant):   # an f-string's start: its listed ends
                assert any(str(key.value).startswith(b) or b.startswith(str(key.value))
                           for b in _BUTTONS), f"{path.name}: {key.value}"
