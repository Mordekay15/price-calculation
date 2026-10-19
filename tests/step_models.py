"""Small STEP models for the tests, built with Open CASCADE: an L bracket
(2 mm, inner radius 2 mm, one 90° bend), a flat plate with a hole, a pin
(not sheet) and an assembly of them."""

import math
import os
import tempfile


def l_bracket(a=50.0, b=30.0, t=2.0, r=2.0, w=40.0, hole=True):
    """Flanges a (along X) and b (along Z) outside, width w along Y."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace,
                                    BRepBuilderAPI_MakeWire)
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec

    ro = r + t
    c = ro
    s45 = math.sin(math.pi / 4)

    def P(x, z):
        return gp_Pnt(x, 0, z)

    def arc(rad, p, q):
        return BRepBuilderAPI_MakeEdge(
            GC_MakeArcOfCircle(p, P(c - rad * s45, c - rad * s45), q).Value()).Edge()

    def seg(p, q):
        return BRepBuilderAPI_MakeEdge(p, q).Edge()

    wire = BRepBuilderAPI_MakeWire()
    for e in (seg(P(c, 0), P(a, 0)), seg(P(a, 0), P(a, t)), seg(P(a, t), P(c, t)),
              arc(r, P(c, t), P(t, c)), seg(P(t, c), P(t, b)), seg(P(t, b), P(0, b)),
              seg(P(0, b), P(0, c)), arc(ro, P(0, c), P(c, 0))):
        wire.Add(e)
    solid = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(wire.Wire()).Face(),
                                  gp_Vec(0, w, 0)).Shape()
    if hole:
        cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(a - 15, w / 2, -1), gp_Dir(0, 0, 1)),
                                       4, t + 2).Shape()
        solid = BRepAlgoAPI_Cut(solid, cyl).Shape()
    return solid


def plate(x=100.0, y=60.0, t=3.0):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    box = BRepPrimAPI_MakeBox(x, y, t).Shape()
    cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20, 20, -1), gp_Dir(0, 0, 1)), 5, t + 2).Shape()
    return BRepAlgoAPI_Cut(box, cyl).Shape()


def pin():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return BRepPrimAPI_MakeCylinder(4, 30).Shape()


def assembly_step(parts=None) -> bytes:
    """STEP bytes of an assembly "Runko": 2 × Kulma, 1 × Pohja and a
    sub-assembly "Tappiryhmä" (used twice) of 3 × Tappi."""
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def named(label, name):
        TDataStd_Name.Set_s(label, TCollection_ExtendedString(name))
        return label

    def at(x):
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(x, 0, 0))
        return TopLoc_Location(t)

    root = named(tool.NewShape(), "Runko")
    kulma = named(tool.AddShape(l_bracket(), False), "Kulma")
    pohja = named(tool.AddShape(plate(), False), "Pohja")
    tappi = named(tool.AddShape(pin(), False), "Tappi")
    group = named(tool.NewShape(), "Tappiryhmä")
    for i in range(3):
        tool.AddComponent(group, tappi, at(20 * i))
    tool.AddComponent(root, kulma, at(0))
    tool.AddComponent(root, kulma, at(200))
    tool.AddComponent(root, pohja, at(400))
    tool.AddComponent(root, group, at(600))
    tool.AddComponent(root, group, at(800))
    tool.UpdateAssemblies()
    return _write(doc)


def part_step(shape, name) -> bytes:
    """STEP bytes of one named part."""
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    TDataStd_Name.Set_s(tool.AddShape(shape, False), TCollection_ExtendedString(name))
    return _write(doc)


def _write(doc) -> bytes:
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs

    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    writer.Transfer(doc, STEPControl_AsIs)
    fd, path = tempfile.mkstemp(suffix=".step")
    os.close(fd)
    try:
        writer.Write(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def bent_part(segments, t=2.0, r=2.0, w=40.0):
    """A sheet bent along a path: ``segments`` are ``(flange length, bend
    angle in degrees after it)`` (positive turns left, 0 for the last), the
    lengths between the bends' tangent lines on the mid-plane; inner radius
    ``r``, width ``w`` along Y."""
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace,
                                    BRepBuilderAPI_MakeWire)
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt, gp_Vec

    mid = r + t / 2

    def P(p):
        return gp_Pnt(p[0], 0, p[1])

    def side(points_heading, off):
        return [(x - off * math.sin(h), z + off * math.cos(h)) for (x, z), h in points_heading]

    # The mid-line as pieces: ("line", [(p, h), (q, h)]) or ("arc", [3 points with headings]).
    pieces, pos, h = [], (0.0, 0.0), 0.0
    for length, angle in segments:
        end = (pos[0] + length * math.cos(h), pos[1] + length * math.sin(h))
        pieces.append(("line", [(pos, h), (end, h)]))
        pos = end
        if angle:
            a = math.radians(angle)
            s = 1 if a > 0 else -1
            centre = (pos[0] - s * mid * math.sin(h), pos[1] + s * mid * math.cos(h))

            def turn(f, pos=pos, centre=centre, h=h, a=a):
                ang = a * f
                dx, dz = pos[0] - centre[0], pos[1] - centre[1]
                return ((centre[0] + dx * math.cos(ang) - dz * math.sin(ang),
                         centre[1] + dx * math.sin(ang) + dz * math.cos(ang)), h + ang)
            pieces.append(("arc", [turn(0), turn(0.5), turn(1)]))
            pos, h = turn(1)
    edges = []
    for off, order in ((t / 2, 1), (-t / 2, -1)):
        for kind, pts in pieces[::order]:
            q = side(pts[::order], off)
            if kind == "line":
                edges.append(BRepBuilderAPI_MakeEdge(P(q[0]), P(q[1])).Edge())
            else:
                edges.append(BRepBuilderAPI_MakeEdge(
                    GC_MakeArcOfCircle(P(q[0]), P(q[1]), P(q[2])).Value()).Edge())
        last = side([pieces[::order][-1][1][::order][-1]], off)[0]
        nxt = side([pieces[::order][-1][1][::order][-1]], -off)[0]
        edges.append(BRepBuilderAPI_MakeEdge(P(last), P(nxt)).Edge())
    wire = BRepBuilderAPI_MakeWire()
    for e in edges:
        wire.Add(e)
    face = BRepBuilderAPI_MakeFace(wire.Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(0, w, 0)).Shape()
