"""The DXF tab's result cache: Sparrow settings are saved with a result, not
part of its key, so changing one keeps every result for per-group re-nesting."""

from view.common import SheetSettings
from view.dxf_tab import _nesting_diff, _run_label, _Settings, _sig


def settings(**kw) -> _Settings:
    base = dict(nest_mode="separate", sheet=SheetSettings(), time_limit=4, margin_pct=15.0)
    return _Settings(**{**base, **kw})


PRODS = [{"id": "a", "qty": 2, "angles": (0, 90)}]
KEY = ("S235", "2", "a")


def test_sparrow_settings_do_not_change_the_cache_key():
    assert _sig(KEY, PRODS, settings()) == _sig(KEY, PRODS, settings(
        time_limit=10, sheet=SheetSettings(rankavali_mm=5, edges_mm=(10, 0, 0, 0))))


def test_a_single_part_keeps_its_result_when_the_nesting_mode_changes():
    combined, separate = ("S235", "2"), ("S235", "2", "a")
    assert _sig(combined, PRODS, settings()) == _sig(separate, PRODS, settings())
    two = PRODS + [{"id": "b", "qty": 1, "angles": (0, 90)}]
    assert _sig(combined, two, settings()) != _sig(separate, PRODS, settings())


def test_a_quantity_changes_the_cache_key_the_margin_does_not():
    # The margin only prices a nesting: the result is kept and priced again.
    assert _sig(KEY, PRODS, settings()) == _sig(KEY, PRODS, settings(margin_pct=20.0))
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, [{**PRODS[0], "qty": 3}], settings())


def test_a_nesting_angle_change_re_nests_the_group():
    fixed = [{**PRODS[0], "angles": (0,)}]
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, fixed, settings())


def test_a_price_set_later_nests_a_changed_price_only_prices():
    # Copper first unpriced has no sheet size to nest on: priced later, it is
    # nested. A changed €/kg keeps the nesting (it is priced again), as does a
    # price for another material.
    from core.pricing import (COPPER_MATERIAL, COPPER_THICKNESSES, build_copper_section,
                              build_lookup)

    def copper(price_kg, **other):
        return settings(lookup=build_lookup({**build_copper_section(price_kg), **other}))

    key, thickness = (COPPER_MATERIAL, COPPER_THICKNESSES[0]), COPPER_THICKNESSES[0]
    unpriced, priced = _sig(key, PRODS, copper(None)), _sig(key, PRODS, copper(15.5))
    assert unpriced != priced                              # set later: nest it
    assert priced == _sig(key, PRODS, copper(15.9))       # changed: price it again
    steel = {"tata": [{"Paksuus (mm)": thickness, "S235 | 1000x2000": 900.0}]}
    assert priced == _sig(key, PRODS, copper(15.5, **steel))   # other material: kept


def test_a_thickness_or_material_with_the_same_sheet_sizes_keeps_the_nesting():
    lookup = {(t, f"{m} | 1000x2000"): 900.0 for t in ("2", "3") for m in ("S235", "S355")}
    keep = settings(lookup=lookup)
    assert _sig(("S235", "2"), PRODS, keep) == _sig(("S235", "3"), PRODS, keep)
    assert _sig(("S235", "2"), PRODS, keep) == _sig(("S355", "2"), PRODS, keep)


def test_nesting_diff_names_only_the_changed_settings():
    old, new = settings().nesting(), settings(time_limit=10).nesting()
    assert _nesting_diff(old, new) == "hakuaika 4 s (nyt 10 s)"
    assert _nesting_diff(new, new) == ""


def test_edge_gaps_read_as_one_value_or_per_edge():
    old = settings().nesting()
    same = settings(sheet=SheetSettings(edges_mm=(10, 10, 10, 10))).nesting()
    mixed = settings(sheet=SheetSettings(edges_mm=(10, 0, 5, 5))).nesting()
    assert _nesting_diff(old, same) == "reunavara 0 mm (nyt 10 mm)"
    assert _nesting_diff(old, mixed) == (
        "reunavara 0 mm (nyt ylä 10, ala 0, vasen 5, oikea 5 mm)")


def test_edges_map_to_the_packers_gaps_in_order():
    gaps = SheetSettings(edges_mm=(1, 2, 3, 4)).gaps()
    assert (gaps.top, gaps.bottom, gaps.left, gaps.right) == (1, 2, 3, 4)


def test_run_label_counts_only_new_groups():
    assert _run_label(3, 0, 3) == "Laske levykäyttö"
    assert _run_label(2, 0, 3) == "Laske levykäyttö — 2 uutta"
    assert _run_label(1, 2, 4) == "Laske levykäyttö — 1 uusi"
    assert _run_label(0, 1, 2) == "Laske levykäyttö — ei uusia osia"
    assert _run_label(0, 0, 2) == "Laske levykäyttö — kaikki laskettu"


def test_dropped_files_become_cards_and_poista_removes_one():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace

        import ezdxf
        import streamlit as st

        from tests.conftest import dxf_bytes
        from view.dxf_tab import _STORE, _evict, _ingest

        doc = ezdxf.new()
        doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
        data = dxf_bytes(doc)
        drop = st.session_state.pop("drop", [])
        files = [SimpleNamespace(file_id=i, name=f"{i}.dxf", getvalue=lambda: data) for i in drop]
        if st.session_state.get("remove"):
            _evict(st.session_state.pop("remove"))
        st.session_state["new"] = _ingest(files)
        st.session_state["shown"] = list(st.session_state.get(_STORE, {}))

    at = AppTest.from_function(page)
    at.session_state["drop"] = ["a", "b"]
    at.run()
    assert at.session_state["new"] and at.session_state["shown"] == ["a", "b"]
    assert at.session_state["dxf_inbox"] == 1                # the drop area starts over
    at.session_state["remove"] = "a"
    at.run()
    assert at.session_state["shown"] == ["b"]
    assert not at.session_state["new"]
    at.session_state["drop"] = ["a2"]                        # dropped again
    at.run()
    assert at.session_state["shown"] == ["b", "a2"]


def test_a_file_dropped_again_is_warned_about_and_not_added():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace
        from unittest import mock

        import ezdxf
        import streamlit as st

        from tests.conftest import dxf_bytes
        from view.dxf_tab import _evict, _render_drop_area

        doc = ezdxf.new()
        doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
        data = dxf_bytes(doc)
        drop = st.session_state.pop("drop", [])
        files = [SimpleNamespace(file_id=i, name=n, getvalue=lambda: data) for i, n in drop]
        if st.session_state.get("remove"):
            _evict(st.session_state.pop("remove"))
        with mock.patch.object(st, "file_uploader", return_value=files):
            st.session_state["shown"] = [d.name for _, d in _render_drop_area()]

    at = AppTest.from_function(page)
    at.session_state["drop"] = [("a", "x.dxf"), ("b", "y.dxf"), ("c", "x.dxf")]
    at.run()
    assert at.session_state["shown"] == ["x.dxf", "y.dxf"]   # the second x.dxf skipped
    assert "x.dxf" in at.warning[0].value and "osa #1" in at.warning[0].value
    at.session_state["drop"] = [("d", "y.dxf"), ("e", "z.dxf")]
    at.run()
    assert at.session_state["shown"] == ["x.dxf", "y.dxf", "z.dxf"]
    assert "y.dxf" in at.warning[0].value and "osa #2" in at.warning[0].value
    at.run()
    assert "y.dxf" in at.warning[0].value                     # still there on a click
    names, _until = at.session_state["dxf_dupes"]
    at.session_state["dxf_dupes"] = (names, 0.0)               # 15 s later
    at.run()
    assert not at.warning
    at.session_state["remove"] = "a"
    at.session_state["drop"] = [("f", "x.dxf")]               # removed, then dropped again
    at.run()
    assert at.session_state["shown"] == ["y.dxf", "z.dxf", "x.dxf"] and not at.warning


def dxf_page(n):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("dxf_page.py")
    at.session_state["n"] = n
    return at.run()


def test_one_part_takes_its_nesting_angle_from_the_run_row():
    at = dxf_page(1)
    assert [c.key for c in at.checkbox if c.key.startswith("dxf_angle")] == ["dxf_angle_0", "dxf_angle_90"]   # none on the card
    assert not at.radio
    at.checkbox(key="dxf_angle_90").uncheck().run()
    assert at.session_state["angles"] == [(0,)]


def test_several_parts_share_one_angle_or_choose_per_part():
    at = dxf_page(2)
    assert at.radio(key="dxf_angle_mode").value == "Sama kaikille"
    at.checkbox(key="dxf_angle_0").uncheck().run()
    assert at.session_state["angles"] == [(90,), (90,)]
    at.radio(key="dxf_angle_mode").set_value("Osakohtainen").run()
    assert {c.key for c in at.checkbox if c.key.startswith("dxf_angle")} == {"dxf_angle_0_f0", "dxf_angle_90_f0",
                                            "dxf_angle_0_f1", "dxf_angle_90_f1"}
    at.checkbox(key="dxf_angle_90_f1").uncheck().run()
    assert at.session_state["angles"] == [(0, 90), (0,)]


def fill_first_card(at):
    at.selectbox(key="dxf_mat_f0").set_value("S235").run()
    return at.selectbox(key="dxf_th_f0").set_value("2").run()


def test_the_first_material_can_be_given_to_the_other_parts(caplog):
    import logging

    import streamlit.elements.lib.policies as policies

    at = fill_first_card(dxf_page(3))
    assert any("samaa materiaalia" in m.value for m in at.markdown)
    policies._LOGGER.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            at.button(key="dxf_fill_yes_f1").click().run()
    finally:
        policies._LOGGER.removeHandler(caplog.handler)
    assert at.session_state["materials"] == [("S235", "2")] * 3
    assert not any("samaa materiaalia" in m.value for m in at.markdown)   # gone
    # the other cards' selectboxes are rebuilt, not set: no Streamlit warning
    assert not [r for r in caplog.records if "Session State" in r.getMessage()]


def test_no_keeps_the_others_empty_and_stops_asking():
    at = fill_first_card(dxf_page(2))
    at.button(key="dxf_fill_no_f1").click().run()
    assert at.session_state["materials"] == [("S235", "2"), (None, None)]
    at.selectbox(key="dxf_mat_f1").set_value("S235").run()
    assert not any("samaa materiaalia" in m.value for m in at.markdown)


def fill_questions(at):
    return [b.key for b in at.button if b.key.startswith("dxf_fill_yes_")]


def page_order(at) -> list[str]:
    """The cards (``#1``…) and the question (``?``) from the top of the page."""
    order = []
    for m in at.markdown:
        if "samaa materiaalia" in m.value:
            order.append("?")
        elif m.value.startswith("**#"):
            order.append(m.value[2:].split("**")[0])
    return order


def test_the_question_is_asked_once_between_the_filled_and_the_next_card():
    at = fill_first_card(dxf_page(3))
    assert fill_questions(at) == ["dxf_fill_yes_f1"]
    assert page_order(at) == ["#1", "?", "#2", "#3"]
    assert any("osille (2 kpl)" in m.value and "osalla #1" in m.value for m in at.markdown)


def test_new_files_are_asked_about_on_the_first_new_card_only():
    at = fill_first_card(dxf_page(2))
    at.button(key="dxf_fill_yes_f1").click().run()
    at.session_state["add"] = 2
    at.run()
    assert at.session_state["materials"][2:] == [(None, None)] * 2
    assert fill_questions(at) == ["dxf_fill_yes_f2"]          # once, not per filled card
    assert page_order(at) == ["#1", "#2", "?", "#3", "#4"]    # before the new cards
    at.button(key="dxf_fill_yes_f2").click().run()
    assert at.session_state["materials"] == [("S235", "2")] * 4
    assert not fill_questions(at)


def test_one_part_is_never_asked():
    at = fill_first_card(dxf_page(1))
    assert not any("samaa materiaalia" in m.value for m in at.markdown)


def test_poista_kaikki_removes_every_card_and_its_settings():
    assert "dxf_del_all" not in [b.key for b in dxf_page(1).button]   # one part: its own Poista
    at = fill_first_card(dxf_page(2))
    at.button(key="dxf_del_all").click().run()
    assert at.session_state["dxf_store"] == {} and not at.session_state["materials"]
    assert not at.session_state["dxf_part_config"]
    assert not [k for k in at.session_state if k.startswith(("dxf_mat_", "dxf_q_"))]


def test_a_margin_change_reprices_without_running_sparrow_again():
    # The same group nested twice, at 15 % and 20 % margin: the second time
    # every Sparrow question is answered from the session, and only the price
    # changes. A new search time asks Sparrow again.
    from streamlit.testing.v1 import AppTest

    def page():
        import ezdxf
        import streamlit as st

        import view.dxf_tab as tab
        from core.dxf import read_dxf
        from tests.conftest import dxf_bytes
        from tests.test_sparrow import fake_solver
        from view.common import SheetSettings

        runs = st.session_state.setdefault("runs", [])

        def counting(instance, *, executable, time_limit_sec, min_item_separation):
            runs.append(time_limit_sec)
            return fake_solver(instance, separation=min_item_separation)

        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
        report = read_dxf(dxf_bytes(doc), "p.dxf").part()
        prods = [{"id": "p", "name": "p.dxf", "report": report, "qty": 30, "angles": (0, 90)}]
        lookup = {("2", "S235 | 1000x2000"): 900.0}
        totals = st.session_state.setdefault("totals", [])
        real, tab.run_sparrow = tab.run_sparrow, counting
        try:
            for margin, time_limit in ((15.0, 4), (20.0, 4), (20.0, 8)):
                settings = tab._Settings("combined", SheetSettings(), time_limit, margin, lookup)
                entry = tab._nester(lookup, settings, "sparrow")(("S235", "2"), prods)
                totals.append((len(runs), round(entry["result"].options[0].total_eur, 2)))
        finally:
            tab.run_sparrow = real

    at = AppTest.from_function(page).run()
    assert not at.exception
    (n15, eur15), (n20, eur20), (n8s, _) = at.session_state["totals"]
    assert n15 > 0 and n20 == n15                     # margin change: no new runs
    assert eur20 > eur15                              # but the new price
    assert n8s > n20                                  # a new search time runs again


def test_price_only_changes_update_without_the_calculate_button():
    # Calculated once; then the margin, the thickness and one sheet size's
    # €/tn change — each shows at once (re-priced, or planned again from the
    # remembered runs), never running Sparrow. A new quantity waits.
    from streamlit.testing.v1 import AppTest

    def page():
        import ezdxf
        import streamlit as st

        import view.dxf_tab as tab
        from core.dxf import read_dxf
        from core.sheet_cost import group_products
        from tests.conftest import dxf_bytes
        from tests.test_sparrow import fake_solver
        from view.common import SheetSettings

        runs = st.session_state.setdefault("runs", [0])

        def counting(instance, *, executable, time_limit_sec, min_item_separation):
            runs[0] += 1
            return fake_solver(instance, separation=min_item_separation)

        step = st.session_state.get("step", {})
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        doc.modelspace().add_lwpolyline([(0, 0), (300, 0), (300, 200), (0, 200)], close=True)
        report = read_dxf(dxf_bytes(doc), "p.dxf").part()
        lookup = {}
        for t in ("2", "3"):
            lookup[(t, "S235 | 1000x2000")] = 900.0
            lookup[(t, "S235 | 1250x2500")] = step.get("price", 870.0)
        prods = [{"id": "p", "name": "p.dxf", "material": "S235",
                  "thickness": step.get("thickness", "2"), "width": 300, "height": 200,
                  "qty": step.get("qty", 40), "angles": (0, 90), "report": report}]
        settings = tab._Settings("combined", SheetSettings(), 4, step.get("margin", 15.0), lookup)
        cache = st.session_state.setdefault(tab._CACHE, {})
        real, tab.run_sparrow = tab.run_sparrow, counting
        try:
            st.session_state["replanned"] = tab._show(
                prods, group_products(prods, "combined"), settings, cache,
                tab._nester(lookup, settings, "sparrow"),
                lambda s: tab._nester(lookup, s, "sparrow", remembered_only=True),
                run=step.get("run", False))
        finally:
            tab.run_sparrow = real

    def show(at, **step):
        at.session_state["step"] = step
        at.run()
        assert not at.exception
        total = [m.value for m in at.metric if m.label == "Yhteensä €"]
        waiting = any("Odottaa laskentaa" in c.value for c in at.caption)
        return at.session_state["runs"][0], (total[0] if total else None), waiting

    at = AppTest.from_function(page, default_timeout=60)
    runs, first, _ = show(at, run=True)
    assert runs > 0 and first
    assert show(at, margin=20.0)[:2] != (runs, first) and show(at, margin=20.0)[0] == runs
    assert not at.session_state["replanned"]                      # just re-priced
    assert show(at, thickness="3")[0] == runs                     # thickness: re-priced
    after, total, waiting = show(at, price=500.0)                 # uneven: planned again
    assert after == runs and total and not waiting and at.session_state["replanned"]
    assert show(at, qty=41)[2]                                    # new quantity: the button


def test_a_pdf_links_to_its_part_and_its_material_can_be_applied(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from core import materials

    aliases = tmp_path / "aliases.json"     # "Muista valinnat" writes here
    monkeypatch.setattr(materials, "ALIAS_FILE", aliases)

    at = AppTest.from_file("dxf_docs_page.py", default_timeout=30).run()
    assert not at.exception
    assert "dxf_checked_f0" in [c.key for c in at.checkbox]
    assert any("100325-revA.pdf" in e.label for e in at.expander)     # on card #1
    at.text_area(key="dxf_rfq_note").set_value("Muut osat S235 2 mm").run()
    at.button(key="dxf_find_materials").click().run()
    at.button(key="dxf_apply_materials").click().run()
    assert at.session_state["materials"] == [("S355MC", "3"), ("S235", "2")]
    assert aliases.exists()


def test_a_chain_picks_its_calculators_on_the_card():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("dxf_docs_page.py", default_timeout=30).run()
    at.selectbox(key="work_chain_f0").set_value("Laser-Särmäys-Maalaus").run()
    assert at.multiselect(key="work_calcs_f0").value == ["sarmays", "maalaus"]
    assert not at.exception


def test_a_parts_numbered_files_and_drawing_make_one_card():
    from streamlit.testing.v1 import AppTest

    def page():
        import io
        from types import SimpleNamespace

        import ezdxf
        import streamlit as st
        from reportlab.pdfgen import canvas

        from tests.conftest import dxf_bytes
        from view.dxf_tab import _STORE, _ingest, _render_part_cards

        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 4
        doc.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
        data = dxf_bytes(doc)
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf)
        pdf.drawString(72, 720, "3AUA0000098622")
        pdf.save()
        if not st.session_state.get(_STORE):
            _ingest([SimpleNamespace(file_id=f"f{i}", name=f"3aua0000098622_{i}.dxf",
                                     getvalue=lambda: data) for i in (1, 2)]
                    + [SimpleNamespace(file_id="d", name="3AUA0000098622.pdf",
                                       getvalue=lambda: buf.getvalue())])
        _render_part_cards(list(st.session_state[_STORE].items()), ["S235"],
                           {("2", "S235 | 1000x2000"): 900.0}, False)

    at = AppTest.from_function(page, default_timeout=30).run()
    assert not at.exception
    assert list(at.session_state["dxf_store"]) == ["f1"]
    assert sum("3AUA0000098622.pdf" in e.label for e in at.expander) == 1
    files = next(c.value for c in at.caption if c.value.startswith("Tiedostot:"))
    assert "✓ 3aua0000098622_1.dxf" in files and "3aua0000098622_2.dxf" in files


def test_a_unitless_dxf_is_confirmed_by_its_pdf_drawing():
    from streamlit.testing.v1 import AppTest

    def page():
        import io
        from types import SimpleNamespace

        import ezdxf
        import streamlit as st
        from reportlab.pdfgen import canvas

        from tests.conftest import dxf_bytes
        from view.dxf_tab import _STORE, _ingest, _render_part_cards

        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 0
        doc.modelspace().add_lwpolyline([(0, 0), (516.4, 0), (516.4, 393.3), (0, 393.3)],
                                        close=True)
        data = dxf_bytes(doc)
        buf = io.BytesIO()
        pdf = canvas.Canvas(buf)
        pdf.drawString(72, 720, "3AUA0000098622 (516.4)")
        pdf.save()
        if not st.session_state.get(_STORE):
            _ingest([SimpleNamespace(file_id="f", name="3aua0000098622_2.dxf",
                                     getvalue=lambda: data),
                     SimpleNamespace(file_id="d", name="3AUA0000098622.pdf",
                                     getvalue=lambda: buf.getvalue())])
        _render_part_cards(list(st.session_state[_STORE].items()), ["S235"],
                           {("2", "S235 | 1000x2000"): 900.0}, False)

    at = AppTest.from_function(page, default_timeout=30).run()
    assert not at.exception
    assert "dxf_unit_ok_f" not in [c.key for c in at.checkbox]
    assert any("vahvistettu: PDF-piirustuksen mitta 516.4 mm" in c.value for c in at.caption)


def test_empty_cards_get_their_drawings_material_once_and_conflicts_wait():
    from streamlit.testing.v1 import AppTest

    def page():
        from types import SimpleNamespace

        import streamlit as st

        from view import material_agent

        parts = [("a", SimpleNamespace(name="a.dxf", texts=["MAT=S355MC", "t=3 mm"])),
                 ("b", SimpleNamespace(name="b.dxf", texts=["MAT=S355MC", "MAT=AISI 304"])),
                 ("c", SimpleNamespace(name="c.dxf", texts=[], step_thickness=2.0))]
        applied = st.session_state.setdefault("applied", [])
        material_agent.auto_fill(
            parts, {}, {}, ["S355MC", "RST 304"], lambda m: ["2", "3"],
            lambda fid, m, t: applied.append((fid, m, t)),
            lambda fid: fid in st.session_state.get("filled", ()))
        st.session_state["to_check"] = [p.part_id for p in
                                        st.session_state.get("dxf_material_proposals") or []]

    at = AppTest.from_function(page, default_timeout=30).run()
    assert not at.exception
    assert at.session_state["applied"] == [("a", "S355MC", "3")]
    assert at.session_state["to_check"] == ["b"]
    at.session_state["filled"] = ()       # the user cleared card a: it stays clear
    at.run()
    assert at.session_state["applied"] == [("a", "S355MC", "3")]
