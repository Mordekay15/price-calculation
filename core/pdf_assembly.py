"""Is a drawing PDF an assembly drawing? Pure, apart from ``read_pages``
(pdfplumber).

Standards (ISO 7200 title blocks, ISO 7573 parts lists, ISO 6433 item
references, ISO 2553 welds) set what an assembly drawing has: a parts list
above the title block, numbered balloons that point into it, a title block
whose number is not one of its rows, and often welds. No one sign is sure,
so each adds to one score:

1. **Read each page** (``read_pages``): its words with their positions, its
   circles. A page of paths with no words is a scan: noted, not read (no OCR).
2. **Title block**: the drawing's own number is the file's (``own``); the
   part numbers in the bottom-right corner are its own too, not items.
3. **Parts list**: a line whose words name at least two of the columns item,
   quantity, part number, name (in English, Finnish, German, Swedish).
4. **Its rows**, above the header (ISO, numbered upwards) or below it
   (SolidWorks): item, qty, part number, name; a standard or vendor code
   (ISO 4762, PEM S-M5-2) is a bought part.
5. **Balloons**: a circle with a 1 to 3 digit number at its centre; the ones
   that are parts-list items confirm the list.
6. **Other cues**: the title's words (ASSEMBLY DRAWING, KOKOONPANO,
   ZUSAMMENBAU; not ABB's "(ASSEM)", which names where a part is used),
   part numbers called out on their own lines,
   welds, "material: see parts list"; against: a detail drawing, a flat
   pattern or bend notes, one material.
7. **Score**: 4 or more is an assembly, 1 to 3 maybe, 0 or less a part.

``PdfAssembly.cues`` keeps every cue that counted, so the app can say why."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.linking import base_key, callouts

ASSEMBLY, MAYBE, PART = "assembly", "maybe", "part"
ASSEMBLY_AT = 4          # a score this high is an assembly
MAYBE_AT = 1             # this high, maybe one: the user decides


# ── 1 · Pages ─────────────────────────────────────────────────────────────────

@dataclass
class PdfPage:
    """One page as the recogniser reads it, in PDF points, y down."""

    number: int
    width: float
    height: float
    words: list[dict]                            # pdfplumber's extract_words
    text: str = ""                               # pdfplumber's extract_text
    circles: list[tuple[float, float, float]] = field(default_factory=list)  # x, y, r
    paths: int = 0                               # lines and curves drawn

    @property
    def scanned(self) -> bool:
        """Drawn, but no text to read: a scan or text turned into paths."""
        return len(self.words) < 5 and self.paths > 50


def read_pages(data: bytes, max_pages: int = 3) -> list[PdfPage]:
    """A PDF's first pages, each opened once; [] when unreadable."""
    import io

    import pdfplumber

    out = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]):
                curves = page.curves
                out.append(PdfPage(i + 1, float(page.width), float(page.height),
                                   page.extract_words(), page.extract_text() or "",
                                   _circles(curves), len(curves) + len(page.lines)))
    except Exception:  # noqa: BLE001 - any unreadable PDF just gives no pages
        return []
    return out


def _circles(curves: list[dict]) -> list[tuple[float, float, float]]:
    """The closed, round curves 6 to 40 pt across (balloon sized): ``(x, y,
    r)``."""
    out = []
    for c in curves:
        w, h = c["x1"] - c["x0"], c["bottom"] - c["top"]
        pts = c.get("pts") or []
        closed = len(pts) > 2 and abs(pts[0][0] - pts[-1][0]) + abs(pts[0][1] - pts[-1][1]) < 1
        if closed and 6 <= w <= 40 and 0.85 <= w / max(h, 0.01) <= 1.18:
            out.append(((c["x0"] + c["x1"]) / 2, (c["top"] + c["bottom"]) / 2, (w + h) / 4))
    return out


# ── The result ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BomRow:
    """One parts-list row. ``number`` is a ``base_key``, "" when none."""

    item: int | None
    qty: int
    number: str
    name: str
    bought: bool
    code: str = ""          # the part-number cell as written (ISO 4762 M5x12)


@dataclass(frozen=True)
class Cue:
    """One sign for or against an assembly, its ``label`` in Finnish."""

    key: str
    label: str
    weight: int
    page: int | None = None


@dataclass
class PdfAssembly:
    own: str
    rows: list[BomRow] = field(default_factory=list)
    balloons: list[int] = field(default_factory=list)
    called_out: list[str] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    scanned: list[int] = field(default_factory=list)     # page numbers

    @property
    def score(self) -> int:
        return sum(c.weight for c in self.cues)

    @property
    def verdict(self) -> str:
        return ASSEMBLY if self.score >= ASSEMBLY_AT else MAYBE if self.score >= MAYBE_AT else PART

    def why(self) -> str:
        """The cues, e.g. "osaluettelo +3 · 5 numeroitua palloa +2"."""
        return " · ".join(f"{c.label} {c.weight:+d}" for c in self.cues if c.weight)


# ── 2 · Title block ───────────────────────────────────────────────────────────

_NUMBER = re.compile(r"(?<![\w.\-])(?=[a-z0-9\-]*\d{5})[a-z0-9]+(?:-[a-z0-9]+)?",
                     re.IGNORECASE)


def title_numbers(page: PdfPage) -> set[str]:
    """The part numbers in the bottom-right corner, where the title block is:
    the drawing's own (its number, its document number), never items."""
    return {base_key(m) for w in page.words
            if w["x0"] > page.width * 0.5 and w["top"] > page.height * 0.7
            for m in _NUMBER.findall(w["text"])}


# ── 3, 4 · Parts list ─────────────────────────────────────────────────────────

ITEM, QTY, NUMBER, NAME = "item", "qty", "number", "name"
_PHRASES = {        # two words that name one column
    ("part", "no"): NUMBER, ("part", "no."): NUMBER, ("part", "number"): NUMBER,
    ("drawing", "no"): NUMBER, ("drawing", "no."): NUMBER, ("dwg", "no"): NUMBER,
    ("dwg", "no."): NUMBER, ("doc", "no"): NUMBER, ("doc", "no."): NUMBER,
    ("art", "no"): NUMBER, ("art.", "no."): NUMBER, ("item", "no"): ITEM,
    ("item", "no."): ITEM, ("find", "no"): ITEM, ("find", "no."): ITEM,
    ("osa", "nro"): ITEM, ("pos", "nr"): ITEM, ("pos.", "nr."): ITEM,
}
_WORDS = {
    ITEM: {"item", "pos", "pos.", "position", "pos.-nr.", "nro", "find"},
    QTY: {"qty", "qty.", "quantity", "kpl", "määrä", "maara", "menge", "stk", "stk.",
          "antal", "pcs", "st", "lkm"},
    NUMBER: {"partnumber", "osanumero", "nimike", "nimikenumero", "piirustusnumero",
             "sachnummer", "teilenummer", "teile-nr.", "zeichnungsnummer", "artikelnr",
             "artikelnummer", "ritningsnr", "ritningsnummer", "koodi", "code", "tunnus"},
    NAME: {"description", "nimitys", "nimi", "benennung", "benämning", "designation", "name"},
}
_ROLE_OF = {w: role for role, ws in _WORDS.items() for w in ws}


def _lines(words: list[dict]) -> list[list[dict]]:
    """The page's words in lines (one baseline), each left to right, the
    lines top to bottom."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        h = w["bottom"] - w["top"]
        if out and abs(out[-1][0]["top"] - w["top"]) < h * 0.5:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in out]


def _columns(line: list[dict]) -> list[tuple[str | None, float, float]]:
    """A line's words as columns ``(role, x0, x1)``: two words naming one
    column (Part No) are one."""
    out, i = [], 0
    while i < len(line):
        a = line[i]["text"].lower().strip(":")
        b = line[i + 1]["text"].lower().strip(":") if i + 1 < len(line) else ""
        near = i + 1 < len(line) and line[i + 1]["x0"] - line[i]["x1"] < 3 * (
            line[i]["bottom"] - line[i]["top"])
        if near and (a, b) in _PHRASES:
            out.append((_PHRASES[a, b], line[i]["x0"], line[i + 1]["x1"]))
            i += 2
            continue
        out.append((_ROLE_OF.get(a), line[i]["x0"], line[i]["x1"]))
        i += 1
    return out


def _is_header(cols) -> bool:
    roles = {r for r, _, _ in cols if r}
    return len(roles) >= 2 and bool(roles & {ITEM, QTY})


_STANDARD = re.compile(r"^(?:ISO|DIN|EN|SFS|ASME|ANSI|JIS)[\s-]?\d", re.IGNORECASE)
_VENDOR = re.compile(r"^(?:PEM[\s-]?)?(?:S|SS|CLS|CLSS|FH|FHS|FHA|SO|SOS|BSO|BSOS|PF|KF|TSO|SP)"
                     r"-M?\d", re.IGNORECASE)
_BOUGHT_NAME = re.compile(r"\b(?:screw|bolt|nut|washer|rivet|pin|hinge|gasket|pem|"
                          r"ruuvi\w*|pultti\w*|mutter\w*|aluslevy\w*|aluslaat\w*|niit\w*|"
                          r"sarana\w*|tiivist\w*|schraube\w*|mutter|scheibe\w*|niete\w*|"
                          r"skruv\w*|bricka\w*)\b", re.IGNORECASE)
_INT = re.compile(r"^\d{1,4}(?:[.,]0+)?$")


def _row(line: list[dict], cols, x_lo: float, x_hi: float) -> BomRow | None:
    """A line under (or over) the header as a row, None when it isn't one."""
    cells: dict[int, list[str]] = {}
    for w in line:
        cx = (w["x0"] + w["x1"]) / 2
        if not x_lo <= cx <= x_hi:
            continue
        # The column whose header the word overlaps, else the nearest.
        i = min(range(len(cols)), key=lambda k: (
            not (cols[k][1] - 2 <= cx <= cols[k][2] + 2),
            abs((cols[k][1] + cols[k][2]) / 2 - cx)))
        cells.setdefault(i, []).append(w["text"])
    by_role = {cols[i][0]: " ".join(ws) for i, ws in cells.items() if cols[i][0]}
    qty_text = re.sub(r"(?i)\s*(pcs|pc|kpl|st|stk)\.?$", "", by_role.get(QTY, "").strip())
    item_text = by_role.get(ITEM, "").strip()
    if not (_INT.match(qty_text) or (QTY not in by_role and _INT.match(item_text))):
        return None
    qty = int(float(qty_text.replace(",", "."))) if _INT.match(qty_text) else 1
    item = int(float(item_text.replace(",", "."))) if _INT.match(item_text) else None
    raw = by_role.get(NUMBER, "")
    found = _NUMBER.findall(raw) or _NUMBER.findall(" ".join(" ".join(ws) for ws in
                                                             cells.values()))
    number = base_key(found[0]) if found else ""
    name = by_role.get(NAME, "")
    bought = bool(_STANDARD.match(raw.strip()) or _VENDOR.match(raw.strip())
                  or _STANDARD.match(name.strip()) or _BOUGHT_NAME.search(f"{raw} {name}"))
    if not (number or name or raw):
        return None
    return BomRow(item, qty, number, name or raw, bought, raw.strip())


def parts_list(page: PdfPage) -> tuple[bool, list[BomRow]]:
    """Whether the page has a parts-list header, and its rows: read upwards
    and downwards from the header, the way with more rows wins."""
    lines = _lines(page.words)
    for at, line in enumerate(lines):
        cols = _columns(line)
        if not _is_header(cols):
            continue
        h = max(w["bottom"] - w["top"] for w in line)
        x_lo, x_hi = cols[0][1] - 4 * h, cols[-1][2] + 12 * h
        best: list[BomRow] = []
        for step in (-1, 1):
            rows, last, k = [], line, at + step
            while 0 <= k < len(lines) and len(rows) < 300:
                nxt = lines[k]
                if abs(nxt[0]["top"] - last[0]["top"]) > 3 * h:
                    break
                row = _row(nxt, cols, x_lo, x_hi)
                if row is None:
                    break
                rows.append(row)
                last, k = nxt, k + step
            if len(rows) > len(best):
                best = rows
        return True, sorted(best, key=lambda r: (r.item is None, r.item or 0))
    return False, []


# ── 5 · Balloons ──────────────────────────────────────────────────────────────

def balloons(page: PdfPage) -> list[int]:
    """The numbers in balloons: a word of 1 to 3 digits at a circle's centre,
    smaller than the circle. Each number once, in order."""
    found: set[int] = set()
    for w in page.words:
        if not re.fullmatch(r"\d{1,3}", w["text"]):
            continue
        cx, cy = (w["x0"] + w["x1"]) / 2, (w["top"] + w["bottom"]) / 2
        for x, y, r in page.circles:
            if (abs(cx - x) < r * 0.45 and abs(cy - y) < r * 0.45
                    and w["bottom"] - w["top"] < 2 * r and w["x1"] - w["x0"] < 2 * r):
                found.add(int(w["text"]))
                break
    return sorted(found)


# ── 6 · Other cues ────────────────────────────────────────────────────────────

_TITLE = re.compile(
    r"\b(?:assembly|assy|weld(?:ed|ing)?|welded\s+assembly)\s+drawing\b|\bweldment\b|"
    r"\b(?:hitsaus)?kokoonpano(?:kuva|piirustus)?\b|\bzusammenbau(?:zeichnung)?\b|\bzsb\b|"
    r"\b(?:schweiß)?baugruppe\b|\bsammanställning(?:sritning)?\b", re.IGNORECASE)
_DETAIL = re.compile(r"\b(?:detail|part)\s+drawing\b|\bosapiirustus\b|\bosakuva\b|"
                     r"\beinzelteil(?:zeichnung)?\b|\bdetaljritning\b", re.IGNORECASE)
_SEE_LIST = re.compile(r"\bsee\s+(?:parts?\s+list|bom)\b|\b(?:ks|katso)\.?\s+osaluettelo|"
                       r"\bsiehe\s+st[üu]ckliste\b|\bse\s+stycklista\b", re.IGNORECASE)
# A fillet weld's throat, "a3", "a4 4x50": a small "a" (a big "A3" is a paper
# size), or a weld word or standard.
_THROAT = re.compile(r"(?<![\w.,])a ?\d{1,2}(?:[.,]\d)?(?: +\d+ ?[x×] ?\d+)?(?![\w.,])")
_WELD = re.compile(r"\bISO\s*5817\b|\bISO\s*3834\b|\b(?:weld(?:ed|ing|s)?|hitsa\w*|"
                   r"schwei\w*|svets\w*)\b", re.IGNORECASE)
_FLAT = re.compile(r"\bflat\s+pattern\b|\blevitys\b|\baukileikkaus\b|\babwicklung\b|"
                   r"\bbend\s+radi|\btaivutus|\bsärmäys|\b(?:up|down|ylös|alas)\s*\d+(?:[.,]\d+)?\s*°|"
                   r"\bk-?fa[ck]tor", re.IGNORECASE)
_MATERIAL = re.compile(r"\b(?:material|materiaali|werkstoff)\s*:\s*\S", re.IGNORECASE)


def analyse(pages: list[PdfPage], own: str) -> PdfAssembly:
    """Steps 2 to 7 over a PDF's pages (``read_pages``); ``own`` is the
    drawing's part number (its file's ``base_key``)."""
    out = PdfAssembly(own)
    lists = {p.number: parts_list(p) for p in pages if not p.scanned}
    listed = {r.number for _, rows in lists.values() for r in rows if r.number}
    # The title block's numbers are the drawing's own, unless they are rows
    # of a parts list that sits in the same corner.
    ours = {own} | ({k for p in pages for k in title_numbers(p)} - listed)
    header_page = None
    for p in pages:
        if p.scanned:
            out.scanned.append(p.number)
            continue
        has, rows = lists[p.number]
        if has and header_page is None:
            header_page, out.rows = p.number, [r for r in rows if r.number not in ours or
                                               not r.number]
        out.balloons = sorted(set(out.balloons) | set(balloons(p)))
        for key in callouts(p.text):
            if key not in ours and key not in out.called_out:
                out.called_out.append(key)
    text = "\n".join(p.text for p in pages)
    cue = out.cues.append
    if header_page is not None:
        cue(Cue("bom", "osaluettelo", 3, header_page))
    made = [r for r in out.rows if not r.bought]
    if len(made) >= 2:
        cue(Cue("bom_rows", f"osaluettelossa {len(made)} valmistettavaa osaa", 3, header_page))
    elif made:
        cue(Cue("bom_rows", "osaluettelossa 1 valmistettava osa", 1, header_page))
    elif out.rows:
        cue(Cue("bom_rows", "osaluettelossa vain ostettuja osia", 0, header_page))
    if len(out.balloons) >= 2:
        cue(Cue("balloons", f"{len(out.balloons)} numeroitua palloa", 2))
        items = {r.item for r in out.rows if r.item is not None}
        if len(items & set(out.balloons)) >= 2:
            cue(Cue("balloons_match", "pallot vastaavat osaluetteloa", 1))
    extra = [k for k in out.called_out if k not in {r.number for r in out.rows}]
    if extra:
        cue(Cue("called_out", f"{len(extra)} muuta osanumeroa", min(len(extra), 2)))
    if m := _TITLE.search(text):
        cue(Cue("title", f"\"{m.group(0).strip()}\"", 3))
    if m := _DETAIL.search(text):
        cue(Cue("detail", f"\"{m.group(0).strip()}\"", -2))
    see_list = _SEE_LIST.search(text)
    if see_list:
        cue(Cue("see_list", "materiaali osaluettelossa", 1))
    elif _MATERIAL.search(text) and header_page is None:
        cue(Cue("material", "yksi materiaali", -1))
    if _THROAT.search(text) or _WELD.search(text):
        cue(Cue("weld", "hitsausmerkintä", 1))
    if m := _FLAT.search(text):
        cue(Cue("flat", f"levitys tai taivutus (\"{m.group(0).strip()}\")", -2))
    if out.scanned:
        cue(Cue("scanned", "sivu ilman tekstiä: " + ", ".join(map(str, out.scanned)), 0))
    return out
