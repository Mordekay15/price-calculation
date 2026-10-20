"""One card per item: grouping a part's files and choosing its shape."""

from types import SimpleNamespace

from core.items import (Choice, Source, choose, confirm_unit, group_sources, score_dxf,
                        score_step)


def _report(w, h, dropped=0, problems=()):
    return SimpleNamespace(problems=list(problems), dropped=[[]] * dropped,
                           dropped_count=dropped,
                           outline=SimpleNamespace(width_mm=w, height_mm=h))


def test_a_parts_files_are_one_item_and_its_drawing_joins_it():
    sources = [
        Source("dwg", "DRW-023179_A.dwg", "dxf", ["Item ID: ITM-072558.A", "DRW-023179.A"]),
        Source("dxf", "ITM-072558_A.dxf", "dxf", []),
        Source("p1", "3aua0000098622_1.dxf", "dxf", []),
        Source("p2", "3aua0000098622_2.dxf", "dxf", ["3AUA0000098622"]),
        Source("other", "100325.dxf", "dxf", ["Next assembly ITM-072558"]),
        Source("s|1", "Part1.step", "step", keys={"part1", "3aua0000098622"}),
    ]
    drawings = {"dwg"}
    assert group_sources(sources, lambda sid: sid in drawings) == [
        ["dwg", "dxf"], ["p1", "p2", "s|1"], ["other"]]


def test_a_part_that_mentions_another_part_number_keeps_its_card():
    sources = [Source("a", "ITM-072558_A.dxf", "dxf", []),
               Source("b", "100325.dxf", "dxf", ["Next assembly ITM-072558"])]
    assert group_sources(sources, lambda sid: False) == [["a"], ["b"]]
    assert group_sources(sources, lambda sid: sid == "b") == [["a", "b"]]


def test_a_clean_flat_beats_a_drawing_sheet():
    flat = score_dxf(_report(400, 120), False, [], None)
    sheet = score_dxf(_report(297, 210), False, ["Title", "Scale 1:2"] * 5, None)
    views = score_dxf(_report(400, 120, dropped=4), False, [], None)
    assert flat.score > views.score > sheet.score
    assert "kehys" in sheet.note and flat.note == "puhdas aihio"
    assert choose({"sheet": sheet, "flat": flat}) == "flat"


def test_the_step_blank_confirms_a_dxf_or_wins_over_a_wrong_one():
    blank = (76.5, 40.0)
    same = score_dxf(_report(40.0, 76.6), False, [], blank)
    other = score_dxf(_report(120, 80), False, [], blank)
    step = score_step(True)
    assert "STEP" in same.note
    assert choose({"s": step, "same": same}) == "same"
    assert choose({"other": other, "s": step}) == "s"


def test_the_users_choice_wins_while_valid():
    scores = {"a": Choice("", 100, ""), "b": Choice("", 50, ""),
              "bad": Choice("", -1e9, "", valid=False)}
    assert choose(scores) == "a"
    assert choose(scores, "b") == "b"
    assert choose(scores, "bad") == "a"
    assert choose({"bad": scores["bad"]}) is None
    assert not score_dxf(_report(10, 10, problems=["x"]), False, [], None).valid


def test_a_guessed_unit_is_confirmed_by_the_step_blank_or_the_pdf_dimensions():
    assert confirm_unit((516.4, 393.3), (393.5, 516.0)) == "vastaa STEP-mallin aihiota"
    # Kate's 3AUA drawing: "(516.4)" and, rotated, ")3.393(" on the flat view.
    texts = ["Weight kg 2.37\n(516.4)\n)3.393(\nSHEET 1,5MM"]
    assert confirm_unit((516.43, 393.28), None, texts) == "PDF-piirustuksen mitta 516.43 × 393.28 mm"
    assert confirm_unit((516.4, 200.0), None, ["516,4"]) == "PDF-piirustuksen mitta 516.4 mm"
    # Inches read as mm (51.64 × 39.33), a mismatching blank, no files: not confirmed.
    assert confirm_unit((51.64, 39.33), (393.3, 516.4), texts) is None
    assert confirm_unit((5.0, 1.5), None, ["5 1,5"]) is None        # too small to tell
    assert confirm_unit((516.4, 393.3)) is None


def test_a_drawing_that_names_no_part_joins_the_item_in_its_folder():
    sources = [Source("dwg", "DRW-023179_A.dwg", "dxf", ["Scale 1:2"], folder="ITM-072558"),
               Source("dxf", "ITM-072558_A.dxf", "dxf", [], folder="ITM-072558"),
               Source("other", "100325.dxf", "dxf", [], folder="100325"),
               Source("lone", "DRW-9.dwg", "dxf", [], folder="")]
    drawings = {"dwg", "lone"}
    assert group_sources(sources, lambda sid: sid in drawings) == [
        ["dwg", "dxf"], ["other"], ["lone"]]
    # Two items in one folder: the drawing can't tell which, it stays apart.
    sources[2] = Source("other", "100325.dxf", "dxf", [], folder="ITM-072558")
    assert group_sources(sources, lambda sid: sid in drawings) == [
        ["dwg"], ["dxf"], ["other"], ["lone"]]
