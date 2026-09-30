# Stremet Price Tool

A Streamlit app that prices sheet-metal parts from supplier price lists.

- **Hintalaskuri** (manual tab): enter rectangular parts. Parts are nested with
  a fast bounding-box packer.
- **DXF-nestaus** (DXF tab): upload one DXF per part. The real shapes are
  nested with the [Sparrow](https://github.com/JeroenGar/sparrow) solver.

Both tabs compare every priced sheet size and pick the cheapest. Parts of the
same material and thickness share sheets, and the app also looks for a plan
production can repeat: one sheet program cut many times (see
[Programs](#programs-repeatable-sheet-layouts)).

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

The DXF tab needs the Sparrow executable (see below).

Price lists are uploaded as PDFs in the sidebar (Tata Steel and Tibnor are
detected automatically). They are saved as `price_data_<supplier>.json` next to
the app, so re-upload only when a new monthly list arrives. Copper has no list
price; set its €/kg in the sidebar.

## Sparrow executable

The DXF tab runs the [Sparrow](https://github.com/JeroenGar/sparrow) solver.
`core/sparrow.py` looks for it in this order:

1. the `SPARROW_BIN` environment variable
2. `bin/sparrow` (Linux / macOS) or `bin/sparrow.exe` (Windows)
3. `sparrow` / `sparrow.exe` on the `PATH`

The repo ships both builds in `bin/`. For another platform, build or download
Sparrow and set `SPARROW_BIN` or replace the file in `bin/`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests take a few seconds. They cover pricing and copper, sheet costing and
utilisation, the DXF reader rules (on drawings generated in the test), the
Sparrow fixed-sheet search (with a small fake solver), the program plans of both
packers, the supplier store, and a smoke test of the whole page. `tests/test_sparrow.py::test_real_sparrow_binary`
also runs the real Sparrow executable and is skipped when it is not installed.

## Deploy (Streamlit Community Cloud)

1. Push the repo to GitHub. The Linux Sparrow binary in `bin/sparrow` is
   committed, so the DXF tab works there too.
2. On [streamlit.io/cloud](https://streamlit.io/cloud), connect the repo. It
   installs `requirements.txt` automatically.

## Project structure

```
app.py                  page setup: sidebar, then the two tabs

core/                   pure logic, no Streamlit
  pricing.py            price lookup, materials, thickness, densities, weight_kg, copper
  sheet_cost.py         price every sheet size (SheetOption), grouping, per-piece cost
  programs.py           repeatable programs: the kit search both packers share
  rect_nesting.py       bounding-box packer (manual tab)
  sparrow.py            Sparrow: DXF part → solver run → fixed sheets → programs → cost
  dxf.py                the DXF reader: the part that is shown, nested and priced
  geometry.py           polygon helpers (area, bbox, rotate, point-in-polygon)
  suppliers/            supplier list, detection, saved price lists, PDF parsers
    cells.py            cell cleaning and thickness-range helpers for the parsers
    tatasteel.py        Tata Steel price list
    tibnor.py           Tibnor price list

view/                   Streamlit UI
  sidebar.py            price-list upload, supplier status, copper price
  calculator_tab.py     manual tab and its product cards
  dxf_tab.py            DXF tab, its part cards and the Sparrow run
  common.py             inputs, the per-group loop, totals, pieces summary
  sheet_usage.py        the sheet-size table, metrics and price breakdown
  drawing.py            every SVG: sheet layouts and the DXF part preview
  sparrow_progress.py   progress bar while Sparrow runs

bin/                    the Sparrow executables (see "Sparrow executable")
tests/                  pytest suite (see "Tests" above)
```

## How a price is calculated

For each group of parts that share a material and thickness,
`core/sheet_cost.py` does the following:

1. Nest the parts on every sheet size that has a price, using the rectangle
   packer or Sparrow. Every sheet lies long side horizontal. The edge gaps
   (*reunavarat*: top, bottom, left, right; the clamp strip, *kynsiraina*, is
   given as the bottom one) are not usable, and the cut gap (*rankaväli*)
   keeps parts apart.
2. For each size, price up to two plans (see below): the fewest sheets, and
   the fewest programs. Each is one row in the sheet-size table.
3. Charge whole sheets: sheets × sheet weight × price per tonne × (1 + margin).
   The cheapest row is chosen; at the same price, the one with fewer programs.
4. Spread that cost over the parts by their real weight, which is the
   per-part summary at the bottom.

**Käyttöaste** (utilisation) is the real part area divided by the area of the
sheets paid for. The real part area excludes the gap and, for DXF parts, has
the holes removed.

## Programs: repeatable sheet layouts

A *program* is one sheet layout. The designer makes it once and production
cuts it as many times as needed. Identical sheets are always shown as one
layout with a count (*Ohjelma 1 · Levy 1–4 · ×4*), and the table's
**Ohjelmia** column counts the layouts.

The packers fill one sheet at a time, which uses the fewest sheets but can
leave uneven quantities spread over several layouts. `core/programs.py` looks
for the plan a designer would make instead, a *kit*: one layout holding
`q // R` of every part, cut R times, plus at most a small program for the
remainder `q % R`. Quantities stay exact; no extra pieces are made.

- R that divides every quantity gives a single program (100 A + 50 B → 10 A +
  5 B, ×10). These are tried first, up to one sheet more than the fewest.
- Otherwise the two R just below the fewest sheet count are tried, each with
  the remainder packed as usual.
- The kit plan is shown next to the fewest-sheets plan unless one is at least
  as good in both sheets and programs; then only that one is shown.

With Sparrow each kit check is a solver run, so at most four are made per sheet
size (`_KIT_BUDGET` in `core/sparrow.py`). The rectangle packer is instant and
has no limit.

## How a DXF file is read

`core/dxf.py` reads each file once. The card preview, the Sparrow nesting and
the price all use the same result.

- **Units** come from the `$INSUNITS` header. If that is missing, a
  `Un="mm"` text label is used. If that is missing too, a drawing that is
  exactly an ISO A0–A4 sheet is taken to be in mm. Otherwise the unit is
  guessed (mm, or inch if `$MEASUREMENT` says imperial): the card shows the
  size this gives, and the part is priced only after the user ticks that the
  size is right.
- **Layers**: layers named like drawing furniture or reference geometry (frame,
  border, title, info, text, dim, bend, centre, …) are left out.
- **The part** is the largest closed outline. Outlines directly inside it are
  its holes, and anything outside it (detail views, sketches, stray lines) is
  dropped.
- **Drawn but not cut**: bend and centre lines, dashed lines, countersinks and
  threads drawn as circles inside a hole, and the ISO thread symbol (a thin
  ¾-circle around a hole).
- **Not priced**, with the reason shown on the card: the file can't be read,
  there is no closed outline, an open line sits
  inside the part, or an outline crosses itself.

A frame drawn on the *same* layer as the part can't be told apart from a
ring-shaped part, so keep frames on their own layer.

## Where to change things

| What you want to change | Where |
|---|---|
| Support a new supplier PDF | a parser in `core/suppliers/` + an entry in `SUPPLIERS` (`core/suppliers/__init__.py`) |
| Densities, copper sizes or price range | `core/pricing.py` |
| How a sheet size is priced | `core/sheet_cost.py` |
| How repeatable programs are searched | `core/programs.py` |
| Which DXF layers are left out, or which drawing marks are recognised | `core/dxf.py` |
| Sparrow settings or the fixed-sheet search | `core/sparrow.py` |
| Inputs shared by both tabs | `view/common.py` |
| Colours and layout drawings | `view/drawing.py` |
