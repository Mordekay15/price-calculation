"""The quote in the UI: the "Tarjous" header above the tabs (customer, quote
number, terms and the batch sizes), and under each tab's results the price
per batch size with the downloads: the quote PDF, the Excel export and, on the
DXF tab, the clean DXFs for production (see ``core.batches``, ``core.quote``)."""

from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import date

import streamlit as st

from core.assemblies import Assembly, assembly_prices, fit_eur, per_unit
from core.batches import (
    DEFAULT_BATCH_SIZES,
    BatchPart,
    GroupSheets,
    batch_prices,
    order_prices,
    parse_batch_sizes,
)
from core.quote import (
    DEFAULT_DXF_NAME,
    CleanPart,
    QuoteInfo,
    QuoteLine,
    clean_dxf_zip,
    fmt_eur,
    quote_pdf,
    quote_totals,
    quote_xlsx,
)
from core.sheet_cost import piece_costs
from view.work import current_rates

_SIZES_KEY = "quote_batch_sizes"
_STARTED = "quote_started"            # when the first part could be priced
_BATCHES, _ORDER = "Eräkoot", "Tilausmäärät"   # the prices' two views
_FIELDS = {                           # QuoteInfo field -> widget key
    "number": "quote_number", "customer": "quote_customer",
    "customer_address": "quote_customer_address", "contact": "quote_contact",
    "seller": "quote_seller", "company": "quote_company",
    "company_address": "quote_company_address", "payment_terms": "quote_payment",
    "delivery": "quote_delivery", "notes": "quote_notes",
}


# ── Header ────────────────────────────────────────────────────────────────────

def render_header() -> None:
    """The quote's header, folded above the tabs. Both tabs read it back with
    ``current_info`` and ``current_sizes``."""
    d = QuoteInfo()
    sizes = current_sizes()
    title = "Tarjous" + (f" {st.session_state.get('quote_number')}"
                         if st.session_state.get("quote_number") else "")
    with st.expander(f"{title} · eräkoot {' · '.join(map(str, sizes))} kpl"):
        c = st.columns(4)
        c[0].text_input("Tarjousnumero", key=_FIELDS["number"])
        c[1].text_input("Asiakas", key=_FIELDS["customer"])
        c[2].text_input("Yhteyshenkilö", key=_FIELDS["contact"])
        c[3].text_input("Myyjä", key=_FIELDS["seller"])
        c = st.columns(4)
        c[0].text_input("Asiakkaan osoite", key=_FIELDS["customer_address"])
        c[1].date_input("Tarjouspäivä", value=date.today(), key="quote_date",
                        format="DD.MM.YYYY")
        c[2].number_input("Voimassa (pv)", min_value=1, value=d.valid_days, step=1,
                          key="quote_valid_days")
        c[3].text_input("Eräkoot (kpl)", value=" ".join(map(str, DEFAULT_BATCH_SIZES)),
                        key=_SIZES_KEY,
                        help="Jokainen osa hinnoitellaan näillä kappalemäärillä, esim. "
                             "\"1 5 10\". Osakortin määrä on se, jolla levyt nestataan; "
                             "muiden eräkokojen materiaali arvioidaan siitä.")
        c = st.columns(4)
        c[0].text_input("Maksuehto", value=d.payment_terms, key=_FIELDS["payment_terms"])
        c[1].text_input("Toimitus", value=d.delivery, key=_FIELDS["delivery"])
        c[2].text_input("Yritys", value=d.company, key=_FIELDS["company"])
        c[3].text_input("Yrityksen osoite", key=_FIELDS["company_address"])
        st.text_area("Lisätiedot tarjoukseen", key=_FIELDS["notes"], height=68)


def current_sizes() -> list[int]:
    """The batch sizes typed in the header (the defaults before it is drawn,
    or when nothing usable is typed)."""
    return (parse_batch_sizes(st.session_state.get(_SIZES_KEY))
            or list(DEFAULT_BATCH_SIZES))


def current_info() -> QuoteInfo:
    ss = st.session_state
    d = QuoteInfo()
    return QuoteInfo(
        **{f: str(ss.get(k, getattr(d, f)) or "") for f, k in _FIELDS.items()},
        quote_date=ss.get("quote_date") or date.today(),
        valid_days=int(ss.get("quote_valid_days", d.valid_days)),
    )


# ── Prices per batch size ─────────────────────────────────────────────────────

def render_batch_prices(products: list[dict], prices: dict[str, float],
                        groups: dict[tuple, list[dict]], sheets: dict[tuple, int],
                        totals: dict[tuple, float], *, lead: str, key: str,
                        areas_mm2: dict[str, float] | None = None,
                        clean_parts: list[CleanPart] | None = None,
                        assemblies: list[Assembly] | None = None,
                        table: bool = True) -> None:
    """The price of every part at every batch size, the quote's total at each,
    and the downloads; or, switched to "Tilausmäärät", every part at its own
    quantity (see ``_render_order``). ``clean_parts`` (DXF tab) adds the clean-DXF ZIP.
    Without ``table``, the breakdown at one batch size replaces the table of
    every size, open.
    A part in one of the ``assemblies`` is priced inside it: the assembly is
    the quote line, and a batch size counts assemblies."""
    costs = piece_costs(products, prices, areas_mm2)
    group_of = {p["id"]: k for k, prods in groups.items() for p in prods}
    costs = [pc for pc in costs if pc.product["id"] in group_of]
    if not costs:
        return
    st.session_state.setdefault(_STARTED, time.time())

    group_sheets = {}
    for k, prods in groups.items():
        if k in totals and k in sheets:
            kg = sum(pc.kg * pc.product["qty"] for pc in costs
                     if group_of[pc.product["id"]] == k)
            group_sheets[k] = GroupSheets(totals[k], sheets[k], kg)
    assemblies = [a for a in assemblies or [] if a.members or a.bought]
    in_assembly = per_unit(assemblies)
    parts = [BatchPart(pc.product["id"], group_of[pc.product["id"]], pc.kg,
                       pc.product.get("work"), in_assembly.get(pc.product["id"], 1))
             for pc in costs]
    rates = current_rates()
    names = {pc.product["id"]: str(pc.product.get("name", pc.index + 1)) for pc in costs}

    if table:
        st.divider()
    mode = st.segmented_control(
        "Hinnoittele", (_BATCHES, _ORDER), default=_BATCHES, key=f"quote_mode_{key}",
        help="Eräkoot: jokainen nimike sivun yläosan eräkoilla (1, 5, 10 …). "
             "Tilausmäärät: jokainen osa omalla määrällään (osakortin Määrä: BOM:sta "
             "tai itse kirjoitettu), juuri niin kuin levyt nestattiin.") or _BATCHES
    if mode == _ORDER:
        lines, totals, labels = _render_order(costs, names, parts, group_sheets,
                                              assemblies, rates)
    else:
        lines, totals, labels = _render_batches(costs, names, parts, group_sheets,
                                                assemblies, rates, lead=lead, key=key,
                                                table=table)

    info = current_info()
    stem = f"tarjous_{info.number or 'uusi'}".replace(" ", "_")
    qty_label = "Määrä" if mode == _ORDER else "Eräkoko"
    c1, c2, c3 = st.columns([2, 2, 3])
    show_material = c3.checkbox("Näytä materiaali PDF:ssä", value=True,
                                key=f"quote_pdf_mat_{key}")
    pdf = _file(lambda: quote_pdf(info, lines, labels, totals, show_material=show_material,
                                  qty_label=qty_label), "reportlab", c1)
    if pdf is not None:
        c1.download_button("Lataa tarjous-PDF", type="primary", key=f"quote_pdf_{key}",
                           data=pdf, file_name=f"{stem}.pdf", mime="application/pdf")
    xlsx = _file(lambda: quote_xlsx(info, lines, qty_label=qty_label), "openpyxl", c2)
    if xlsx is not None:
        c2.download_button("Excel-vienti", key=f"quote_xlsx_{key}",
                           data=xlsx, file_name=f"{stem}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")
    if clean_parts:
        _render_clean_dxfs(clean_parts, key)


def _contents(a: Assembly, names: dict[str, str]) -> tuple[str, ...]:
    """What goes into an assembly, for its quote line: "Pohja × 1", …"""
    return tuple([f"{names.get(pid, '?')} × {n}" for pid, n in a.members]
                 + [f"{b.name} × {b.qty}" for b in a.bought]) or ("—",)


def _render_started(col) -> None:
    started = st.session_state.get(_STARTED)
    if started:
        col.metric("Laskenta-aika", f"{(time.time() - started) / 60:.0f} min",
                   help="Aika siitä, kun ensimmäinen osa oli hinnoiteltavissa.")


def _render_batches(costs, names: dict[str, str], parts: list[BatchPart],
                    group_sheets: dict[tuple, GroupSheets], assemblies: list[Assembly],
                    rates, *, lead: str, key: str, table: bool):
    """Every quote line at every batch size. Returns what the downloads take:
    the lines, the quote's total at each size and their labels."""
    sizes = current_sizes()
    by_part = batch_prices(parts, group_sheets, sizes, rates)
    in_assembly = per_unit(assemblies)
    lines = [QuoteLine(names[pc.product["id"]], pc.product["material"] or "",
                       pc.product["thickness"] or "", tuple(by_part[pc.product["id"]]))
             for pc in costs if pc.product["id"] not in in_assembly]
    members: list[list[dict]] = []      # each assembly's rows in the table
    priced: list[tuple[Assembly, dict, tuple]] = []     # for the breakdown
    for a in assemblies:
        units = {pid: [p.unit for p in by_part[pid]] for pid, _ in a.members if pid in by_part}
        lines.append(QuoteLine(a.name, prices=tuple(assembly_prices(a, units, sizes, rates)),
                               parts=_contents(a, names)))
        prices = lines[-1].prices
        priced.append((a, units, prices))
        # Each row is what it adds to one assembly (kpl/kok. × €/kpl), so the
        # rows add up to the assembly's €/kpl; its quantity is per batch.
        members.append(
            [{"name": f"\u2003└ {names.get(pid, '?')} × {n}", "per": n,
              "units": [None if u is None else u * n
                        for u in units.get(pid, [None] * len(sizes))]}
             for pid, n in a.members]
            + [{"name": f"\u2003└ {b.name} × {b.qty} (ostettu)", "per": b.qty,
                "units": [b.unit * b.qty] * len(sizes)} for b in a.bought]
            + ([{"name": "\u2003└ Kokoonpanotyö", "per": None,
                 "units": [p.work for p in prices]}] if any(p.work for p in prices) else []))
    line_totals = quote_totals(lines, len(sizes))

    st.markdown("**Eräkohtaiset hinnat**", help=(
        "Kpl-hinta jokaisella eräkoolla: materiaali + työ + erän kiinteät kulut "
        "jaettuna eräkoolla. Materiaali: ryhmän levyt nestattuna osakorttien "
        "määrillä; muilla eräkoilla tarvittavat kokonaiset levyt arvioidaan samalla "
        "hukalla. Eräkoot muutetaan sivun yläosan Tarjous-kohdasta."
        + (" 🧩 Kokoonpanon └-rivit ovat sen osuuksia yhdestä kokoonpanosta "
           "(kpl / kokoonpano × €/kpl), ja niiden summa on kokoonpanon kpl-hinta."
           if assemblies and table else "")))
    cols = st.columns(len(sizes) + 1)
    for col, q, t in zip(cols, sizes, line_totals):
        col.metric(f"Yhteensä, {q} kpl/nimike", fmt_eur(t) or "–",
                   help=None if t is not None else "Osalta puuttuu hinta.")
    _render_started(cols[-1])
    if table:
        _render_table(lines, members, sizes, lead)
    _render_breakdown(costs, names, by_part, priced, sizes, rates, key, folded=table)
    return lines, line_totals, [f"{q} kpl / nimike" for q in sizes]


def _assemblies_ordered(a: Assembly, qtys: dict[str, int]) -> int:
    """How many of an assembly its parts' quantities make (the fewest any
    part allows); one with no parts of its own counts the largest batch size."""
    counts = [qtys[pid] // int(n) for pid, n in a.members if pid in qtys and int(n) > 0]
    return max(min(counts), 1) if counts else max(current_sizes())


def _render_order(costs, names: dict[str, str], parts: list[BatchPart],
                  group_sheets: dict[tuple, GroupSheets], assemblies: list[Assembly],
                  rates):
    """Every part at its own quantity (``core.batches.order_prices``) and each
    assembly at as many as its parts make. Returns what the downloads take:
    the lines, the quote's total and its label."""
    qtys = {pc.product["id"]: max(int(pc.product["qty"]), 1) for pc in costs}
    by_id = order_prices(parts, group_sheets, qtys, rates)
    in_assembly = per_unit(assemblies)
    lines = [QuoteLine(names[pc.product["id"]], pc.product["material"] or "",
                       pc.product["thickness"] or "", (by_id[pc.product["id"]],))
             for pc in costs if pc.product["id"] not in in_assembly]
    priced: list[tuple[Assembly, dict, tuple]] = []
    for a in assemblies:
        units = {pid: [by_id[pid].unit] for pid, _ in a.members if pid in by_id}
        prices = tuple(assembly_prices(a, units, [_assemblies_ordered(a, qtys)], rates))
        lines.append(QuoteLine(a.name, prices=prices, parts=_contents(a, names)))
        priced.append((a, units, prices))
    total = quote_totals(lines, 1)[0]
    bps = [by_id[pc.product["id"]] for pc in costs]
    material = (None if any(bp.material is None for bp in bps)
                else sum(bp.material * bp.qty for bp in bps))

    st.markdown("**Hinnat tilausmäärillä**", help=(
        "Jokainen osa omalla määrällään (osakortin Määrä: BOM:sta tai itse "
        "kirjoitettu), samoilla määrillä kuin levyt nestattiin. Ryhmän levyjen "
        "hinta hukkineen jaetaan osille sen mukaan, kuinka paljon kukin osa vie "
        "levyistä (kappaleiden paino eli saman materiaalin ja paksuuden ryhmässä "
        "pinta-ala, reiät pois). Osien materiaali on siksi yhteensä ryhmän "
        "levyjen hinta. Kokoonpanoja on niin monta kuin sen osien määrät riittävät."))
    cols = st.columns(3)
    cols[0].metric("Yhteensä", fmt_eur(total) or "–",
                   help=None if total is not None else "Osalta puuttuu hinta.")
    cols[1].metric("Josta materiaali", fmt_eur(material) or "–",
                   help="Kaikkien ryhmien levyt hukkineen, kate mukana.")
    _render_started(cols[2])

    rows = []
    for pc, bp in zip(costs, bps):
        part = next(p for p in parts if p.id == pc.product["id"])
        group = group_sheets.get(part.group)
        w = bp.work
        row = {"Osa": names[pc.product["id"]], "Määrä": bp.qty, "Levyjä (ryhmä)": bp.sheets,
               "Osuus levyistä": None if not group or not group.pieces_kg else
               f"{part.kg * bp.qty / group.pieces_kg * 100:.1f} %",
               "Materiaali €/kpl": None if bp.material is None else round(bp.material, 2),
               "Materiaali yht. €": None if bp.material is None
               else round(bp.material * bp.qty, 2)}
        if w is not None:
            row.update({
                **{phase: round(eur, 2) for phase, eur in w.breakdown().items()},
                "Erän kulut €": round(w.batch_fixed, 2),
                "Erän kulut / kpl": round(w.batch_fixed / w.qty, 2)})
        row["Kpl-hinta €"] = None if bp.unit is None else round(bp.unit, 2)
        row["Yhteensä €"] = None if bp.total is None else round(bp.total, 2)
        rows.append(row)
    st.dataframe(rows, width="stretch", hide_index=True)
    for a, units, prices in priced:
        _render_assembly_steps(a, units, prices[0], 0, names, rates)
    return lines, [total], ["Tilausmäärillä"]


def _file(make, package: str, slot) -> bytes | None:
    """A download's bytes; None, with a note in ``slot``, when the package
    that makes it isn't installed (the prices still show)."""
    try:
        return make()
    except ModuleNotFoundError:
        slot.caption(f":orange[Asenna {package}: `pip install -r requirements.txt`]")
        return None


def _render_clean_dxfs(parts: list[CleanPart], key: str) -> None:
    with st.expander(f"Lataa puhtaat DXF:t tuotantoon ({len(parts)} osaa)"):
        c1, c2 = st.columns([3, 2])
        template = c1.text_input(
            "Tiedostonimi", value=DEFAULT_DXF_NAME, key=f"quote_dxf_name_{key}",
            help="Kentät: {part} osan nimi, {material}, {thickness}, {index} järjestysnumero.")
        markings = c2.checkbox("Sisällytä merkinnät (taivutuslinjat ym.)",
                               key=f"quote_dxf_marks_{key}",
                               help="Merkinnät tasolle MARKING; niitä ei leikata.")
        st.download_button("Lataa ZIP", key=f"quote_dxf_zip_{key}",
                           help="Jokaisesta osasta vain leikattava geometria (ulkoreuna, "
                                "reiät ja avoimet leikkaukset) tasolla CUT, millimetreinä. "
                                "Kaaret ovat lyhyinä suorina paloina (enintään 0,2 mm "
                                "poikkeama).",
                           data=clean_dxf_zip(parts, template, markings=markings),
                           file_name="puhtaat_dxf.zip", mime="application/zip")


def _eur(v: float | None) -> str:
    return "–" if v is None else f"{v:,.2f}"


def _render_table(lines: list[QuoteLine], members: list[list[dict]], sizes: list[int],
                  lead: str) -> None:
    """Every quote line's €/kpl at every batch size, each assembly's rows
    under it."""
    def qty(per: int | None) -> str:
        return "" if per is None else " · ".join(str(q * per) for q in sizes)

    rows = []
    sub = iter(members)
    for line in lines:
        row = {lead: ("🧩 " if line.is_assembly else "") + line.name, "Määrä": qty(1),
               "Materiaali": line.material, "Paksuus": line.thickness}
        for q, p in zip(sizes, line.prices):
            row[f"{q} kpl, €/kpl"] = None if p.unit is None else round(p.unit, 2)
        rows.append(row)
        for m in (next(sub) if line.is_assembly else []):
            rows.append({lead: m["name"], "Määrä": qty(m["per"]), "Materiaali": "",
                         "Paksuus": "",
                         **{f"{q} kpl, €/kpl": None if u is None else round(u, 2)
                            for q, u in zip(sizes, m["units"])}})
    st.dataframe(rows, width="stretch", hide_index=True)


def _render_breakdown(costs, names: dict[str, str], by_part: dict[str, list],
                      priced: list[tuple[Assembly, dict, tuple]], sizes: list[int],
                      rates, key: str, *, folded: bool = True) -> None:
    """At one batch size: each part's material and work per piece, and how
    each assembly's price adds up (``folded``: behind an expander)."""
    with st.expander("Näytä laskennan erittely") if folded else nullcontext():
        q = st.segmented_control("Eräkoko", sizes, default=sizes[0],
                                 format_func=lambda n: f"{n} kpl",
                                 key=f"quote_breakdown_q_{key}") or sizes[0]
        i = sizes.index(q)
        st.markdown(f"**Työt ja materiaali, eräkoko {q}**", help=(
            "€/kpl kullekin osalle. Kappaleet erässä = eräkoko × kpl / kokoonpano. "
            "Leikkaus sisältää levyn minimiveloituksen. Asetus ja "
            "laskurien asetus ovat €/erä, ja ne jaetaan erän kappaleille."))
        rows = []
        for pc in costs:
            pid = pc.product["id"]
            bp = by_part[pid][i]
            w = bp.work
            row = {"Osa": names[pid], "Kpl erässä": bp.qty, "Levyjä": bp.sheets,
                   "Materiaali": None if bp.material is None else round(bp.material, 2)}
            if w is not None:
                row.update({
                    **{phase: round(eur, 2) for phase, eur in w.breakdown().items()},
                    "Erän kulut €": round(w.batch_fixed, 2),
                    "Erän kulut / kpl": round(w.batch_fixed / w.qty, 2)})
            row["Kpl-hinta €"] = None if bp.unit is None else round(bp.unit, 2)
            rows.append(row)
        st.dataframe(rows, width="stretch", hide_index=True)

        for a, units, prices in priced:
            _render_assembly_steps(a, units, prices[i], i, names, rates)


def _render_assembly_steps(a: Assembly, units: dict, p, i: int, names: dict[str, str],
                           rates) -> None:
    """How an assembly's price ``p`` (at ``p.qty`` assemblies) adds up; its
    parts' €/kpl are ``units[pid][i]``."""
    q = p.qty
    w = a.work
    parts = " + ".join(
        f"{names.get(pid, '?')} {n} × {_eur(units[pid][i] if pid in units else None)}"
        for pid, n in a.members) or "—"
    bought = " + ".join(f"{b.name} {b.qty} × ({b.eur:,.2f} + {b.fit_eur:,.2f})"
                        for b in a.bought) or "—"
    steps = [
        ("1. Osat (kpl / kok. × €/kpl)", parts, _eur(p.parts)),
        ("2. Ostetut osat (kpl × (€/kpl + asennus))", bought, _eur(p.bought)),
        ("3. Kokoonpanotyö",
         f"({w.setup_min:g} min / {q} + {w.fit_min:g} min) / 60 × {w.eur_per_h:g} €/h",
         _eur(fit_eur(w, q))),
        ("4. Hitsaus",
         f"{w.weld_m:g} m / {rates.weld_m_per_h:g} m/h × {rates.weld_eur_per_h:g} €/h",
         _eur(rates.weld_eur(w.weld_m))),
        ("5. Muut", "per kokoonpano", _eur(w.other_eur)),
        ("6. Kokoonpanon kpl-hinta", "1 + 2 + 3 + 4 + 5", _eur(p.unit)),
        ("7. Yhteensä", f"{_eur(p.unit)} × {q} kok.", _eur(p.total)),
    ]
    st.markdown(f"**🧩 {a.name}, {q} kpl**")
    st.dataframe([{"Vaihe": v, "Laskenta": c, "Arvo €": val} for v, c, val in steps],
                 width="stretch", hide_index=True)
