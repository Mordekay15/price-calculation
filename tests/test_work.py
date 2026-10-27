"""Work pricing: the laser table, the per-piece work cost (checked against
Stremet's worked examples), the per-sheet cutting minimum, and what the DXF
reader gives the work: cutting length, threads and bends."""

import math

import pytest

from core.dxf import read_dxf
from core.work import (
    PUNCH,
    LaserSpeed,
    WorkInputs,
    WorkRates,
    apply_sheet_minimum,
    laser_speed,
    phases,
    rect_cut_length_m,
    work_cost,
)
from tests.conftest import dxf_bytes, rect

RATES = WorkRates()


# ── Laser table ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("material, t, expected", [
    ("S235", 3, (12, 0.045)),                 # steel, nitrogen
    ("KU-SI DX51D+Z275", 10, (45, 0.05)),     # steel, oxygen
    ("RST 2B", 8, (46, 0.045)),
    ("HST 2B", 1, (8, 0.045)),
    ("Alumiini 5754", 3, (20, 0.05)),
    ("KUPARI", 4, (21, 0.05)),
])
def test_speeds_come_from_the_table(material, t, expected):
    speed = laser_speed(material, t)
    assert (speed.s_per_m, speed.eur_per_s) == expected


def test_a_thickness_between_rows_is_interpolated():
    assert laser_speed("S235", 2.5).s_per_m == pytest.approx(9.5)    # 7 … 12


def test_between_nitrogen_and_oxygen_the_thicker_row_is_used():
    assert laser_speed("S235", 7) == LaserSpeed(37, 0.05)


def test_thinner_than_the_table_takes_its_first_row_and_thicker_has_none():
    assert laser_speed("S235", 0.7) == LaserSpeed(7, 0.045)
    assert laser_speed("KUPARI", 8) is None
    assert laser_speed("RST 2B", 12) is None


# ── Stremet's worked examples ─────────────────────────────────────────────────

def test_cencorp_rst_3mm_four_pieces():
    # laser 11 s, one bend, five threads, three phases → 80 € setup over 4 pcs
    inp = WorkInputs(cut_length_m=1, laser=LaserSpeed(11, 0.045), bends=1, threads=5)
    cost = work_cost(inp, 4, RATES)
    assert phases(inp) == 3 and cost.setup == 80
    assert 7.8 + cost.per_piece == pytest.approx(29.5, abs=0.01)


def test_mankeli_punch_100_pieces():
    # 8 hits, two long bends at 0.5 €, cutting + bending → 60 € setup
    inp = WorkInputs(method=PUNCH, hits=8, bends=2, eur_per_bend=0.5)
    assert 3.8 + work_cost(inp, 100, RATES).per_piece == pytest.approx(5.464)


def test_5610542_steel_20mm_70_pieces():
    inp = WorkInputs(cut_length_m=1, laser=LaserSpeed(300, 0.045))
    assert 31.4 + work_cost(inp, 70, RATES).per_piece == pytest.approx(45.47, abs=0.01)


# ── The rest of the work ──────────────────────────────────────────────────────

def test_welding_is_metres_over_metres_per_hour_times_the_hourly_rate():
    cost = work_cost(WorkInputs(weld_m=0.75), 1, RATES)
    assert cost.welding == pytest.approx(30)          # 0.75 m / 1.5 m/h × 60 €/h


def test_setup_follows_the_phases_unless_set():
    assert work_cost(WorkInputs(), 1, RATES).setup == 40
    assert work_cost(WorkInputs(bends=2), 1, RATES).setup == 60
    assert work_cost(WorkInputs(bends=2, threads=1, weld_m=1), 1, RATES).setup == 80
    assert work_cost(WorkInputs(setup_eur=25), 1, RATES).setup == 25


def test_batch_costs_shrink_per_piece_with_the_quantity():
    inp = WorkInputs(setup_eur=80)
    assert work_cost(inp, 1, RATES).per_piece == 80
    assert work_cost(inp, 100, RATES).per_piece == pytest.approx(0.8)


def test_laser_without_a_speed_is_unpriced():
    assert not work_cost(WorkInputs(cut_length_m=1), 1, RATES).priced


# ── Cutting minimum per sheet ─────────────────────────────────────────────────

def test_cutting_is_raised_to_the_minimum_per_sheet_in_proportion():
    a = work_cost(WorkInputs(method=PUNCH, hits=100), 10, RATES)    # 0.8 €/kpl, 8 €
    b = work_cost(WorkInputs(method=PUNCH, hits=50), 8, RATES)      # 0.4 €/kpl, 3.2 €
    a2, b2 = apply_sheet_minimum([a, b], 2, RATES)                  # floor 14 €
    assert a2.cut_minimum * 10 + b2.cut_minimum * 8 == pytest.approx(14 - 11.2)
    assert a2.cut_minimum / a.cutting == pytest.approx(b2.cut_minimum / b.cutting)


def test_cutting_above_the_minimum_is_left_as_it_is():
    a = work_cost(WorkInputs(method=PUNCH, hits=1000), 10, RATES)   # 80 €
    assert apply_sheet_minimum([a], 2, RATES) == [a]


def test_with_no_cutting_the_minimum_is_shared_by_pieces():
    a, b = work_cost(WorkInputs(), 3, RATES), work_cost(WorkInputs(), 1, RATES)
    a2, b2 = apply_sheet_minimum([a, b], 1, RATES)
    assert a2.cut_minimum == b2.cut_minimum == pytest.approx(7 / 4)


# ── From the drawing ──────────────────────────────────────────────────────────

def test_rectangle_cut_length():
    assert rect_cut_length_m(300, 200) == 1.0


def test_dxf_gives_cut_length_threads_and_bends(drawing):
    doc, msp = drawing()
    rect(msp, 0, 0, 300, 200)
    msp.add_circle((100, 100), 10)                       # plain hole
    msp.add_circle((200, 100), 1.65)                     # M4 tap hole …
    msp.add_arc((200, 100), 2.0, 255, 195)               # … with its thread mark
    msp.add_line((150, 0), (150, 200), dxfattribs={"layer": "IV_BEND_DOWN"})
    msp.add_line((140, 0), (140, 200), dxfattribs={"layer": "IV_BEND_EXTENT"})
    report = read_dxf(dxf_bytes(doc), "part.dxf").part()
    assert report.threads == 1 and report.bends == 1
    expected = 1000 + 2 * math.pi * 10 + 2 * math.pi * 1.65
    assert report.cut_length_mm == pytest.approx(expected, rel=1e-2)


# ── On the page ───────────────────────────────────────────────────────────────

def test_manual_tab_prices_work_per_piece(tmp_path, monkeypatch):
    # One 300 × 200 S235 2 mm plate × 10 with two bends: laser 1 m × 7 s/m ×
    # 0.045 €/s = 0.315 €/kpl is under 7 € for its one sheet, so cutting is
    # 7 €; bends 10 × 2 × 0.2 = 4 €; cutting + bending → 60 € setup.
    import json
    from streamlit.testing.v1 import AppTest
    from core import suppliers

    monkeypatch.chdir(tmp_path)
    rows = [{"Paksuus (mm)": "2", "S235 | 1000x2000": 900.0}]
    json.dump({"data": {"levyt": rows}, "source_file": "t.pdf", "updated_at": "x"},
              open(tmp_path / suppliers.SUPPLIERS[0].path, "w"))
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    pid = at.session_state["calc_products"][0]["id"]
    at.selectbox(key=f"mat_{pid}").select("S235").run()
    at.selectbox(key=f"th_{pid}").select("2").run()
    for key, value in (("w", 300.0), ("h", 200.0), ("q", 10), ("work_bends", 2.0)):
        at.number_input(key=f"{key}_{pid}").set_value(value).run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Työ yhteensä (€)"] == "71.00"
    table = next(d.value for d in at.dataframe if "Leikkaus €/kpl" in d.value.columns)
    row = table.iloc[0]
    assert row["Leikkaus €/kpl"] == 0.7 and row["Asetus ym. €/kpl"] == 6.0
    # The batch-size table prices the nested quantity (10) exactly as above.
    batches = next(d.value for d in at.dataframe if "10 kpl, €/kpl" in d.value.columns)
    assert batches.iloc[0]["10 kpl, €/kpl"] == row["Kpl-hinta €"]
    assert batches.iloc[0]["1 kpl, €/kpl"] > batches.iloc[0]["10 kpl, €/kpl"]


def test_a_fastener_put_in_costs_stremets_minimum_by_default():
    from core.work import FASTENER_INSERT_EUR, fastener_price
    assert fastener_price("RST 2B") + FASTENER_INSERT_EUR == pytest.approx(1.00)
    assert fastener_price("S235") + FASTENER_INSERT_EUR == pytest.approx(0.25)


def test_fasteners_are_counted_and_priced_as_count_times_part_plus_insert():
    from core.dxf import fasteners_from_texts
    assert fasteners_from_texts(["M5-PURISTEMUTTERIT X4KPL", "M4-PURISTEMUTTERIT 5KPL",
                            "MATERIAALI: CR2-11 PL1,5 sinkitty"]) == 9
    assert fasteners_from_texts(["M6 PURISTERUUVIT 2 KPL", "PULTIT X3 KPL"]) == 5
    inp = WorkInputs(fasteners=9, eur_per_fastener=0.3, eur_per_insert=0.2)
    cost = work_cost(inp, 1, RATES)
    assert cost.fasteners == pytest.approx(4.5) and phases(inp) == 2


def test_a_missing_pdf_package_hides_its_download_not_the_page(tmp_path, monkeypatch):
    # Updated code run before `pip install -r requirements.txt`: no reportlab.
    import builtins
    import json
    from streamlit.testing.v1 import AppTest
    from core import suppliers

    real_import = builtins.__import__

    def no_reportlab(name, *args, **kwargs):
        if name.split(".")[0] == "reportlab":
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    monkeypatch.chdir(tmp_path)
    rows = [{"Paksuus (mm)": "2", "S235 | 1000x2000": 900.0}]
    json.dump({"data": {"levyt": rows}, "source_file": "t.pdf", "updated_at": "x"},
              open(tmp_path / suppliers.SUPPLIERS[0].path, "w"))
    monkeypatch.setattr(builtins, "__import__", no_reportlab)
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    pid = at.session_state["calc_products"][0]["id"]
    at.selectbox(key=f"mat_{pid}").select("S235").run()
    at.selectbox(key=f"th_{pid}").select("2").run()
    for key, value in (("w", 300.0), ("h", 200.0), ("q", 10)):
        at.number_input(key=f"{key}_{pid}").set_value(value).run()
    assert not at.exception
    assert any("10 kpl, €/kpl" in d.value.columns for d in at.dataframe)
    assert any("Asenna reportlab" in c.value for c in at.caption)
