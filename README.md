# Stremet Price Tool

A Streamlit app that prices sheet-metal parts from supplier price lists.

- **Hintalaskuri** (manual tab): enter rectangular parts. Parts are nested with
  a fast bounding-box packer.
- **DXF-nestaus** (DXF tab): upload one DXF per part. The real shapes are
  nested with the [Sparrow](https://github.com/JeroenGar/sparrow) solver.

Both tabs compare every priced sheet size and pick the cheapest.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

The DXF tab needs the Sparrow executable. See [`bin/README.md`](bin/README.md).

Price lists are uploaded as PDFs in the sidebar (Tata Steel and Tibnor are
detected automatically). They are saved as `price_data_<supplier>.json` next to
the app, so re-upload only when a new monthly list arrives. Copper has no list
price; set its €/kg in the sidebar.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests take a few seconds. They cover pricing and copper, sheet costing and
utilisation, the DXF reader rules (on drawings generated in the test), the
Sparrow fixed-sheet search (with a small fake solver), the price store, and a
smoke test of the whole page. `tests/test_sparrow.py::test_real_sparrow_binary`
also runs the real Sparrow executable and is skipped when it is not installed.

## Deploy (Streamlit Community Cloud)

1. Push the repo to GitHub, including a Linux Sparrow binary in `bin/` if the
   DXF tab is needed (`git add -f bin/sparrow`).
2. On [streamlit.io/cloud](https://streamlit.io/cloud), connect the repo. It
   installs `requirements.txt` automatically.

## Project structure

```
app.py                  page setup: sidebar, then the two tabs

core/                   pure logic, no Streamlit
  pricing.py            price lookup, materials, densities, weights, copper
  sheet_cost.py         price every sheet size for a group (packer passed in)
  rect_nesting.py       bounding-box packer (manual tab)
  sparrow.py            Sparrow: DXF part → solver run → fixed sheets → cost
  dxf.py                the DXF reader: the part that is shown, nested and priced
  geometry.py           polygon helpers (area, bbox, rotate, point-in-polygon)
  price_store.py        the supplier list and the saved price-list JSON files
  price_parser/         PDF price-list parsers (Tata Steel, Tibnor)

view/                   Streamlit UI
  sidebar.py            price-list upload, supplier status, copper price
  calculator_tab.py     manual tab and its product cards
  dxf_tab.py            DXF tab, its part cards and the Sparrow run
  common.py             inputs, grouping and totals shared by both tabs
  sheet_usage.py        the sheet-size table, metrics, breakdown and drawings
  sparrow_progress.py   progress bar while Sparrow runs

bin/                    the Sparrow executable (see bin/README.md)
tests/                  pytest suite (see "Tests" above)
```

## How a price is calculated

For each group of parts that share a material and thickness,
`core/sheet_cost.py` does the following:

1. Nest the parts on every sheet size that has a price, using the rectangle
   packer or Sparrow. The clamp strip (*kynsiraina*) on the long side is not
   usable, and the cut gap (*rankaväli*) keeps parts apart.
2. Charge whole sheets: sheets × sheet weight × price per tonne × (1 + margin).
3. Spread that cost over the parts by their real weight, which is the
   per-part summary at the bottom.

**Käyttöaste** (utilisation) is the real part area divided by the area of the
sheets paid for. The real part area excludes the gap and, for DXF parts, has
the holes removed.

## How a DXF file is read

`core/dxf.py` reads each file once. The card preview, the Sparrow nesting and
the price all use the same result.

- **Units** come from the `$INSUNITS` header. If that is missing, a
  `Un="mm"` text label is used. If that is missing too, a drawing that is
  exactly an ISO A0–A4 sheet is taken to be in mm.
- **Layers**: layers named like drawing furniture or reference geometry (frame,
  border, title, info, text, dim, bend, centre, …) are left out by default. The
  card's layer picker changes the choice.
- **The part** is the largest closed outline. Outlines directly inside it are
  its holes, and anything outside it (detail views, sketches, stray lines) is
  dropped.
- **Drawn but not cut**: bend and centre lines, dashed lines, countersinks and
  threads drawn as circles inside a hole, and the ISO thread symbol (a thin
  ¾-circle around a hole).
- **Not priced**, with the reason shown on the card: the file can't be read,
  there is no unit anywhere, there is no closed outline, an open line sits
  inside the part, or an outline crosses itself.

A frame drawn on the *same* layer as the part can't be told apart from a
ring-shaped part, so keep frames on their own layer.

## Where to change things

| What you want to change | Where |
|---|---|
| Support a new supplier PDF | `core/price_parser/` (new parser) + `SUPPLIERS` in `core/price_store.py` |
| Densities, copper sizes or price range | `core/pricing.py` |
| How a sheet size is priced | `core/sheet_cost.py` |
| Which DXF layers are left out, or which drawing marks are recognised | `core/dxf.py` |
| Sparrow settings or the fixed-sheet search | `core/sparrow.py` |
| Inputs shared by both tabs | `view/common.py` |
