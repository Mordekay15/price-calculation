# Stremet Price Tool

A Streamlit app that prices sheet-metal parts from supplier price lists.

- **Peruslaskuri** (manual tab): enter rectangular parts. Parts are nested with
  a fast bounding-box packer.
- **Edistynyt** (the DXF tab): a menu of three screens, as in Offeri:
  **BOM & Tiedostot**, **Nestaus** and **Hinnoittelu**: the files and the part cards (with each card's
  work), then the nesting, then the prices: the batch totals, one breakdown
  table switched by batch size, and the downloads. Only the open screen is drawn (the menu
  reruns the page on a click), but each one runs every card, headless where
  it isn't drawn (`view/ui.py`), so every screen prices every part and the
  other screens' inputs are kept. Upload one DXF or DWG per part. The real shapes are
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

## Work (Työvaiheet)

Every part card has a folded **Työvaiheet** section (on the DXF tab, on the
BOM & Tiedostot screen). On the manual tab the prices end with
**Kappalehinnat**: the price per piece, material plus work, as
`c + F / kpl` (what each piece costs, plus the batch's fixed costs shared over
the quantity). Work has no margin; its rates are selling prices.

- **Cutting**: *Laser* or *Levari*. Laser = cutting length (m/kpl) × seconds per
  metre × €/s. The cutting length is read from the DXF (outline, holes and open
  cuts; a rectangle's outline on the manual tab), the seconds and €/s from the
  laser table in `core/work.py` (Platino 6 kW / Genius 6 kW, by material family
  and thickness: steel, RST/HST, aluminium, copper/brass; between two rows the
  time is interpolated). Levari = hits × €/hit. Cutting is at least the sheet
  minimum (7 €) per sheet the group uses; what is missing is shared in
  proportion to the parts' cutting.
- **Bends** and **threads**: count × € each (0.20 by default). The DXF gives
  both: bend lines on a bend layer (e.g. `IV_BEND_DOWN`) and threads drawn with
  the ISO ¾-circle mark. When the card has a STEP model, the bends are the
  model's (a flat DXF often has no bend lines), and the card's preview draws
  the model's bend lines on the DXF (green up, orange down), lined up with it
  even when the DXF is turned or mirrored.
- **Fasteners** (kiinnikkeet: press nuts, screws, bolts, rivets…): count ×
  (one fastener's price + putting one in). By default 0.80 + 0.20 € in RST/HST
  and 0.05 + 0.20 € otherwise. The DXF gives the count from notes like
  "M5-PURISTEMUTTERIT X4KPL".
- **Welding**: metres ÷ m/h × €/h. **Subcontracting** €/kpl. **Programming**
  € per batch.
- **Setup** per batch by the number of work phases: 40 / 60 / 80 €.

A grey value in an input is the automatic one; typing replaces it, clearing the
input goes back to it. The rates are under **Työn hinnat** in the sidebar.

## Quote (Tarjous) and batch sizes

The folded **Tarjous** section above the tabs holds the quote's header:
quote number, customer, contact, seller, date, validity, payment terms,
delivery and notes. It also holds the **Eräkoot** (batch sizes, default
`1 5 10`). Under each tab's results, **Eräkohtaiset hinnat** prices every
part at every batch size and shows the quote's total at each one. It also
shows **Laskenta-aika**, the time since the first part could be priced.

- The table's **Määrä** shows the pieces at each batch size (`1 · 5 · 10`).
  An assembly's `└` rows are what each part, bought part and the assembly
  work adds to one assembly (kpl / kokoonpano × €/kpl), so they add up to the
  assembly's €/kpl; their Määrä is kpl / kokoonpano × the batch size.
- **Näytä laskennan erittely** under the table picks one batch size and
  shows each part's pieces in the batch, sheets, material and every work cost
  €/kpl (the batch costs € and shared per piece), and each assembly's steps:
  its parts, bought parts, assembly work, welding, other, €/kpl and total.
- Parts are nested once, at the quantities on their cards. For another batch
  size `q`, a group needs `ceil(sheets × kg(q) / kg)` whole sheets (at least
  one), each costing as much as in the nesting. The material is shared by piece
  weight, as in the summary. At the nested quantity this is the exact nested
  price; at other quantities it is an estimate that keeps the nesting's scrap
  rate.
- Work is `work_cost` at `q`, so the batch costs are shared over `q`. The
  sheet minimum is applied over that batch size's sheets.
- **Lataa tarjous-PDF** makes the customer quote (reportlab). The material
  column can be hidden. **Excel-vienti** writes a sheet `Tarjous` (one row
  per part and batch size) and a sheet `Erittely` (material and each work
  cost per piece).
- On the DXF tab, **Lataa puhtaat DXF:t tuotantoon** zips one clean DXF per
  part. Each file is R2010 in mm and has only the outline, holes and open cuts,
  on layer `CUT`. Bend lines and other markings can be added on `MARKING`.
  File names come from a template such as `{part}_{material}_{thickness}mm`.

## One card per part (DXF tab)

Drop every file you have for a part at once: the flat DXF, the drawing as DWG
or DXF, the STEP model and the PDF. They become **one card** (`core/items.py`):

- **Grouping.** Files of one part number go together, ignoring revisions
  (`-revA`, `_A`) and numbered pieces (`_1`, `_2`): `3aua0000098622_1.dxf`,
  `_2.dxf`, `3AUA0000098622.stp` and `.pdf` are one part. A drawing saved
  under its drawing number (`DRW-023179_A.dwg`) joins the part its title block
  names (`ITM-072558.A`), when it looks like a drawing (a paper-size frame, or
  other views around the part) and names exactly one card's part number.
- **Choosing the shape.** Each DXF/DWG is scored: a clean flat pattern beats
  a drawing sheet (frame, views and title block, many texts, no unit). With a
  STEP model, a DXF the size of the unfolded blank is confirmed by it; when no
  DXF agrees, the blank unfolded from the model is used. The card lists its
  files, marks the one priced (✓) with the reason, and **Muoto tiedostosta**
  picks another.
- **The rest of the card.** PDFs show their first page under
  **Piirustukset**; a PDF named otherwise links by the part number in its
  text. The material search reads the texts of all the card's files.
  **Poista** removes the card and its files. The **Tarkastettu** tick is
  counted above the cards. In laser mode a card also prices punching (Levari)
  for comparison, and says which method would be cheaper.

## STEP models: BOM and unfolding

A STEP file dropped on the DXF tab is read with Open CASCADE (`cadquery-ocp`).
**STEP-mallit** shows its BOM: the assembly tree with the quantity per parent
and in the whole model. Each body is checked for sheet metal:

- A sheet part has two parallel skins at one thickness, made of flat flanges
  and cylindrical bends. One skin is laid flat face by face, with every bend
  at its neutral radius `inner radius + K × thickness` (the **K-kerroin**,
  default 0.44). The flat outline, holes and bend lines become a part card,
  with the model's total as its quantity. The card's bends come from the bend
  lines (layers `BEND_UP` / `BEND_DOWN`), so the work cost uses them. The thickness note (`t = 2 mm`) is read
  by the material proposals.
- The unfolded part is on the card of its part number with the part's DXF,
  if there is one; the card's shape is chosen as described in
  [One card per part](#one-card-per-part-dxf-tab).
- Other bodies (bought or machined parts, or anything too thick for its size)
  are listed as *ei levyosa* with the reason, and are not priced. Tiny loose
  bodies in a part (under 1 % of its main body, e.g. what a form tool leaves
  behind) are left out, so such a part is still one part, not an assembly.
- **Näytä 3D** shows a body you can turn (plotly), in the BOM and on a card
  made from a model.

The unfolding handles straight bends (cylinders along a straight bend line).
Cones, rolled parts and hems are not supported: such a part is marked as not
sheet, and a DXF of it is needed. Without `cadquery-ocp`, STEP files are only
attached to the cards by part number.

## A ZIP of item folders

A dropped **ZIP** is unpacked (`core/archive.py`), ZIPs inside it too, and its
files are taken as if dropped one by one, folder by folder. Files the app
doesn't use (Creo `.prt`, `.drw`, `.frm`, notes…) are skipped and named under
the drop area. Grouping works by part number as usual, and the folder helps
where that can't: a drawing that names no part joins the one other item in
its folder, and so does a PDF. A folder with several items keeps them
apart, and files at the top of the ZIP are matched by name and text only.
Each card shows its folder. Only the files the app uses are read, one at a
time, with a progress bar.

In a drop of more than 5 files, a STEP model over 5 MB (often a punched panel
with thousands of faces, up to a minute to read) is not read on load. Its card
has a **Lue 3D-malli** button, and the drop area one for all of them; once
read, the model brings its bends, thickness and size check to the card.

With more than 20 cards, the cards are shown **20 at a time** (*Näytettävät
osat*). Every card is still priced: the ones not shown run the same card code
without drawing (`view/ui.py`), with their inputs kept. The attached-files list
shows only files without a part, 30 at a time.

To keep memory low (a hosted app's memory is shared by everyone using it), a
DXF's lines are dropped once its part is built, a file of a ZIP or folder is
kept as a way to read it again rather than its bytes, and a STEP model's 3D
bodies are let go after unfolding (read again for **Näytä 3D** or a new K
factor). The 580-07 RFQ (1,020 files) takes about 0.9 GB.

## Assemblies (Kokoonpanot)

Parts sold together are one **assembly** (`core/assemblies.py`,
`view/assemblies.py`), shown as one line on the quote, PDF and Excel. The
Excel export gets a **Kokoonpanot** sheet with each assembly's parts, bought
parts, work and contents.

- **From a STEP assembly.** A dropped STEP file of several parts becomes an
  assembly automatically. Its sheet parts are the cards they are on, with the
  model's quantities as kpl / kokoonpano. Its other bodies (pins, screws,
  machined parts) become **bought parts** with a € price to fill in and an
  **Asennus (€/kpl)** for putting one in (0.20 € by default for fasteners and
  pins, e.g. pressing a PEM nut). Removing
  the STEP file removes its assembly.
- **From an assembly drawing.** A DXF that is only a drawing sheet, with no
  blank to cut (the biggest closed shape a line under 3 mm wide among other
  views), gets a note on its card and is not priced. When its drawings call
  out part numbers (a number alone on its line, as an item's leader label;
  numbers among words such as `ASSEMBLY KIT: …` are the title block's), it
  becomes an assembly of the cards with those numbers, 1 kpl each, with the
  drawing shown in it instead of as a card in Osat. The ones neither dropped
  yet nor added as bought parts are listed as awaited.
- **Removing.** An assembly's **Poista** removes it with its parts' cards
  (one also in another assembly stays), its drawing and its STEP file.
- **Apart from the loose parts.** **Osat** lists only the parts in no
  assembly. Each assembly below it shows its own cards, paged the same way.
  Their **Määrä** starts as kpl / kokoonpano × the largest batch size (a
  loose card's as the largest batch size), shown in grey: the quantity they
  are nested at. A typed Määrä is nested instead; clearing it goes back.
- **No cost twice.** A card in an assembly has no fasteners or welding: the
  assembly's bought parts (PEM nuts and studs too, with their pressing as
  Asennus) and its welding price them. The card keeps the part's own work
  and its setup (laser, bending); the assembly's setup is for assembling.
- **Pricing.** A batch size counts assemblies. Each part is priced at
  `batch × kpl / kokoonpano` pieces, so its setup and sheets are shared over
  all of them. One assembly costs its parts' €/kpl × kpl/kokoonpano, plus the
  bought parts, plus the assembly work. The work is `(setup min / batch +
  fitting min) / 60 × €/h`, welding at the sidebar's welding rate, and
  anything else per assembly. The assembly lists its parts with their kpl /
  kokoonpano (the cards don't mention it), and a part in an assembly is not
  quoted separately.

## Materials

A card without a material is filled in automatically, with no AI, from its
own files: the material grade from the linked PDF's title block **Material**
field (other lines on the drawing don't count then), or else from any line of
the PDFs and the DXF texts that names one; the thickness from "PL 3", "t = 2",
"2,0MM" and the like, and from the card's STEP model. It knows structural
steels (S235, S355MC…), cold-rolled (DC01…), hot-dip galvanised (DX51D+Z275,
HDG, kuumasinkitty; the coating picks the price-list row), stainless and
acid-proof (1.4301, AISI 316…), aluminium (EN AW-5754…), copper and brass.
Grades are matched to the price list's materials, and thicknesses must be in
the price list (±0.05 mm).

The card says what its material was read from. A card whose files disagree
(two grades, or 3 mm and 2 mm) is left empty and says why, so you choose the
material yourself. A card is searched again only when its files change, so a
material you clear stays clear. Texts saved earlier in
`material_aliases.json` still map to the material chosen for them.

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

## DWG drawings

The DXF tab also takes DWG files. Python can't read DWG, so each one is first
converted to DXF (`core/dwg.py`), by the first converter found:

1. **ODA File Converter** (free, most reliable): install it from the Open
   Design Alliance site; ezdxf finds it in its default install folder.
2. **LibreDWG** `dwg2dxf` (open source): the `LIBREDWG_BIN` environment
   variable, `bin/dwg2dxf` (`bin/dwg2dxf.exe` on Windows), or `dwg2dxf` on the
   `PATH`.

Without either, or when a conversion fails, the part's card says why and asks
for the drawing as DXF.

`bin/dwg2dxf` (Linux) is built from the LibreDWG source by the GitHub Actions
workflow `.github/workflows/build-dwg2dxf.yml`, which also checks that a plate
survives a round trip through DWG. Run it again (Actions → Build dwg2dxf) to
rebuild, or change `LIBREDWG_VERSION` in it to update.

## Sparrow executable

The DXF tab runs the [Sparrow](https://github.com/JeroenGar/sparrow) solver.
`core/sparrow.py` looks for it in this order:

1. the `SPARROW_BIN` environment variable
2. `bin/sparrow` (Linux / macOS) or `bin/sparrow.exe` (Windows)
3. `sparrow` / `sparrow.exe` on the `PATH`

The repo ships both builds in `bin/`. For another platform, build or download
Sparrow and set `SPARROW_BIN` or replace the file in `bin/`.

Each Sparrow run takes the search time (*Sijoittelun hakuaika*). The DXF tab
remembers every successful run for the session: the same question (parts,
strip, cut gap, search time) is answered without running Sparrow again.

A result is stored by what decides the nesting — the parts, their quantities
and angles, and the sheet sizes — not by what only prices it. So without
pressing *Laske levykäyttö*:

- **margin, copper €/kg, a part's thickness or material** (when every sheet
  size's price changes by one factor): the result is re-priced at once — the
  same plans stay cheapest;
- **a new price list** (sizes' €/tn changing unevenly, so another plan may be
  cheapest): planned again from the remembered runs, with no new Sparrow runs
  (a few seconds; only a planned sheet that loses pieces may be nested again).

A new part, quantity, angle, gap, search time or sheet size still needs the
button. The manual tab applies its margin the same way, instantly.

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
app.py                  page setup: sidebar, the Tarjous header, then the two tabs

core/                   pure logic, no Streamlit
  pricing.py            price lookup, materials, thickness, densities, weight_kg, copper
  sheet_cost.py         price every sheet size (SheetOption), grouping, per-piece cost
  work.py               work: laser table, cutting / bends / threads / welding / setup, sheet minimum
  batches.py            prices per batch size from one nesting
  quote.py              quote PDF, Excel export, clean DXFs and their ZIP
  assemblies.py         assemblies: parts × kpl/kokoonpano + bought parts + assembly work
  linking.py            part numbers from file names; files → parts by name or text
  archive.py            dropped ZIPs: files folder by folder, nested ZIPs, Finnish names
  items.py              one card per part: grouping its files, choosing the shape
  materials.py          material/thickness hints, proposals, alias table, optional Claude
  step.py               STEP: BOM with quantities, sheet unfolding, flat DXF, 3D mesh
  programs.py           repeatable programs: the kit search both packers share
  cutting_stock.py      the fewest-sheets plan: candidate sheets + integer program
  rect_nesting.py       bounding-box packer (manual tab)
  sparrow.py            Sparrow: DXF part → solver run → fixed sheets → programs → cost
  dxf.py                the DXF reader: the part that is shown, nested and priced
  dwg.py                DWG → DXF with ODA File Converter or LibreDWG, then the DXF reader
  geometry.py           polygon helpers (area, bbox, rotate, point-in-polygon)
  suppliers/            supplier list, detection, saved price lists, PDF parsers
    cells.py            cell cleaning and thickness-range helpers for the parsers
    tatasteel.py        Tata Steel price list
    tibnor.py           Tibnor price list

view/                   Streamlit UI
  sidebar.py            price-list upload, supplier status, copper price
  calculator_tab.py     manual tab and its product cards
  dxf_tab.py            DXF tab: the order of the page and the part cards
  dxf_files.py          the DXF tab's dropped files, grouped into cards (ingest, sync, remove)
  dxf_nesting.py        the DXF tab's run row, the Sparrow runs, cached results and prices
  common.py             inputs, the per-group loop, totals, pieces summary
  sheet_usage.py        the sheet-size table, metrics and price breakdown
  work.py               the work rates (sidebar), Työvaiheet on the cards, work per product
  quote.py              the Tarjous header, prices per batch size and the downloads
  attachments.py        PDF/STEP files on the DXF tab, links, previews, Tarkastettu
  material_agent.py     materials filled in from each card's files
  bom.py                STEP-mallit: BOM, cards from unfolded parts, K factor, 3D view
  assemblies.py         Kokoonpanot: from STEP assemblies and assembly drawings, parts, bought parts, work
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
   each candidate: first the lowest price (solved to the optimum), then at
   that price the fewest programs, with exact quantities: a candidate may be
   cut a whole number of times plus one copy with pieces left off, which
   counts as a program of its own. No extra pieces are made. A sheet
   that loses pieces this way is nested again with just the pieces it keeps
   (one Sparrow run, or the box packer in the manual tab), so it keeps no
   holes where the extra pieces were.

**Good programs are kept.** A candidate sheet at least 80 % full (real part
area over the sheet bought, as *käyttöaste* shows it) whose repeats use up its
parts exactly — e.g. two light blue plates per 1 × 2 m sheet ×5 for an order of
10 — is kept as its own program, and the rest is planned without those parts.
Production would rather cut one good program more often than have a part split
over programs or sheet sizes to save a little steel. It is kept only while the
plan costs at most 5 % more than the cheapest; otherwise the cheapest plan is
used. (`GOOD_FILL` and `KEEP_MAX_EXTRA` in `core/cutting_stock.py`.)

**Several sheet sizes.** Each size's candidate sheets are kept, also for a
size too small for some parts, and one more integer program picks over all of
them at each sheet's price (weight × €/tn with the margin): the big parts on a
big sheet, the rest on a smaller one where that costs less. It is shown as one
more row (e.g. *1.0 × 2.0 m (3) + 1.25 × 2.5 m (7) + 1.5 × 3.0 m (5)*) when it
uses two sizes or more; the layouts are drawn per size, to one scale, and the
price breakdown lists each size. In the example above with 1000 × 2000,
1250 × 2500 and 1500 × 3000 at one price per tonne, the best single size
costs 894 € and the mixed plan 834 €.

**Parts that hook together.** The box check can't see shapes that interlock:
square plates with 38.9 mm corner notches laid as a checkerboard (big, small,
big, small over small, big, small, big) hold 4 + 4 on a 1250 × 2500 sheet,
where their boxes need 2557 mm. So on each sheet size the app looks for *near
misses*: for each part and each pair of parts, in the order's ratio, the most
the boxes fit on a sheet and then one more, with how much longer the sheet
would have to be for those boxes. The two smallest misses that have the area
are nested by Sparrow on the real shapes (one run each, `_NEAR_BUDGET` in
`core/sparrow.py`); those that fit join the candidates. A pair whose boxes
miss by a lot (two 704 mm plates can't share the 1250 mm height whatever
their notches) is not tried.

Candidate sheets are checked with the box packer (instant). For DXF parts each
real shape is placed in its box, which is always a valid layout; Sparrow's own
sheets are candidates too, so where the shapes interlock its tighter sheets
are used. Apart from the near-miss runs above it adds no Sparrow runs; it is
capped at 4000 box checks per sheet size (`max_checks`), a few seconds on a
large order. The manual tab
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
  guessed (mm, or inch if `$MEASUREMENT` says imperial). The guess is
  confirmed by the part's other files: the STEP model's unfolded blank is the
  same size (within 3 %), or its PDF drawing writes the width or height as a
  dimension (e.g. `(516.4)`). Otherwise the card shows the size the guess
  gives, and the part is priced only after the user ticks that it is right.
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
- **Slits**: a single straight line inside the part (louvres, lanced slits)
  is cut as a line and counted in the cut length.
- **Not priced**, with the reason shown on the card: the file can't be read,
  there is no closed outline, an open line that isn't straight sits inside
  the part (a gap in a hole's outline), or an outline crosses itself.

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
| The quote PDF or Excel layout | `core/quote.py` |
| Material grades that are recognised | `_GRADES` in `core/materials.py` |
| How STEP bodies are unfolded, or the default K factor | `core/step.py` |
| How an assembly is priced | `core/assemblies.py` |
