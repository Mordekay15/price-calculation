"""Shared helpers: build small DXF drawings in memory with ezdxf."""

import io

import ezdxf
import pytest


def rect(msp, x, y, w, h, **attribs):
    msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                       close=True, dxfattribs=attribs)


def dxf_bytes(doc) -> bytes:
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


@pytest.fixture
def drawing():
    """``drawing(units=4)`` → ``(doc, msp)``; ``units=0`` leaves the unit out."""
    def make(units: int = 4):
        doc = ezdxf.new(setup=True)
        doc.header["$INSUNITS"] = units
        return doc, doc.modelspace()
    return make
