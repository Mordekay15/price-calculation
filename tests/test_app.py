"""Smoke test: the whole page renders without an exception."""

from streamlit.testing.v1 import AppTest


def test_app_renders_the_menu_and_each_screen():
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Peruslaskuri", "Edistynyt"]
    at.session_state["menu"] = "Edistynyt"
    at.run()
    assert [t.label for t in at.tabs] == ["Peruslaskuri", "Edistynyt", "BOM & Tiedostot",
                                          "Nestaus", "Hinnoittelu"]
    for screen in ("Nestaus", "Hinnoittelu", "BOM & Tiedostot"):
        at.session_state["dxf_menu"] = screen
        at.run()
        assert not at.exception
    at.session_state["dxf_menu"] = "Nestaus"
    at.run()
    at.session_state["menu"] = "Peruslaskuri"
    at.run()
    assert not at.exception and len(at.tabs) == 2
    at.session_state["menu"] = "Edistynyt"
    at.run()
    assert at.session_state["dxf_menu"] == "Nestaus"        # back on the same screen


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
    at.session_state["menu"] = "Edistynyt"
    at.run()
    at.selectbox(key="dxf_mat_f0").select("S235").run()
    at.selectbox(key="dxf_th_f0").select("2").run()
    at.session_state["dxf_menu"] = "Hinnoittelu"
    at.run()
    at.number_input(key="work_bends_f0").set_value(3.0).run()
    assert any("Laske levykäyttö ensin" in i.value for i in at.info)
    at.session_state["dxf_menu"] = "Nestaus"
    at.run()
    at.number_input(key="dxf_sparrow_t").set_value(1).run()
    at.button(key="dxf_sparrow_run").click().run()
    assert not at.exception
    assert not any(n.key == "work_bends_f0" for n in at.number_input)   # not drawn here
    assert not any("kpl, €/kpl" in "".join(d.value.columns) for d in at.dataframe)
    at.session_state["dxf_menu"] = "Hinnoittelu"
    at.run()
    assert not at.exception
    assert at.number_input(key="work_bends_f0").value == 3.0
    batches = next(d.value for d in at.dataframe if "10 kpl, €/kpl" in d.value.columns)
    assert len(batches) == 1
    at.session_state["dxf_menu"] = "BOM & Tiedostot"
    at.run()
    assert at.selectbox(key="dxf_mat_f0").value == "S235"
    assert "Määrä: **10 kpl**" in [m.value for m in at.markdown]   # the largest batch
    at.session_state["dxf_menu"] = "Nestaus"
    at.run()
    assert at.number_input(key="dxf_sparrow_t").value == 1
    assert at.button(key="dxf_sparrow_run").label.endswith("kaikki laskettu")


def test_a_dxf_part_is_nested_for_the_largest_batch_size():
    import ezdxf

    from core.dxf import read_dxf
    from tests.conftest import dxf_bytes

    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    doc.modelspace().add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.session_state["dxf_files"] = {"f0": read_dxf(dxf_bytes(doc), "p.dxf")}
    at.session_state["menu"] = "Edistynyt"
    at.run()
    assert "Määrä: **10 kpl**" in [m.value for m in at.markdown]
    at.text_input(key="quote_batch_sizes").set_value("2 25").run()
    assert "Määrä: **25 kpl**" in [m.value for m in at.markdown]
