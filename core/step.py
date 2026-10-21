"""STEP models, pure (no Streamlit): the assembly tree with quantities (the
BOM), and every sheet-metal body unfolded to a flat part that the DXF tab
prices like a dropped DXF.

1. ``read_step`` reads the file with Open CASCADE (the optional ``OCP``
   package, ``cadquery-ocp``): the assembly's parts, each with its name and
   how many the whole assembly uses.
2. ``unfold`` takes one solid. A sheet part is a body whose faces form two
   parallel skins of the same thickness: flat flanges joined by cylindrical
   bends. One skin is laid flat face by face, with the bends at the neutral
   radius ``inner radius + K × thickness``. The result is the flat outline,
   its holes and the bend lines. A body that isn't sheet (a bought or
   machined part) comes back with ``is_sheet`` False and a reason.
3. ``flat_dxf`` writes the flat part as a DXF: outline and holes on ``CUT``,
   bend lines on ``BEND_UP`` / ``BEND_DOWN``, and a note with the thickness,
   so that the DXF reader counts the bends and the material panel reads the
   thickness.
4. ``mesh`` triangulates a body for the 3D preview.
"""

from __future__ import annotations

import io
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import PurePath

DEFAULT_K_FACTOR = 0.44           # neutral axis at r + K·t (air bending, steel)
_TANGENT_COS = 0.995              # faces this parallel at an edge are one skin
_JOIN_TOL_MM = 0.05               # loop ends this close are joined
_ARC_STEP_RAD = math.radians(5)   # curve sampling on the flat
_MAX_THICKNESS_RATIO = 0.25       # thicker than this × the middle box size: not sheet


def available() -> bool:
    """True when Open CASCADE (``OCP``) can be imported."""
    try:
        import OCP.STEPCAFControl  # noqa: F401
    except ImportError:
        return False
    return True


# ── Results ───────────────────────────────────────────────────────────────────

@dataclass
class Bend:
    angle_deg: float
    inner_radius_mm: float
    up: bool                      # bends toward the side the flat is seen from
    line: tuple                   # ((x, y), (x, y)) on the flat


@dataclass
class FlatPart:
    """One body: its size and, for sheet metal, the flat pattern in mm."""

    is_sheet: bool
    volume_mm3: float
    bbox_mm: tuple[float, float, float]           # sorted, largest first
    thickness_mm: float | None = None
    outline: list = field(default_factory=list)   # [(x, y)], closed
    holes: list = field(default_factory=list)
    bends: list[Bend] = field(default_factory=list)
    reason: str = ""                              # why not sheet, or a warning

    @property
    def flat_area_mm2(self) -> float:
        from core.geometry import net_area
        return net_area(self.outline, self.holes) if self.outline else 0.0

    @property
    def blank_mm(self) -> tuple[float, float] | None:
        """The flat's ``(width, height)`` in mm, None when it has none."""
        from core.geometry import bbox_wh
        return bbox_wh(self.outline) if self.outline else None


@dataclass
class StepPart:
    """A unique body of the model and how many of it the assembly uses."""

    key: str
    name: str
    qty: int
    shape: object = field(repr=False, default=None)   # TopoDS_Shape


@dataclass
class BomRow:
    level: int
    name: str
    qty: int                      # per parent
    total: int                    # in the whole model
    part_key: str | None = None   # None for a (sub)assembly


@dataclass
class StepModel:
    name: str
    parts: list[StepPart] = field(default_factory=list)
    bom: list[BomRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ── Reading ───────────────────────────────────────────────────────────────────

def read_step(data: bytes, name: str = "model.step") -> StepModel:
    """The model's parts and BOM. Never raises: a bad file comes back with
    problems."""
    model = StepModel(name=name)
    if not available():
        model.problems.append("STEP-tiedostojen lukuun tarvitaan cadquery-ocp-paketti.")
        return model
    try:
        _read(data, model)
    except Exception as exc:  # noqa: BLE001 - a bad file must not crash the page
        model.problems.append(f"STEP-tiedostoa ei voitu lukea ({exc}).")
    if not model.parts and not model.problems:
        model.problems.append("STEP-tiedostosta ei löytynyt kappaleita.")
    return model


def _read(data: bytes, model: StepModel) -> None:
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    fd, path = tempfile.mkstemp(suffix=".step")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        if reader.ReadFile(path) != IFSelect_RetDone:
            model.problems.append("Tiedosto ei ole luettava STEP-tiedosto.")
            return
        doc = TDocStd_Document(TCollection_ExtendedString("step"))
        if not reader.Transfer(doc):
            model.problems.append("STEP-tiedoston muunnos epäonnistui.")
            return
    finally:
        os.unlink(path)

    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    roots = _label_sequence()
    tool.GetFreeShapes(roots)
    parts: dict[str, StepPart] = {}
    stem = PurePath(model.name).stem
    for i in range(1, roots.Length() + 1):
        _walk(tool, roots.Value(i), 1, 1, 0, parts, model.bom, stem)
    model.parts = list(parts.values())


# What CAD programs name a body inside a part (Creo: "SOLID" and an empty
# "COMPOUND" under each part): not a part number, so the part's name is used.
_GENERIC_NAME = re.compile(r"(solid|compound|shell|body|part\d*|=>\[.*\])?", re.IGNORECASE)


def _walk(tool, label, qty, mult, level, parts, bom, parent) -> None:
    """``parent`` is the name of the assembly the label is in (the file's
    name at the top)."""
    from OCP.XCAFDoc import XCAFDoc_ShapeTool

    own = _label_name(label)
    name = parent if _GENERIC_NAME.fullmatch(own) else own
    if XCAFDoc_ShapeTool.IsAssembly_s(label):
        bom.append(BomRow(level, name, qty, qty * mult))
        comps = _label_sequence()
        XCAFDoc_ShapeTool.GetComponents_s(label, comps, False)
        counts: dict[str, list] = {}
        for j in range(1, comps.Length() + 1):
            ref = _referred(comps.Value(j))
            counts.setdefault(_entry(ref), [ref, 0])[1] += 1
        for ref, n in counts.values():
            _walk(tool, ref, n, qty * mult, level + 1, parts, bom, name)
        return
    shape = XCAFDoc_ShapeTool.GetShape_s(label)
    solids = _solids(shape)
    if not solids:
        return
    groups = _group_same(solids)
    for k, (solid, n) in enumerate(groups):
        part_name = name if len(groups) == 1 else f"{name}_{k + 1}"
        key = f"{_entry(label)}/{k}"
        total = n * qty * mult
        if key in parts:
            parts[key].qty += total
        else:
            parts[key] = StepPart(key, part_name, total, solid)
        bom.append(BomRow(level, part_name, n * qty, total, key))


def _label_sequence():
    """An empty label sequence (its class moved between OCP versions)."""
    try:
        from OCP.TDF import TDF_LabelSequence
    except ImportError:
        from OCP.OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
    return TDF_LabelSequence()


def _label_name(label) -> str:
    from OCP.TDataStd import TDataStd_Name

    attr = TDataStd_Name()
    if not label.FindAttribute(TDataStd_Name.GetID_s(), attr):
        return ""
    name = attr.Get().ToExtString().strip()
    try:   # UTF-8 names read back byte by byte ("TappiryhmÃ¤")
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _referred(label):
    from OCP.TDF import TDF_Label
    from OCP.XCAFDoc import XCAFDoc_ShapeTool

    if XCAFDoc_ShapeTool.IsReference_s(label):
        ref = TDF_Label()
        XCAFDoc_ShapeTool.GetReferredShape_s(label, ref)
        return ref
    return label


def _entry(label) -> str:
    from OCP.TCollection import TCollection_AsciiString
    from OCP.TDF import TDF_Tool

    s = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, s)
    return s.ToCString()


def _solids(shape) -> list:
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    out = []
    exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while exp.More():
        out.append(exp.Current())
        exp.Next()
    return out


def _signature(solid) -> tuple:
    """Same volume, area and box size: the same part (copies placed apart)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    vp, sp = GProp_GProps(), GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, vp)
    BRepGProp.SurfaceProperties_s(solid, sp)
    return (round(vp.Mass(), 0), round(sp.Mass(), 0),
            tuple(round(d, 1) for d in _bbox(solid)))


_CRUMB_SHARE = 0.01   # a loose body this small next to the part's main body is CAD waste


def _group_same(solids: list) -> list[tuple[object, int]]:
    """The bodies, copies counted together. Crumbs are left out: tiny loose
    bodies in a part (under 1 % of its biggest body's volume), such as the
    pieces a form tool leaves behind, which no drawing shows."""
    groups: dict[tuple, list] = {}
    for s in solids:
        groups.setdefault(_signature(s), [s, 0])[1] += 1
    biggest = max(abs(sig[0]) for sig in groups)
    return [(s, n) for sig, (s, n) in groups.items()
            if abs(sig[0]) >= _CRUMB_SHARE * biggest]


def _bbox(shape) -> tuple[float, float, float]:
    """The body's box size along the model's axes, largest first. The quick
    box: a curved face can make it up to a few mm too big, which is fine for
    what it is used for (telling copies apart, a thickness check), and the
    exact one (``AddOptimal``) took most of a STEP file's reading time."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box, False)
    lo, hi, gap = box.CornerMin(), box.CornerMax(), 2 * box.GetGap()
    return tuple(sorted((hi.X() - lo.X() - gap, hi.Y() - lo.Y() - gap, hi.Z() - lo.Z() - gap),
                        reverse=True))


# ── Unfolding ─────────────────────────────────────────────────────────────────

class _Face:
    """One face of the body: its surface type and outward geometry."""

    def __init__(self, face):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.BRepGProp import BRepGProp
        from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
        from OCP.GProp import GProp_GProps

        self.face = face
        surf = BRepAdaptor_Surface(face)
        kind = surf.GetType()
        self.kind = ("plane" if kind == GeomAbs_Plane
                     else "cylinder" if kind == GeomAbs_Cylinder else "other")
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        self.area = props.Mass()
        c = props.CentreOfMass()
        self.centre = (c.X(), c.Y(), c.Z())
        if self.kind == "plane":
            pln = surf.Plane()
            p = pln.Location()
            self.origin = (p.X(), p.Y(), p.Z())
            self.normal = self.normal_at_uv((surf.FirstUParameter() + surf.LastUParameter()) / 2,
                                            (surf.FirstVParameter() + surf.LastVParameter()) / 2)
        elif self.kind == "cylinder":
            cyl = surf.Cylinder()
            ax = cyl.Axis()
            p, d = ax.Location(), ax.Direction()
            self.axis_point = (p.X(), p.Y(), p.Z())
            self.axis = (d.X(), d.Y(), d.Z())
            self.radius = cyl.Radius()
            self.u_span = surf.LastUParameter() - surf.FirstUParameter()
            u, v = surf.FirstUParameter() + self.u_span / 2, surf.FirstVParameter()
            n = self.normal_at_uv(u, v)
            pt = surf.Value(u, v)
            radial = _sub((pt.X(), pt.Y(), pt.Z()), self._on_axis((pt.X(), pt.Y(), pt.Z())))
            # Outward normal toward the axis: the inside of the bend (concave).
            self.concave = _dot(n, radial) < 0

    def normal_at_uv(self, u, v):
        from OCP.BRepGProp import BRepGProp_Face
        from OCP.gp import gp_Pnt, gp_Vec

        p, n = gp_Pnt(), gp_Vec()
        BRepGProp_Face(self.face).Normal(u, v, p, n)
        return _unit((n.X(), n.Y(), n.Z()))

    def normal_on_edge(self, edge):
        from OCP.BRepAdaptor import BRepAdaptor_Curve2d

        if self.kind == "plane":
            return self.normal   # the same all over (and the slow part on a face of many holes)
        c2d = BRepAdaptor_Curve2d(edge, self.face)
        uv = c2d.Value((c2d.FirstParameter() + c2d.LastParameter()) / 2)
        return self.normal_at_uv(uv.X(), uv.Y())

    def _on_axis(self, p):
        t = _dot(_sub(p, self.axis_point), self.axis)
        return _add(self.axis_point, _mul(self.axis, t))


def unfold(solid, k_factor: float = DEFAULT_K_FACTOR) -> FlatPart:
    """The body's flat pattern, or why it has none (see the module doc)."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(solid, props)
    flat = FlatPart(False, abs(props.Mass()), _bbox(solid))
    try:
        _unfold(solid, flat, k_factor)
    except _NotSheet as exc:
        flat.is_sheet = False
        flat.outline, flat.holes, flat.bends = [], [], []
        flat.reason = str(exc)
    return flat


class _NotSheet(Exception):
    pass


def _unfold(solid, flat: FlatPart, k: float) -> None:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer

    from OCP.TopoDS import TopoDS

    faces = []
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        faces.append(_Face(TopoDS.Face(exp.Current())))
        exp.Next()
    planes = [f for f in faces if f.kind == "plane"]
    if not planes:
        raise _NotSheet("ei tasomaisia pintoja")
    base = max(planes, key=lambda f: f.area)

    # The nearest opposite face is usually the flange's other skin, but in a
    # joggle another flange's face can be nearer: the first that unfolds to
    # the body's volume is the thickness.
    ts = _thicknesses(base, planes)
    if not ts:
        raise _NotSheet("ei vastakkaista samansuuntaista pintaa (ei levyosa)")
    first = None
    for t in ts:
        if t > _MAX_THICKNESS_RATIO * flat.bbox_mm[1]:
            first = first or _NotSheet(
                f"paksuus {t:g} mm on suuri kappaleen kokoon nähden (ei levyosa)")
            break
        try:
            return _lay_flat(faces, base, t, flat, k)
        except _NotSheet as exc:
            first = first or exc
    raise first


def _lay_flat(faces: list, base, t: float, flat: FlatPart, k: float) -> None:
    """Unfold the skin from ``base`` at thickness ``t`` into ``flat``."""
    flat.thickness_mm = t

    # Edge -> the faces it bounds (a shape's hash ignores its orientation).
    edge_faces: dict[int, set[int]] = {}
    for i, f in enumerate(faces):
        for edge in _edges(f.face):
            edge_faces.setdefault(hash(edge), set()).add(i)

    def neighbours(i):
        for edge in _edges(faces[i].face):
            for j in edge_faces.get(hash(edge), ()):
                if j != i:
                    yield edge, j

    # Lay the skin flat face by face, from the largest flange.
    u = _unit(_any_perpendicular(base.normal))
    v = _cross(base.normal, u)
    o = base.origin
    start = faces.index(base)
    maps: dict[int, object] = {start: lambda p: (_dot(_sub(p, o), u), _dot(_sub(p, o), v))}
    queue = [start]
    bend_faces = []
    while queue:
        i = queue.pop(0)
        for edge, j in neighbours(i):
            if j in maps or faces[j].kind not in ("plane", "cylinder"):
                continue
            if _dot(faces[i].normal_on_edge(edge), faces[j].normal_on_edge(edge)) < _TANGENT_COS:
                continue
            line = _line_ends(edge)
            if line is None:
                raise _NotSheet("taivutus, jota ei osata avata (ei suora taiteviiva)")
            a3, b3 = line
            a2, b2 = maps[i](a3), maps[i](b3)
            if faces[j].kind == "plane":
                maps[j] = _plane_map(faces[j], a3, b3, a2, b2)
            else:
                maps[j], info = _cylinder_map(faces[j], a3, b3, a2, b2, t, k)
                bend_faces.append((j, info))
            queue.append(j)

    skin = set(maps)
    # The skin's free edges, laid flat, chain into the outline and holes.
    pieces = []
    for i in skin:
        for edge in _edges(faces[i].face):
            if any(j in skin and j != i for j in edge_faces.get(hash(edge), ())):
                continue
            pts = [maps[i](p) for p in _sample(edge)]
            if len(pts) >= 2:
                pieces.append(pts)
    loops = _chain(pieces)
    if not loops:
        raise _NotSheet("avattu muoto ei sulkeudu")
    from core.geometry import area
    loops.sort(key=lambda lp: abs(area(lp)), reverse=True)
    flat.outline, flat.holes = loops[0], loops[1:]
    flat.bends = _bends(faces, bend_faces, t)
    flat.is_sheet = True

    # The flat area times the thickness should give the volume (exactly at
    # K = 0.5); a large gap means the body isn't one sheet.
    expected = flat.volume_mm3 / t
    got = flat.flat_area_mm2
    if expected <= 0 or abs(got - expected) / expected > 0.08:
        raise _NotSheet(f"avattu pinta-ala {got:.0f} mm² ei vastaa tilavuutta "
                        f"({expected:.0f} mm²): ei tasapaksu levyosa")


def _thicknesses(base: _Face, planes: list[_Face]) -> list[float]:
    """The distances to the faces facing the other way, parallel to the
    largest flange, nearest first."""
    ds = {round(_dot(_sub(base.origin, f.origin), base.normal), 6) for f in planes
          if _dot(f.normal, base.normal) <= -_TANGENT_COS}
    return sorted(d for d in ds if d > 1e-3)


def _plane_map(face: _Face, a3, b3, a2, b2):
    e3 = _unit(_sub(b3, a3))
    w3 = _cross(face.normal, e3)
    if _dot(_sub(face.centre, a3), w3) < 0:
        w3 = _mul(w3, -1)
    e2 = _unit2(_sub2(b2, a2))
    w2 = _side2(e2, e3, w3, face.normal)

    def f(p):
        d = _sub(p, a3)
        x, y = _dot(d, e3), _dot(d, w3)
        return (a2[0] + x * e2[0] + y * w2[0], a2[1] + x * e2[1] + y * w2[1])
    return f


def _cylinder_map(face: _Face, a3, b3, a2, b2, t: float, k: float):
    axis = face.axis
    ra = _unit(_sub(a3, face._on_axis(a3)))

    def angle(p):
        r = _sub(p, face._on_axis(p))
        return math.atan2(_dot(axis, _cross(ra, r)), _dot(ra, r))

    sign = 1.0 if angle(face.centre) >= 0 else -1.0
    e3 = _unit(_sub(b3, a3))
    w3 = _mul(_cross(axis, ra), sign)                 # into the face, at the edge
    normal = _mul(ra, -1.0 if face.concave else 1.0)
    e2 = _unit2(_sub2(b2, a2))
    w2 = _side2(e2, e3, w3, normal)
    inner = face.radius if face.concave else face.radius - t
    neutral = inner + k * t

    def f(p):
        z = _dot(_sub(p, a3), e3)
        s = max(sign * angle(p), 0.0) * neutral
        return (a2[0] + z * e2[0] + s * w2[0], a2[1] + z * e2[1] + s * w2[1])
    info = {"inner": inner, "neutral": neutral, "a2": a2, "e2": e2, "w2": w2,
            "e3": e3, "a3": a3}
    return f, info


def _side2(e2, e3, w3, normal):
    """The flat direction for ``w3``: the flat keeps the 3D orientation seen
    from the outward normal."""
    hand = 1.0 if _dot(_cross(e3, w3), normal) >= 0 else -1.0
    return (-e2[1] * hand, e2[0] * hand)


def _bends(faces, bend_faces, t) -> list[Bend]:
    """One bend per axis (a bend split into several faces counts once): its
    angle, inner radius and centre line on the flat."""
    out: list[Bend] = []
    seen: dict[tuple, Bend] = {}
    for j, info in bend_faces:
        f = faces[j]
        zs = [_dot(_sub(p, info["a3"]), info["e3"]) for e in _edges(f.face) for p in _sample(e)]
        s_mid = f.u_span / 2 * info["neutral"]
        a2, e2, w2 = info["a2"], info["e2"], info["w2"]
        ends = tuple((a2[0] + z * e2[0] + s_mid * w2[0], a2[1] + z * e2[1] + s_mid * w2[1])
                     for z in (min(zs), max(zs)))
        key = (tuple(round(c, 1) for c in f.axis_point), round(f.radius, 2))
        key = (*key, tuple(round(abs(c), 3) for c in f.axis))
        if key in seen:
            continue
        bend = Bend(math.degrees(f.u_span), round(info["inner"], 3), up=f.concave, line=ends)
        seen[key] = bend
        out.append(bend)
    return out


# ── Edges ─────────────────────────────────────────────────────────────────────

def _edges(face) -> list:
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out = []
    exp = TopExp_Explorer(face, TopAbs_EDGE)
    while exp.More():
        out.append(TopoDS.Edge(exp.Current()))
        exp.Next()
    return out


def _line_ends(edge):
    """A straight edge's two ends, None when the edge is curved."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Line

    c = BRepAdaptor_Curve(edge)
    if c.GetType() != GeomAbs_Line:
        return None
    p, q = c.Value(c.FirstParameter()), c.Value(c.LastParameter())
    return (p.X(), p.Y(), p.Z()), (q.X(), q.Y(), q.Z())


def _sample(edge) -> list[tuple]:
    """Points along an edge, in its own direction as used by the face."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line

    c = BRepAdaptor_Curve(edge)
    a, b = c.FirstParameter(), c.LastParameter()
    kind = c.GetType()
    if kind == GeomAbs_Line:
        n = 1
    elif kind == GeomAbs_Circle:
        n = max(2, math.ceil(abs(b - a) / _ARC_STEP_RAD))
    else:
        n = 48
    pts = []
    for i in range(n + 1):
        p = c.Value(a + (b - a) * i / n)
        pts.append((p.X(), p.Y(), p.Z()))
    return pts


def _chain(pieces: list[list[tuple]]) -> list[list[tuple]]:
    """Join polylines end to end into closed loops (dropping the repeated
    end point). Pieces that never close are left out."""
    pieces = [list(p) for p in pieces]
    loops = []
    tol = _JOIN_TOL_MM

    def near(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    while pieces:
        loop = pieces.pop(0)
        grown = True
        while grown and not (len(loop) > 2 and near(loop[0], loop[-1])):
            grown = False
            for i, p in enumerate(pieces):
                if near(loop[-1], p[0]):
                    loop += p[1:]
                elif near(loop[-1], p[-1]):
                    loop += p[-2::-1]
                elif near(loop[0], p[-1]):
                    loop = p[:-1] + loop
                elif near(loop[0], p[0]):
                    loop = p[:0:-1] + loop
                else:
                    continue
                pieces.pop(i)
                grown = True
                break
        if len(loop) > 3 and near(loop[0], loop[-1]):
            loops.append(_dedupe(loop[:-1]))
    return loops


def _dedupe(points):
    out = []
    for p in points:
        if not out or abs(p[0] - out[-1][0]) > 1e-6 or abs(p[1] - out[-1][1]) > 1e-6:
            out.append((round(p[0], 4), round(p[1], 4)))
    return out


# ── Bends on another drawing of the flat ──────────────────────────────────────

_FIT_TOL = 0.02          # mean miss allowed, as a share of the part's size


def bends_on(flat: FlatPart, rings: list[list[tuple[float, float]]]
             ) -> list[tuple[tuple, bool]] | None:
    """The flat's bend lines placed on a DXF of the same part (``rings``: its
    outline and holes, in mm), as ``[(line, up)]``, or None when the shapes
    don't match. The DXF may be the flat turned in steps of 90°, mirrored (seen
    from the other side: up and down swap) and moved."""
    if not flat.bends or not flat.outline or not rings:
        return None
    tx = [p[0] for ring in rings for p in ring]
    ty = [p[1] for ring in rings for p in ring]
    t_min, t_size = (min(tx), min(ty)), (max(tx) - min(tx), max(ty) - min(ty))
    size = max(t_size)
    # Points along the DXF's edges, so a miss measures the distance to them.
    target = _thin([p for ring in rings for p in _along(ring, size / 100)], 800)
    source = _thin([p for ring in (flat.outline, *flat.holes) for p in ring])
    best = None
    for turn in range(4):
        for mirror in (False, True):
            def move(p, turn=turn, mirror=mirror):
                x, y = (-p[0], p[1]) if mirror else p
                for _ in range(turn):
                    x, y = -y, x
                return x, y
            moved = [move(p) for p in source]
            mx, my = min(x for x, _ in moved), min(y for _, y in moved)
            w = max(x for x, _ in moved) - mx
            h = max(y for _, y in moved) - my
            if abs(w - t_size[0]) > _FIT_TOL * size or abs(h - t_size[1]) > _FIT_TOL * size:
                continue
            dx, dy = t_min[0] - mx, t_min[1] - my
            miss = sum(min(math.hypot(x + dx - u, y + dy - v) for u, v in target)
                       for x, y in moved) / len(moved)
            if best is None or miss < best[0]:
                best = (miss, move, mirror, dx, dy)
    if best is None or best[0] > _FIT_TOL * size:
        return None
    _, move, mirror, dx, dy = best

    def place(p):
        x, y = move(p)
        return x + dx, y + dy

    return [((place(b.line[0]), place(b.line[1])), b.up != mirror) for b in flat.bends]


def _along(ring: list, spacing: float) -> list:
    out = []
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / spacing))
        out += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n)]
    return out


def _thin(points: list, n: int = 240) -> list:
    step = max(1, len(points) // n)
    return points[::step]


# ── Output ────────────────────────────────────────────────────────────────────

BEND_UP, BEND_DOWN = "BEND_UP", "BEND_DOWN"


def flat_dxf(flat: FlatPart, name: str) -> bytes:
    """The flat part as a DXF in mm (see the module doc)."""
    import ezdxf

    doc = ezdxf.new("R2010", units=4)
    doc.header["$INSUNITS"] = 4
    for layer, colour in (("CUT", 7), (BEND_UP, 3), (BEND_DOWN, 1), ("NOTES", 8)):
        doc.layers.add(layer, color=colour)
    msp = doc.modelspace()
    for loop in (flat.outline, *flat.holes):
        msp.add_lwpolyline(loop, close=True, dxfattribs={"layer": "CUT"})
    for b in flat.bends:
        msp.add_line(b.line[0], b.line[1], dxfattribs={"layer": BEND_UP if b.up else BEND_DOWN})
    note = f"{name} | t = {flat.thickness_mm:g} mm | {len(flat.bends)} taivutusta (STEP)"
    msp.add_text(note, height=3, dxfattribs={"layer": "NOTES"})
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")


def mesh(shape, deflection: float = 0.2) -> tuple[list[tuple], list[tuple]]:
    """``(vertices, triangles)`` of a body for the 3D preview."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)
    verts, tris = [], []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            base = len(verts)
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(trsf)
                verts.append((p.X(), p.Y(), p.Z()))
            rev = face.Orientation() == TopAbs_REVERSED
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                tris.append((base + a - 1, base + (c if rev else b) - 1,
                             base + (b if rev else c) - 1))
        exp.Next()
    return verts, tris


def safe_name(name: str) -> str:
    """A part name usable as a file name."""
    return re.sub(r"[^\w.\-+ ]+", "_", name).strip(" _.") or "osa"


# ── Small vector helpers ──────────────────────────────────────────────────────

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a):
    n = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def _any_perpendicular(n):
    trial = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    return _cross(n, trial)


def _sub2(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _unit2(a):
    n = math.hypot(*a) or 1.0
    return (a[0] / n, a[1] / n)
