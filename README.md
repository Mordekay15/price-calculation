# Stremet Price Tool

A Streamlit app that prices sheet-metal parts from supplier price lists.

- **Hintalaskuri** (manual tab): enter rectangular parts. Parts are nested with
  a fast bounding-box packer.
- **DXF-nestaus** (DXF tab): upload one DXF per part. The real shapes are
  nested with the [Sparrow](https://github.com/JeroenGar/sparrow) solver. Dropped
  files become part cards and the drop area empties; a card's **Poista**
  button removes the part, and **Poista kaikki** above the cards (asks to
  confirm) removes them all. A file whose name is already on a card is not
  added again: a warning names it and its card for 15 seconds (to replace a changed
  drawing, remove the old card first). When a card has its material and thickness and
  others are still empty, a question between it and the first empty card
  below asks whether the empty cards get the same (*Kyllä* / *Ei*). It is
  shown once and goes away once answered; files dropped later are asked
  about once, between the last old card and the first new one.

Both tabs compare every priced sheet size and pick the cheapest, including a
plan that cuts the parts from several sizes. Parts of the same material and
thickness share sheets. The app looks for the plan that uses
the fewest sheets ([Fewest sheets](#fewest-sheets-the-cutting-stock-plan)) and
for a plan production can repeat: one sheet program cut many times (see
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
utilisation, the fewest-sheets plan, the DXF reader rules (on drawings generated in the test), the
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
  cutting_stock.py      the fewest-sheets plan: candidate sheets + integer program
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
2. For each size, price up to three plans (see below): sheet by sheet, the
   fewest sheets over the whole order, and the fewest programs. Each is one
   row in the sheet-size table; a plan another one beats in both sheets and
   programs is left out. A size the
   parts don't fit says which part is too big, e.g. *ITM-072558 (3059 mm) ei
   mahdu*.
3. Charge whole sheets: sheets × sheet weight × price per tonne × (1 + margin).
   The cheapest row is chosen; at the same price, the one with fewer programs.
4. Spread that cost over the parts by their real weight, which is the
   per-part summary at the bottom.

**Käyttöaste** (utilisation) is the real part area divided by the area of the
sheets paid for. The real part area excludes the gap and, for DXF parts, has
the holes removed.

## Fewest sheets: the cutting-stock plan

The packers fill one sheet at a time and never look ahead, so they can miss a
plan that needs fewer sheets. Example, 10 each of seven square plates (524–1194
mm) on 1250 × 2500: filled sheet by sheet they take 20 sheets. Planned as a
whole they take 18: violet + 2 light blue (each over a pink) ×5, red + green +
yellow (over a dark blue) ×10, and the 5 violets left two to a sheet.

`core/cutting_stock.py` finds such plans with the classic cutting-stock method:

1. **Candidate sheets** (how many of each part one sheet holds): the sheets the
   packers already made, each part on its own sheet, sheets the LP's part
   prices make worth adding (column generation), and sheets of two parts that
   suit each other (k of one, m of another, the rest filled largest first).
   Leftover demand gets its own round, as it needs other partners.
2. **An integer program** (`scipy.optimize.milp`) picks how many times to cut
   each candidate: the fewest sheets, then the fewest programs. Pieces it
   makes too many come off single copies; no extra pieces are made.

**Several sheet sizes.** Each size's candidate sheets are kept, also for a
size too small for some parts, and one more integer program picks over all of
them at each sheet's price (weight × €/tn with the margin): the big parts on a
big sheet, the rest on a smaller one where that costs less. It is shown as one
more row (e.g. *1.0 × 2.0 m (3) + 1.25 × 2.5 m (7) + 1.5 × 3.0 m (5)*) when it
uses two sizes or more; the layouts are drawn per size, to one scale, and the
price breakdown lists each size. In the example above with 1000 × 2000,
1250 × 2500 and 1500 × 3000 at one price per tonne, the best single size
costs 894 € and the mixed plan 834 €.

Candidate sheets are checked with the box packer (instant). For DXF parts each
real shape is placed in its box, which is always a valid layout; Sparrow's own
sheets are candidates too, so where the shapes interlock its tighter sheets
are used. The search adds no Sparrow runs; it is capped at 4000 box checks per
sheet size (`max_checks`), a few seconds on a large order. The manual tab
keeps its result until a part or setting changes.

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
- **Laid along its length**: the part is turned so its smallest bounding
  rectangle lies straight, long side horizontal, as a designer lays it before
  nesting. A curved strip drawn diagonally (2120 × 2061 mm as drawn) is then
  2936 × 437 mm, and a part drawn standing lies down. The card size, the
  price, the nesting angle and the nesting all use the turned part; a part
  already lying straight (within 0.5°) stays as drawn.
- **Drawn but not cut**: bend and centre lines, dashed lines, countersinks and
  threads drawn as circles inside a hole, and the ISO thread symbol (a thin
  ¾-circle around a hole).
- **Not priced**, with the reason shown on the card: the file can't be read,
  there is no closed outline, an open line sits
  inside the part, or an outline crosses itself.

**Nesting angle and rolling direction.** *Sallittu nestauskulma* (0/180 and
90/270, both ticked by default) has its own row above the search time and the
run button. With several parts a switch picks *Sama kaikille* (one
choice for every part, the default) or *Osakohtainen* (each card has its own). The rolling direction
(*valssaussuunta*) runs along the sheet's long side. Angles are counted from
the part as its card shows it, laid along its length: 0/180 keeps that length
along the rolling direction, 90/270 across it. With both ticked the part may
take any quarter turn; with one, only that angle or half a turn round. The
layout then notes the rolling direction.

A frame drawn on the *same* layer as the part can't be told apart from a
ring-shaped part, so keep frames on their own layer.

## Where to change things

| What you want to change | Where |
|---|---|
| Which Tibnor sheet sizes are priced | `TIBNOR_SIZES` in `core/suppliers/tibnor.py` (re-upload the Tibnor PDF after a change) |
| Support a new supplier PDF | a parser in `core/suppliers/` + an entry in `SUPPLIERS` (`core/suppliers/__init__.py`) |
| Densities, copper sizes or price range | `core/pricing.py` |
| How a sheet size is priced | `core/sheet_cost.py` |
| How repeatable programs are searched | `core/programs.py` |
| How the fewest-sheets plan is searched | `core/cutting_stock.py` |
| Which DXF layers are left out, or which drawing marks are recognised | `core/dxf.py` |
| Sparrow settings or the fixed-sheet search | `core/sparrow.py` |
| Inputs shared by both tabs | `view/common.py` |
| Colours and layout drawings | `view/drawing.py` |
