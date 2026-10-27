"""The quote in the UI: the bar above the tabs (the quote's name, the batch
sizes, the quote's details behind a button, and the live total at each batch
size with what it is made of), and under each tab's results the price per
batch size with the downloads: the quote PDF, the Excel export and, on the
DXF tab, the clean DXFs for production (see ``core.batches``, ``core.quote``)."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date

from core.assemblies import Assembly, assembly_prices, fit_eur, per_unit
from core.batches import (
    DEFAULT_BATCH_SIZES,
    BatchPart,
    GroupSheets,
    batch_prices,
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
from core.work import BREAKDOWN
from view.ui import is_headless, st
from view.work import current_rates

_SIZES_KEY = "quote_batch_sizes"
_FIELDS = {                           # QuoteInfo field -> widget key
    "number": "quote_number", "customer": "quote_customer",
    "customer_address": "quote_customer_address", "contact": "quote_contact",
    "seller": "quote_seller", "company": "quote_company",
    "company_address": "quote_company_address", "payment_terms": "quote_payment",
    "delivery": "quote_delivery", "notes": "quote_notes",
}


# ── Header ────────────────────────────────────────────────────────────────────

@dataclass
class Totals:
    """A tab's quote at each batch size, for the bar: the total (None where a
    line is unpriced) and what it is made of (``parts``: label -> €, one dict
    per size). ``missing``: some groups aren't nested yet."""

    sizes: list[int]
    totals: list[float | None]
    parts: list[dict[str, float]] = field(default_factory=list)
    missing: bool = False
    margin_pct: float | None = None


def render_header():
    """The bar above the tabs: the quote's name, the batch sizes and the
    quote's details behind a button. Returns the place for the totals, filled
    once the open tab has priced (``render_totals``). Both tabs read it back
    with ``current_info`` and ``current_sizes``."""
    d = QuoteInfo()
    ss = st.session_state
    bar = st.container(border=True)
    with bar:
        name, sizes, details = st.columns([5, 2, 2], vertical_alignment="bottom")
        title = " · ".join(filter(None, [
            "Tarjous " + ss["quote_number"] if ss.get("quote_number") else "Uusi tarjous",
            ss.get("quote_customer")]))
        name.markdown(f"#### {title}")
        name.caption("Stremet Price Tool")
        sizes.text_input("Eräkoot (kpl)", value=" ".join(map(str, DEFAULT_BATCH_SIZES)),
                         key=_SIZES_KEY,
                         help="Jokainen osa hinnoitellaan näillä kappalemäärillä, esim. "
                              "\"1 5 10\". Osakortin määrä on se, jolla levyt nestataan; "
                              "muiden eräkokojen materiaali arvioidaan siitä.")
        with details.popover("Tarjouksen tiedot", width="stretch"):
            c = st.columns(2)
            c[0].text_input("Tarjousnumero", key=_FIELDS["number"])
            c[1].text_input("Asiakas", key=_FIELDS["customer"])
            c[0].text_input("Yhteyshenkilö", key=_FIELDS["contact"])
            c[1].text_input("Myyjä", key=_FIELDS["seller"])
            c[0].text_input("Asiakkaan osoite", key=_FIELDS["customer_address"])
            c[1].date_input("Tarjouspäivä", value=date.today(), key="quote_date",
                            format="DD.MM.YYYY")
            c[0].number_input("Voimassa (pv)", min_value=1, value=d.valid_days, step=1,
                              key="quote_valid_days")
            c[1].text_input("Maksuehto", value=d.payment_terms, key=_FIELDS["payment_terms"])
            c[0].text_input("Toimitus", value=d.delivery, key=_FIELDS["delivery"])
            c[1].text_input("Yritys", value=d.company, key=_FIELDS["company"])
            st.text_input("Yrityksen osoite", key=_FIELDS["company_address"])
            st.text_area("Lisätiedot tarjoukseen", key=_FIELDS["notes"], height=68)
        return st.container()


def render_totals(slot, totals: Totals | None) -> None:
    """The bar's totals: the quote at each batch size, each with "Miksi tämä
    hinta?" (what it is made of)."""
    with slot:
        if totals is None or not totals.totals:
            st.caption("Hinta näkyy tässä, kun ensimmäinen osa on hinnoiteltu.")
            return
        cols = st.columns([2] * len(totals.sizes) + [3], vertical_alignment="center")
        for i, (col, q, t) in enumerate(zip(cols, totals.sizes, totals.totals)):
            col.metric(f"{q} kpl / nimike", fmt_eur(t) or "–",
                       help=None if t is not None else "Osalta puuttuu hinta.")
            if t is not None and i < len(totals.parts):
                with col.popover("Miksi tämä hinta?"):
                    _render_why(q, t, totals.parts[i], totals.margin_pct)
        notes = []
        if totals.missing:
            notes.append(":orange[Arvio: kaikkia ryhmiä ei ole vielä nestattu.]")
        if totals.margin_pct is not None:
            notes.append(f"Materiaalin kate {totals.margin_pct:g} %")
        cols[-1].caption("  \n".join(notes) or "ALV 0 %")


def _render_why(q: int, total: float, parts: dict[str, float],
                margin_pct: float | None) -> None:
    """One batch size's total, by what it is made of."""
    st.markdown(f"**Eräkoko {q}: {fmt_eur(total)}**")
    rows = [{"Mistä": k, "€": round(v, 2), "Osuus": f"{v / total * 100:.0f} %" if total else ""}
            for k, v in parts.items() if abs(v) >= 0.005]
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption("Materiaali on nestattujen levyjen hinta"
               + (f" materiaalin katteella ({margin_pct:g} %)" if margin_pct is not None else "")
               + ". Asetus on erän kiinteä kulu. Tarkempi erittely osittain: Hinnoittelu.")


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
                        table: bool = True, margin_pct: float | None = None,
                        missing: bool = False) -> Totals | None:
    """The price of every part at every batch size, the quote's total at each,
    and the downloads. ``clean_parts`` (DXF tab) adds the clean-DXF ZIP.
    Without ``table``, the breakdown at one batch size replaces the table of
    every size, open.
    A part in one of the ``assemblies`` is priced inside it: the assembly is
    the quote line, and a batch size counts assemblies. Returns the totals
    for the bar (None when nothing is priced); headless, only that, without
    making the downloads."""
    costs = piece_costs(products, prices, areas_mm2)
    group_of = {p["id"]: k for k, prods in groups.items() for p in prods}
    costs = [pc for pc in costs if pc.product["id"] in group_of]
    if not costs:
        return None

    sizes = current_sizes()
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
    by_part = batch_prices(parts, group_sheets, sizes, rates)

    def name(pc) -> str:
        return str(pc.product.get("name", pc.index + 1))

    lines = [QuoteLine(name(pc), pc.product["material"] or "", pc.product["thickness"] or "",
                       tuple(by_part[pc.product["id"]]))
             for pc in costs if pc.product["id"] not in in_assembly]
    names = {pc.product["id"]: name(pc) for pc in costs}
    members: list[list[dict]] = []      # each assembly's rows in the table
    priced: list[tuple[Assembly, dict, tuple]] = []     # for the breakdown
    for a in assemblies:
        units = {pid: [p.unit for p in by_part[pid]] for pid, _ in a.members if pid in by_part}
        contents = tuple([f"{names.get(pid, '?')} × {n}" for pid, n in a.members]
                         + [f"{b.name} × {b.qty}" for b in a.bought])
        lines.append(QuoteLine(a.name, prices=tuple(assembly_prices(a, units, sizes, rates)),
                               parts=contents or ("—",)))
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
    result = Totals(sizes, line_totals, [_made_of(by_part, i, t) for i, t in
                                         enumerate(line_totals)], missing, margin_pct)
    if is_headless():
        return result

    if table:
        st.divider()
    st.markdown("**Eräkohtaiset hinnat**", help=(
        "Kpl-hinta jokaisella eräkoolla: materiaali + työ + erän kiinteät kulut "
        "jaettuna eräkoolla. Materiaali: ryhmän levyt nestattuna osakorttien "
        "määrillä; muilla eräkoilla tarvittavat kokonaiset levyt arvioidaan samalla "
        "hukalla. Eräkoot muutetaan sivun ylälaidasta."
        + (" 🧩 Kokoonpanon └-rivit ovat sen osuuksia yhdestä kokoonpanosta "
           "(kpl / kokoonpano × €/kpl), ja niiden summa on kokoonpanon kpl-hinta."
           if assemblies and table else "")))
    st.caption("Tarjouksen summa kullakin eräkoolla on sivun ylälaidassa.")
    if table:
        _render_table(lines, members, sizes, lead)
    _render_breakdown(costs, names, by_part, priced, sizes, rates, key, folded=table)

    info = current_info()
    stem = f"tarjous_{info.number or 'uusi'}".replace(" ", "_")
    c1, c2, c3 = st.columns([2, 2, 3])
    show_material = c3.checkbox("Näytä materiaali PDF:ssä", value=True,
                                key=f"quote_pdf_mat_{key}")
    pdf = _file(lambda: quote_pdf(info, lines, sizes, line_totals,
                                  show_material=show_material), "reportlab", c1)
    if pdf is not None:
        c1.download_button("Lataa tarjous-PDF", type="primary", key=f"quote_pdf_{key}",
                           data=pdf, file_name=f"{stem}.pdf", mime="application/pdf")
    xlsx = _file(lambda: quote_xlsx(info, lines, sizes), "openpyxl", c2)
    if xlsx is not None:
        c2.download_button("Excel-vienti", key=f"quote_xlsx_{key}",
                           data=xlsx, file_name=f"{stem}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet")
    _render_customer_view(info, lines, sizes, line_totals, key)
    if clean_parts:
        _render_clean_dxfs(clean_parts, key)
    return result


def _made_of(by_part: dict[str, list], i: int, total: float | None) -> dict[str, float]:
    """What the quote's total at the ``i``-th batch size is made of: the
    material, each work phase, the batch setup and the assemblies' own work
    and bought parts (the rest)."""
    out = {"Materiaali": 0.0, **{phase: 0.0 for phase in BREAKDOWN}, "Asetus (erä)": 0.0}
    priced = 0.0
    for prices in by_part.values():
        bp = prices[i]
        if bp.material is not None:
            out["Materiaali"] += bp.material * bp.qty
        if bp.work is not None:
            for phase, eur in bp.work.breakdown().items():
                out[phase] += eur * bp.qty
            out["Asetus (erä)"] += bp.work.batch_fixed
        if bp.unit is not None:
            priced += bp.unit * bp.qty
    if total is not None and total - priced > 0.005:
        out["Kokoonpanot (työ, ostetut osat)"] = total - priced
    return out


def _render_customer_view(info: QuoteInfo, lines: list[QuoteLine], sizes: list[int],
                          totals: list[float | None], key: str) -> None:
    """The quote as the customer gets it in the PDF: the header, then each
    line's €/kpl at each batch size and the totals."""
    with st.expander("Esikatsele tarjous asiakkaan näkymänä"):
        head = " · ".join(filter(None, [
            f"Tarjous {info.number}" if info.number else "Tarjous",
            info.customer, info.quote_date.strftime("%d.%m.%Y"),
            f"voimassa {info.valid_days} pv"]))
        st.markdown(f"**{head}**")
        rows = []
        for line in lines:
            row = {"Nimike": ("🧩 " if line.is_assembly else "") + line.name,
                   "Materiaali": " ".join(filter(None, [line.material, line.thickness
                                                        and f"{line.thickness} mm"]))}
            for q, p in zip(sizes, line.prices):
                row[f"{q} kpl, €/kpl"] = None if p.unit is None else round(p.unit, 2)
            rows.append(row)
        rows.append({"Nimike": "Yhteensä € (koko erä)", "Materiaali": "",
                     **{f"{q} kpl, €/kpl": None if t is None else round(t, 2)
                        for q, t in zip(sizes, totals)}})
        st.dataframe(rows, hide_index=True, width="stretch", key=f"quote_preview_{key}")
        st.caption("Viimeinen rivi on tarjouksen summa kullakin eräkoolla (ALV 0 %). "
                   + (info.payment_terms and f"Maksuehto: {info.payment_terms}. " or "")
                   + (info.delivery and f"Toimitus: {info.delivery}." or ""))


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
            p = prices[i]
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
            st.markdown(f"**🧩 {a.name}, eräkoko {q}**")
            st.dataframe([{"Vaihe": v, "Laskenta": c, "Arvo €": val} for v, c, val in steps],
                         width="stretch", hide_index=True)
