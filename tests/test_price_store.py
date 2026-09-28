import dataclasses
import os

from core import price_store


def test_save_then_load_and_reload_after_a_new_save(tmp_path):
    supplier = dataclasses.replace(price_store.SUPPLIERS[0], path=tmp_path / "prices.json")
    assert price_store.load(supplier) is None

    price_store.save(supplier, {"thin": [{"Paksuus (mm)": "2", "S235 | 1000x2000": 900.0}]}, "a.pdf")
    first = price_store.load(supplier)
    assert first["source_file"] == "a.pdf"
    assert price_store.load(supplier) is first           # cached until the file changes

    price_store.save(supplier, {"thin": [{"Paksuus (mm)": "2", "S235 | 1000x2000": 950.0}]}, "b.pdf")
    stat = supplier.path.stat()
    os.utime(supplier.path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = price_store.load(supplier)
    assert second["source_file"] == "b.pdf"
    assert second["data"]["thin"][0]["S235 | 1000x2000"] == 950.0
