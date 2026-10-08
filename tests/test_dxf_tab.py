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


def test_margin_and_quantity_change_the_cache_key():
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, PRODS, settings(margin_pct=20.0))
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, [{**PRODS[0], "qty": 3}], settings())


def test_a_nesting_angle_change_re_nests_the_group():
    fixed = [{**PRODS[0], "angles": (0,)}]
    assert _sig(KEY, PRODS, settings()) != _sig(KEY, fixed, settings())


def test_a_price_set_or_changed_later_makes_the_group_new_again():
    from core.pricing import (COPPER_MATERIAL, COPPER_THICKNESSES, build_copper_section,
                              build_lookup)

    def copper(price_kg, **other):
        return settings(lookup=build_lookup({**build_copper_section(price_kg), **other}))

    key, thickness = (COPPER_MATERIAL, COPPER_THICKNESSES[0]), COPPER_THICKNESSES[0]
    unpriced, priced = _sig(key, PRODS, copper(None)), _sig(key, PRODS, copper(15.5))
    assert unpriced != priced                              # set later: nest again
    assert priced != _sig(key, PRODS, copper(15.9))       # changed: nest again
    steel = {"tata": [{"Paksuus (mm)": thickness, "S235 | 1000x2000": 900.0}]}
    assert priced == _sig(key, PRODS, copper(15.5, **steel))   # other material: kept


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
    assert not at.warning                                     # shown once
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
    assert [c.key for c in at.checkbox] == ["dxf_angle_0", "dxf_angle_90"]   # none on the card
    assert not at.radio
    at.checkbox(key="dxf_angle_90").uncheck().run()
    assert at.session_state["angles"] == [(0,)]


def test_several_parts_share_one_angle_or_choose_per_part():
    at = dxf_page(2)
    assert at.radio(key="dxf_angle_mode").value == "Sama kaikille"
    at.checkbox(key="dxf_angle_0").uncheck().run()
    assert at.session_state["angles"] == [(90,), (90,)]
    at.radio(key="dxf_angle_mode").set_value("Osakohtainen").run()
    assert {c.key for c in at.checkbox} == {"dxf_angle_0_f0", "dxf_angle_90_f0",
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
    assert any("sama materiaali" in m.value for m in at.markdown)
    policies._LOGGER.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING):
            at.button(key="dxf_fill_yes_f0").click().run()
    finally:
        policies._LOGGER.removeHandler(caplog.handler)
    assert at.session_state["materials"] == [("S235", "2")] * 3
    assert not any("sama materiaali" in m.value for m in at.markdown)   # gone
    # the other cards' selectboxes are rebuilt, not set: no Streamlit warning
    assert not [r for r in caplog.records if "Session State" in r.getMessage()]


def test_no_keeps_the_others_empty_and_stops_asking():
    at = fill_first_card(dxf_page(2))
    at.button(key="dxf_fill_no_f0").click().run()
    assert at.session_state["materials"] == [("S235", "2"), (None, None)]
    at.selectbox(key="dxf_mat_f1").set_value("S235").run()
    assert not any("sama materiaali" in m.value for m in at.markdown)


def test_one_part_is_never_asked():
    at = fill_first_card(dxf_page(1))
    assert not any("sama materiaali" in m.value for m in at.markdown)


def test_poista_kaikki_removes_every_card_and_its_settings():
    assert "dxf_del_all" not in [b.key for b in dxf_page(1).button]   # one part: its own Poista
    at = fill_first_card(dxf_page(2))
    at.button(key="dxf_del_all").click().run()
    assert at.session_state["dxf_store"] == {} and not at.session_state["materials"]
    assert not at.session_state["dxf_part_config"]
    assert not [k for k in at.session_state if k.startswith(("dxf_mat_", "dxf_q_"))]
