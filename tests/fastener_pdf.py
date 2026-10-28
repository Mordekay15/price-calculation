"""A drawing PDF laid out like an ABB sheet's fastener callouts (from
RFQ 580-07, 3AXD50000101375 and 3AXD50000050459)."""

import io

from reportlab.pdfgen import canvas


def drawing_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(1190, 842))
    c.setFont("Helvetica", 8)

    def text(x, top, s, right=False):
        (c.drawRightString if right else c.drawString)(x, 842 - top - 8, s)

    text(82, 60, "F 2 holes removed and 4 Kalei Nut added 05-Apr-23 R.Orgusaar 500002059051")
    text(82, 70, "C 2pcs of M4x12 PEM replaced with M4x8 13-Aug-20 Nele Vaade")
    text(953, 508, "Identification mark M5 KALEI NUT")      # its count on the next line
    text(953, 519, "inside marked area 4 pcs")
    text(792, 585, "M6 KALEI NUT", right=True)               # right-aligned, count below
    text(790, 595, "6 pcs", right=True)
    text(719, 343, "Hole for PEM-FH-M4,")                    # a hole, not a fastener
    text(799, 354, "27pcs")
    text(592, 625, "PEM-FH-M4-12 or equal")
    text(592, 636, "25pcs")
    text(119, 755, "UNMARKED BEND RADII R=0,6mm PEM-FH-M4-8 or equal, 2pcs")
    text(658, 430, "7, 8pcs")                                # holes
    text(300, 200, "PEM-FH-M6-12")                           # no count: 1 each
    text(500, 300, "PEM-FH-M6-12")
    c.save()
    return buf.getvalue()
