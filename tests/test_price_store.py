import dataclasses
import os

from core import suppliers


def test_save_then_load_and_reload_after_a_new_save(tmp_path):
    supplier = dataclasses.replace(suppliers.SUPPLIERS[0], path=tmp_path / "prices.json")
    assert suppliers.load(supplier) is None

    suppliers.save(supplier, {"thin": [{"Paksuus (mm)": "2", "S235 | 1000x2000": 900.0}]}, "a.pdf")
    first = suppliers.load(supplier)
    assert first["source_file"] == "a.pdf"
    assert suppliers.load(supplier) is first           # cached until the file changes

    suppliers.save(supplier, {"thin": [{"Paksuus (mm)": "2", "S235 | 1000x2000": 950.0}]}, "b.pdf")
    stat = supplier.path.stat()
    os.utime(supplier.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = suppliers.load(supplier)
    assert second["source_file"] == "b.pdf"
    assert second["data"]["thin"][0]["S235 | 1000x2000"] == 950.0
