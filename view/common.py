"""Page pieces shared by the manual tab and the DXF tab: the material / thickness
pickers, margin and nesting inputs, the per-group loop, and the pieces summary
with the page's total. Every widget takes a key from the caller, because Streamlit
renders both tabs on every run."""

from dataclasses import dataclass

from core.pricing import (
    COPPER_MATERIAL,
    COPPER_THICKNESSES,
    get_materials,
    get_thicknesses_for_material,
)
from core.cutting_stock import have_solver
from core.sheet_cost import EdgeGaps, piece_costs
from core.work import WorkCost
from view.drawing import edge_gaps_svg
from view.ui import st


def materials_with_copper(lookup: dict) -> list[str]:
    """Sorted material names; copper is always selectable, priced or not."""
    materials = get_materials(lookup)
    if COPPER_MATERIAL not in materials:
        materials = sorted([*materials, COPPER_MATERIAL])
    return materials


def render_page_picker(n: int, per_page: int, *, key: str, label: str, prefix: str = "",
                       suffix: str = "", help: str | None = None) -> range:
    """The rows of a long list drawn now: all of ``n`` when they fit one page
    of ``per_page``, else the page picked, labelled ``{prefix}1–20{suffix}``."""
    pages = -(-n // per_page)
    if pages <= 1:
        return range(n)
    if st.session_state.get(key, 0) >= pages:
        st.session_state[key] = 0
    page = st.selectbox(
        label, range(pages), key=key, help=help,
        format_func=lambda i: f"{prefix}{i * per_page + 1}–{min(n, (i + 1) * per_page)}{suffix}")
    return range(page * per_page, min(n, (page + 1) * per_page))


# ── Shared inputs ─────────────────────────────────────────────────────────────

_PLACEHOLDER_MAT   = "— Valitse materiaali —"
_PLACEHOLDER_THICK = "— Valitse paksuus —"


def thicknesses_for(lookup: dict, material: str | None) -> list[str]:
    """Thicknesses available for ``material`` (empty if none is picked)."""
    if material == COPPER_MATERIAL:
        # Copper's thicknesses are fixed and available even with no price.
        return COPPER_THICKNESSES
    if material is not None:
        return get_thicknesses_for_material(lookup, material)
    return []


def render_material_thickness(
    materials: list[str],
    lookup: dict,
    *,
    mat_key: str,
    thick_key: str,
    mat_default: str | None = None,
    thick_default: str | None = None,
    cols=None,
    help: str | None = None,
) -> tuple[str | None, str | None]:
    """Render the shared material + thickness selectboxes.

    Used by both the manual calculator product cards and the DXF part cards so
    the two pages pick material/thickness identically (copper always
    selectable, the thickness box disabled until a material is chosen).
    ``mat_default`` / ``thick_default`` seed the initial selection — pass a
    card's stored values to keep its choice across reruns, or leave them None
    to start on the placeholder. ``cols`` are the two containers to draw the
    boxes in (default: two new side-by-side columns); ``help`` is the
    material box's tooltip. Returns
    ``(material, thickness)``, each None when unset.
    """
    mat_col, thick_col = cols or st.columns(2)
    mat_opts = [_PLACEHOLDER_MAT] + materials
    mat_default = mat_default if mat_default in materials else _PLACEHOLDER_MAT
    mat_raw = mat_col.selectbox(
        "Materiaali",
        mat_opts,
        index=mat_opts.index(mat_default),
        key=mat_key,
        help=help,
    )
    material = mat_raw if mat_raw != _PLACEHOLDER_MAT else None

    thicknesses = thicknesses_for(lookup, material)
    if thicknesses:
        th_opts = [_PLACEHOLDER_THICK] + thicknesses
        th_default = thick_default if thick_default in thicknesses else _PLACEHOLDER_THICK
        th_raw = thick_col.selectbox(
            "Paksuus (mm)",
            th_opts,
            index=th_opts.index(th_default),
            key=thick_key,
        )
        thickness = th_raw if th_raw != _PLACEHOLDER_THICK else None
    else:
        thick_col.selectbox(
            "Paksuus (mm)", [_PLACEHOLDER_THICK], index=0,
            disabled=True, key=f"{thick_key}_disabled",
        )
        thickness = None

    return material, thickness


_ADVANCED_LABEL = "Lisäasetukset"

_BOTTOM_HELP = (
    "Kynsiraina — pitkän sivun kaista, johon koneen kynnet tarttuvat — "
    "kuuluu tähän: anna koko kaistan leveys, jolle ei sijoiteta osia."
)

# The four edges in EdgeGaps order: (name in summaries, input label, help).
_EDGES = (("ylä", "Yläreuna (mm)", None), ("ala", "Alareuna (mm)", _BOTTOM_HELP),
          ("vasen", "Vasen reuna (mm)", None), ("oikea", "Oikea reuna (mm)", None))


def _edges_label(edges: tuple) -> str:
    """``reunavara 10 mm`` when every edge is the same, else each edge."""
    if len(set(edges)) == 1:
        return f"reunavara {edges[0]} mm"
    return "reunavarat " + ", ".join(
        f"{name} {v}" for (name, _, _), v in zip(_EDGES, edges)) + " mm"


# How each nesting setting reads in a one-line summary, e.g. "rankaväli 2 mm".
NESTING_LABELS = {
    "rankavali_mm": lambda v: f"rankaväli {v} mm",
    "edges_mm":     _edges_label,
}


@dataclass(frozen=True)
class SheetSettings:
    """The sheet inputs both tabs share: the cut gap and the four edge gaps
    (``edges_mm`` = top, bottom, left, right; the bottom one includes the
    clamp strip)."""

    rankavali_mm: int = 0
    edges_mm: tuple[int, int, int, int] = (0, 0, 0, 0)

    def gaps(self) -> EdgeGaps:
        """The unusable strip along each edge, as the packers take it."""
        return EdgeGaps(*self.edges_mm)

    def values(self) -> dict:
        """The settings keyed as in ``NESTING_LABELS``."""
        return {"rankavali_mm": self.rankavali_mm, "edges_mm": self.edges_mm}


_NEST_HELP = (
    "Yhdistettynä saman materiaalin ja paksuuden tuotteet sijoitellaan "
    "samoille levyille (sekanestaus). Erikseen-vaihtoehdolla kullekin "
    "tuotteelle lasketaan oma levytarpeensa."
)
_RANKAVALI_HELP = (
    "Kappaleiden välinen rankaväli (leikkausvara): vierekkäiset kappaleet "
    "pysyvät tämän etäisyyden päässä toisistaan. Levyn reunaan rankaväliä ei "
    "jätetä — kappale voi ulottua reunaan asti. Reunoille jätettävä kaista "
    "annetaan reunavaroilla."
)
_EDGES_HELP = (
    "Kaistat levyn reunoilla, joille ei sijoiteta osia. Levy on sijoittelu"
    "kuvissa pitkä sivu vaakasuorassa: ylä- ja alareuna ovat pitkät sivut, "
    "vasen ja oikea reuna lyhyet. Kynsiraina annetaan alareunaan."
)


def _render_main_settings(*, key_prefix: str, separate_label: str) -> tuple[float, str]:
    """The settings every quote touches, in one row: margin and "Sijoittelutapa".

    Returns ``(margin_pct, nest_mode)``; ``nest_mode`` is "combined" or
    "separate".
    """
    c1, c2 = st.columns([1, 3])
    margin_pct = c1.number_input(
        "Materiaalin kate (%)", min_value=0.0, value=15.0, step=0.5,
        key=f"{key_prefix}_margin_pct",
    )
    nest_mode = c2.radio(
        "Sijoittelutapa",
        options=("combined", "separate"),
        format_func=lambda v: {
            "combined": "Yhdistä samat materiaalit samalle levylle",
            "separate": separate_label,
        }[v],
        horizontal=True,
        key=f"{key_prefix}_nest_mode",
        help=_NEST_HELP,
    )
    return margin_pct, nest_mode


def render_settings(*, key_prefix: str, separate_label: str) -> tuple[float, str, SheetSettings]:
    """The settings row (margin, nesting mode), then the folded sheet settings
    and their one-line summary: ``(margin_pct, nest_mode, sheet)``."""
    margin_pct, nest_mode = _render_main_settings(key_prefix=key_prefix,
                                                  separate_label=separate_label)
    with st.expander(_ADVANCED_LABEL):
        sheet = _render_nesting_inputs(key_prefix=key_prefix)
    st.caption(" · ".join(label(sheet.values()[k]) for k, label in NESTING_LABELS.items()))
    return margin_pct, nest_mode, sheet


def _render_nesting_inputs(*, key_prefix: str) -> SheetSettings:
    """Rankaväli and the four edge gaps, with a small sheet diagram that
    shows which edge is which."""
    rankavali_mm = int(st.columns(2)[0].number_input(
        "Rankaväli (mm)", min_value=0, value=0, step=1,
        key=f"{key_prefix}_rankavali_mm", help=_RANKAVALI_HELP,
    ))

    st.markdown("**Levyn reunavarat**", help=_EDGES_HELP)
    diagram, inputs = st.columns([2, 3])
    with inputs:
        rows = (st.columns(2), st.columns(2))
    edges = tuple(
        int(col.number_input(label, min_value=0, value=0, step=1,
                             key=f"{key_prefix}_edge_{name}", help=help_text))
        for col, (name, label, help_text) in zip((*rows[0], *rows[1]), _EDGES)
    )
    diagram.markdown(edge_gaps_svg(edges), unsafe_allow_html=True)
    return SheetSettings(rankavali_mm, edges)


# ── The per-group loop ────────────────────────────────────────────────────────

def render_groups(groups: dict[tuple, list[dict]], render_one):
    """Render every group and collect the prices for the pieces summary.

    ``render_one(key, products)`` renders one group and returns
    ``(total_eur, price_per_tonne, sheets)`` — the first two may be None when
    unpriced — or None when the group has no result yet.

    Returns ``(price_by_product_id, grand_total_eur, missing, sheets,
    totals)``: the price each product's pieces are billed at, the summed total
    (None when no group was priced), whether any group had no result yet, the
    sheets each priced group is cut from (for the cutting minimum) and each
    priced group's material total (for the batch-size prices).
    """
    if not have_solver():
        # Without it the fewest-sheets and mixed-size plans silently vanish
        # and an old, worse plan is shown: say so.
        st.warning("Levysuunnitelmien optimointi ei ole käytössä: Python-paketti **scipy** "
                   "puuttuu. Asenna se (`pip install -r requirements.txt`) ja käynnistä "
                   "sovellus uudelleen — muuten näytetään vain levy kerrallaan täytetyt "
                   "suunnitelmat.")
    prices: dict[str, float] = {}
    sheets: dict[tuple, int] = {}
    totals: dict[tuple, float] = {}
    grand_total = None
    missing = False
    for i, (key, prods) in enumerate(groups.items()):
        if i:
            st.divider()  # one line between groups (each part, in "separate")
        out = render_one(key, prods)
        if out is None:
            missing = True
            continue
        total, ppt, n_sheets = out
        if total is not None:
            grand_total = (grand_total or 0.0) + total
            sheets[key] = n_sheets
            totals[key] = total
        if ppt is not None:
            for p in prods:
                prices[p["id"]] = ppt
    return prices, grand_total, missing, sheets, totals


# ── Pieces summary ────────────────────────────────────────────────────────────

def render_pieces_summary(
    products: list[dict],
    prices: dict[str, float],
    *,
    weight_label: str,
    lead: str,
    work: dict[str, WorkCost] | None = None,
) -> None:
    """The per-piece weight / cost table (see ``core.sheet_cost.piece_costs``),
    the piece prices with the work (``work`` by product id), and the totals.
    ``lead`` heads the first column: the product's name, or its running number
    when it has none."""
    costs = piece_costs(products, prices)
    if not costs:
        return
    work = work or {}

    def money(value, digits):
        return None if value is None else round(value, digits)

    rows = [{
        lead:             pc.product.get("name", pc.index + 1),
        "Materiaali":     pc.product["material"] or "",
        "Paksuus":        pc.product["thickness"] or "",
        "Leveys (mm)":    pc.product["width"],
        "Korkeus (mm)":   pc.product["height"],
        "Määrä (kpl)":    pc.product["qty"],
        "kg/kpl":         round(pc.kg, 3),
        "Yhteensä kg":    round(pc.batch_kg, 3),
        "€/kpl":          money(pc.eur, 4),
        "Yhteensä €":     money(pc.batch_eur, 2),
    } for pc in costs]
    total_cost_eur = sum(pc.batch_eur for pc in costs if pc.batch_eur)

    st.divider()
    m1, m2 = st.columns(2)
    if total_cost_eur:
        m1.metric("Materiaalikustannukset yhteensä (€)", f"{total_cost_eur:,.2f}")
    m2.metric(weight_label, f"{sum(pc.batch_kg for pc in costs):.3f}")
    st.dataframe(rows, width="stretch", hide_index=True)

    priced = [(pc, work[pc.product["id"]]) for pc in costs if pc.product["id"] in work]
    if priced:
        _render_piece_prices(priced, lead, total_cost_eur)


def _render_piece_prices(priced: list, lead: str, material_total: float) -> None:
    """Price per piece: material + work, the batch costs shared over the
    quantity (c + F / kpl), and the page's total."""
    def r(value):
        return round(value, 2)

    rows = []
    for pc, wc in priced:
        unit = None if pc.eur is None or not wc.priced else pc.eur + wc.per_piece
        rows.append({
            lead:                     pc.product.get("name", pc.index + 1),
            "Määrä (kpl)":            pc.product["qty"],
            "Materiaali €/kpl":       None if pc.eur is None else r(pc.eur),
            **{f"{phase} €/kpl": r(eur) if wc.priced or phase != "Leikkaus" else None
               for phase, eur in wc.breakdown().items()},
            "Asetus ym. €/kpl":       r(wc.batch_fixed / wc.qty),
            "Kpl-hinta €":            None if unit is None else r(unit),
            "Yhteensä €":             None if unit is None else r(unit * pc.product["qty"]),
        })
    work_total = sum(wc.batch for _, wc in priced if wc.priced)
    complete = all(pc.eur is not None and wc.priced for pc, wc in priced)

    st.markdown("**Kappalehinnat**", help=(
        "Kpl-hinta = materiaali + työ + erän kiinteät kulut (asetus) "
        "jaettuna kappalemäärällä. Työssä ei ole katetta. Leikkaus on "
        "vähintään sivupalkin minimi levyä kohden."))
    if any(wc.cut_minimum for _, wc in priced):
        st.caption("Leikkaukseen on lisätty levykohtainen minimiveloitus.")
    m1, m2 = st.columns(2)
    m1.metric("Työ yhteensä (€)", f"{work_total:,.2f}")
    m2.metric("Yhteensä (€, alv 0 %)", f"{material_total + work_total:,.2f}"
              + ("" if complete else " *"))
    st.dataframe(rows, width="stretch", hide_index=True)
    if not complete:
        st.caption("* Osalta puuttuu materiaalin tai laserin hinta; se ei ole summassa.")
