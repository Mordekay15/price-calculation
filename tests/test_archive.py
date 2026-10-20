"""Dropped ZIPs: their files folder by folder, nested ZIPs, names."""

import io
import zipfile

import pytest

from core.archive import ArchiveError, read_path, unpack


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_a_zip_of_item_folders_comes_out_folder_by_folder():
    inner = _zip({"a.dxf": b"A", "sub/b.pdf": b"B"})
    data = _zip({"order/ITM-2/x.dxf": b"2", "order/ITM-1/y.stp": b"1",
                 "order/ITM-1/inner.zip": inner, "__MACOSX/order/._y.stp": b"",
                 "order/.DS_Store": b"", "readme.txt": b"r"})
    got = [(e.folder, e.name, e.data) for e in unpack(data)]
    assert got == [("", "readme.txt", b"r"),
                   ("order/ITM-1", "y.stp", b"1"),
                   ("order/ITM-1/inner", "a.dxf", b"A"),
                   ("order/ITM-1/inner/sub", "b.pdf", b"B"),
                   ("order/ITM-2", "x.dxf", b"2")]


def test_finnish_names_from_windows_zips_are_read_right():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("Kiinnike/levy.dxf")
        zf.writestr(info, b"x")
    data = buf.getvalue()
    # Rewrite the name as Windows' own ZIP does: CP850 bytes, no UTF-8 flag.
    data = data.replace(b"Kiinnike", "Kääntö__".encode("cp850"))
    assert unpack(data)[0].folder == "Kääntö__"


def test_a_broken_zip_is_an_error():
    with pytest.raises(ArchiveError):
        unpack(b"not a zip")


def test_unused_files_are_named_not_read():
    skipped = []
    data = _zip({"a/x.dxf": b"1", "a/x.prt.1": b"creo", "a/b.xlsx": b"x"})
    got = unpack(data, lambda name: name.endswith(".dxf"), skipped)
    assert [(e.folder, e.name) for e in got] == [("a", "x.dxf")]
    assert sorted(skipped) == ["b.xlsx", "x.prt.1"]


def test_a_zip_or_a_folder_is_read_from_disk(tmp_path):
    zipped = tmp_path / "RFQ 580.zip"
    zipped.write_bytes(_zip({"RFQ/ITM-1/y.stp": b"1", "RFQ/ITM-1/y.prt.1": b"c"}))
    skipped = []
    got = read_path(f'"{zipped}"', lambda n: not n.endswith(".1"), skipped)  # "Copy as path"
    assert [(e.path, e.data) for e in got] == [("RFQ/ITM-1/y.stp", b"1")]
    assert skipped == ["y.prt.1"]
    folder = tmp_path / "RFQ"
    (folder / "ITM-2").mkdir(parents=True)
    (folder / "ITM-2" / "z.dxf").write_bytes(b"2")
    (folder / "inner.zip").write_bytes(_zip({"a.pdf": b"3"}))
    got = read_path(str(folder))
    assert [(e.path, e.data) for e in got] == [("RFQ/inner/a.pdf", b"3"),
                                               ("RFQ/ITM-2/z.dxf", b"2")]
    with pytest.raises(ArchiveError):
        read_path(str(tmp_path / "missing.zip"))
