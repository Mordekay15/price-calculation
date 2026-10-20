"""The quote in the UI: the "Tarjous" header above the tabs (customer, quote
number, terms and the batch sizes), and under each tab's results the price
per batch size with the downloads: the quote PDF, the Excel export and, on the
DXF tab, the clean DXFs for production (see ``core.batches``, ``core.quote``)."""

from __future__ import annotations

import time
from datetime import date

import streamlit as st

from core.assemblies import Assembly, assembly_prices, per_unit
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
    quote_xlsx,
)
from core.sheet_cost import piece_costs
from view.work import current_rates

_SIZES_KEY = "quote_batch_sizes"
_STARTED = "quote_started"            # when the first part could be priced
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
                        assemblies: list[Assembly] | None = None) -> None:
    """The price of every part at every batch size, the quote's total at each,
    and the downloads. ``clean_parts`` (DXF tab) adds the clean-DXF ZIP.
    A part in one of the ``assemblies`` is priced inside it: the assembly is
    the quote line, and a batch size counts assemblies."""
    costs = piece_costs(products, prices, areas_mm2)
    group_of = {p["id"]: k for k, prods in groups.items() for p in prods}
    costs = [pc for pc in costs if pc.product["id"] in group_of]
    if not costs:
        return
    st.session_state.setdefault(_STARTED, time.time())

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
    for a in assemblies:
        units = {pid: [p.unit for p in by_part[pid]] for pid, _ in a.members if pid in by_part}
        contents = tuple([f"{names.get(pid, '?')} × {n}" for pid, n in a.members]
                         + [f"{b.name} × {b.qty}" for b in a.bought])
        lines.append(QuoteLine(a.name, prices=tuple(assembly_prices(a, units, sizes, rates)),
                               parts=contents or ("—",)))
        members.append(
            [{"name": f"\u2003└ {names.get(pid, '?')} × {n}",
              "units": units.get(pid, [None] * len(sizes))} for pid, n in a.members]
            + [{"name": f"\u2003└ {b.name} × {b.qty} (ostettu)",
                "units": [b.eur] * len(sizes)} for b in a.bought])
    quote_totals = [None if any(line.prices[i].total is None for line in lines)
                    else sum(line.prices[i].total for line in lines)
                    for i in range(len(sizes))]

    st.divider()
    st.markdown("**Eräkohtaiset hinnat**", help=(
        "Kpl-hinta jokaisella eräkoolla: materiaali + työ + erän kiinteät kulut "
        "jaettuna eräkoolla. Materiaali: ryhmän levyt nestattuna osakorttien "
        "määrillä; muilla eräkoilla tarvittavat kokonaiset levyt arvioidaan samalla "
        "hukalla. Eräkoot muutetaan sivun yläosan Tarjous-kohdasta."))
    cols = st.columns(len(sizes) + 1)
    for col, q, t in zip(cols, sizes, quote_totals):
        col.metric(f"Yhteensä, {q} kpl/nimike", fmt_eur(t) or "–",
                   help=None if t is not None else "Osalta puuttuu hinta.")
    started = st.session_state.get(_STARTED)
    if started:
        cols[-1].metric("Laskenta-aika", f"{(time.time() - started) / 60:.0f} min",
                        help="Aika siitä, kun ensimmäinen osa oli hinnoiteltavissa.")
    rows = []
    sub = iter(members)
    for line in lines:
        row = {lead: ("🧩 " if line.is_assembly else "") + line.name,
               "Materiaali": line.material, "Paksuus": line.thickness}
        for q, p in zip(sizes, line.prices):
            row[f"{q} kpl, €/kpl"] = "" if p.unit is None else round(p.unit, 2)
        rows.append(row)
        for m in (next(sub) if line.is_assembly else []):
            rows.append({lead: m["name"], "Materiaali": "", "Paksuus": "",
                         **{f"{q} kpl, €/kpl": "" if u is None else round(u, 2)
                            for q, u in zip(sizes, m["units"])}})
    st.dataframe(rows, width="stretch", hide_index=True)

    info = current_info()
    stem = f"tarjous_{info.number or 'uusi'}".replace(" ", "_")
    c1, c2, c3 = st.columns([2, 2, 3])
    show_material = c3.checkbox("Näytä materiaali PDF:ssä", value=True,
                                key=f"quote_pdf_mat_{key}")
    pdf = _file(lambda: quote_pdf(info, lines, sizes, quote_totals,
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
    if clean_parts:
        _render_clean_dxfs(clean_parts, key)


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
        st.caption("Jokaisesta osasta vain leikattava geometria (ulkoreuna, reiät ja "
                   "avoimet leikkaukset) tasolla CUT, millimetreinä. Kaaret ovat "
                   "lyhyinä suorina paloina (enintään 0,2 mm poikkeama).")
        c1, c2 = st.columns([3, 2])
        template = c1.text_input(
            "Tiedostonimi", value=DEFAULT_DXF_NAME, key=f"quote_dxf_name_{key}",
            help="Kentät: {part} osan nimi, {material}, {thickness}, {index} järjestysnumero.")
        markings = c2.checkbox("Sisällytä merkinnät (taivutuslinjat ym.)",
                               key=f"quote_dxf_marks_{key}",
                               help="Merkinnät tasolle MARKING; niitä ei leikata.")
        st.download_button("Lataa ZIP", key=f"quote_dxf_zip_{key}",
                           data=clean_dxf_zip(parts, template, markings=markings),
                           file_name="puhtaat_dxf.zip", mime="application/zip")
