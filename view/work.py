"""Work pricing in the UI: the shop's rates in the sidebar, the "Työvaiheet"
inputs on every part card (both tabs), and each product's work cost with the
per-sheet cutting minimum applied by group (see ``core.work``)."""

from core.pricing import parse_thickness_mm
from core.work import (
    LASER,
    PUNCH,
    LaserSpeed,
    WorkCost,
    WorkInputs,
    WorkRates,
    apply_sheet_minimum,
    cheaper_method,
    default_setup,
    laser_family,
    laser_speed,
    fastener_price,
    FASTENER_INSERT_EUR,
    work_cost,
)
from view.ui import st

WORK_LABEL = "Työvaiheet"

_RATE_KEYS = {          # WorkRates field -> sidebar widget key
    "eur_per_hit":           "work_eur_per_hit",
    "min_cut_eur_per_sheet": "work_min_cut",
    "weld_m_per_h":          "work_weld_m_per_h",
    "weld_eur_per_h":        "work_weld_eur_per_h",
}
_SETUP_KEYS = ("work_setup_1", "work_setup_2", "work_setup_3")


# ── Sidebar: the shop's rates ─────────────────────────────────────────────────

def render_rates() -> None:
    """The work rates, folded in the sidebar. Laser seconds and €/s come from
    the speed table; everything here is a selling price (no margin)."""
    d = WorkRates()
    with st.expander("Työn hinnat"):
        st.number_input("Levari, iskun hinta (€)", min_value=0.0, value=d.eur_per_hit,
                        step=0.001, format="%.3f", key=_RATE_KEYS["eur_per_hit"])
        st.number_input("Leikkaus vähintään (€/levy)", min_value=0.0,
                        value=d.min_cut_eur_per_sheet, step=1.0,
                        key=_RATE_KEYS["min_cut_eur_per_sheet"],
                        help="Jos levyn osien leikkaus (laser tai levari) maksaa vähemmän, "
                             "levyltä veloitetaan silti tämä.")
        c1, c2 = st.columns(2)
        c1.number_input("Hitsaus (m/h)", min_value=0.1, value=d.weld_m_per_h, step=0.1,
                        key=_RATE_KEYS["weld_m_per_h"])
        c2.number_input("Hitsaus (€/h)", min_value=0.0, value=d.weld_eur_per_h, step=5.0,
                        key=_RATE_KEYS["weld_eur_per_h"])
        st.markdown("Asetus (€/erä) työvaiheiden mukaan",
                    help="Asetuskustannus ei riipu kappalemäärästä. Työvaiheita ovat "
                         "leikkaus, taivutus, kierteet, hitsaus ja alihankinta.")
        for col, key, label, value in zip(st.columns(3), _SETUP_KEYS,
                                          ("1 vaihe", "2 vaihetta", "3+ vaihetta"),
                                          d.setup_eur):
            col.number_input(label, min_value=0.0, value=value, step=5.0, key=key)


def current_rates() -> WorkRates:
    """The rates set in the sidebar (the defaults before it is drawn)."""
    d = WorkRates()
    ss = st.session_state
    return WorkRates(
        **{f: float(ss.get(k, getattr(d, f))) for f, k in _RATE_KEYS.items()},
        setup_eur=tuple(float(ss.get(k, v)) for k, v in zip(_SETUP_KEYS, d.setup_eur)),
    )


# ── Part card: Työvaiheet ─────────────────────────────────────────────────────

_METHODS = {LASER: "Laser", PUNCH: "Levari"}
_AUTO_HELP = ("Harmaa arvo on laskettu automaattisesti. Kirjoita oma arvo "
              "korvataksesi sen; tyhjennä kenttä palataksesi automaattiseen.")


def _auto(col, label: str, key: str, auto: float | None, *, step: float,
          fmt: str = "%.2f", help: str | None = None) -> float | None:
    """A number input that is empty by default and then uses ``auto``; the
    placeholder shows that value in grey. Clearing it goes back to ``auto``."""
    shown = "ei tiedossa" if auto is None else f"{auto:.{fmt[2]}f}"
    help = " ".join(filter(None, (help, _AUTO_HELP)))
    value = col.number_input(label, min_value=0.0, value=None, step=step, format=fmt,
                             placeholder=shown, key=key, help=help)
    return auto if value is None else value


def render_work_inputs(key: str, *, material: str | None, thickness: str | None,
                       qty: int, cut_length_m: float, bends: int = 0,
                       threads: int = 0, fasteners: int = 0,
                       in_assembly: bool = False) -> WorkInputs:
    """The card's "Työvaiheet": cutting, bends, threads, fasteners,
    welding, subcontracting and setup. ``cut_length_m``, ``bends``,
    ``threads`` and ``fasteners`` are what the drawing gives (the defaults,
    overwritable); the laser speed comes from the table. ``key`` makes the widget keys unique per card. A card
    ``in_assembly`` has no fasteners or welding: the assembly prices them (its
    bought parts and welding), so they can't be counted twice."""
    thickness_mm = parse_thickness_mm(thickness)
    table = laser_speed(material, thickness_mm) if material else None
    rates = current_rates()

    with st.expander(WORK_LABEL):
        c = st.columns(4)
        method = c[0].selectbox("Leikkaus", list(_METHODS), format_func=_METHODS.get,
                                key=f"work_method_{key}")
        hits = 0
        laser = None
        if method == LASER:
            cut_length_m = _auto(c[1], "Leikkuupituus (m/kpl)", f"work_len_{key}",
                                 cut_length_m, step=0.1, fmt="%.3f",
                                 help="Ulkoreuna ja kaikki reiät.")
            s_per_m = _auto(c[2], "Leikkausaika (s/m)", f"work_spm_{key}",
                            table.s_per_m if table else None, step=1.0, fmt="%.1f",
                            help=_speed_help(material))
            eur_per_s = _auto(c[3], "Laser (€/s)", f"work_eps_{key}",
                              table.eur_per_s if table else None, step=0.005, fmt="%.3f")
            if s_per_m is not None and eur_per_s is not None:
                laser = LaserSpeed(s_per_m, eur_per_s)
            elif material and thickness_mm is not None:
                st.warning(f"Laserin leikkausaikaa ei ole taulukossa: "
                           f"{laser_family(material)[0]} {thickness} mm. Anna aika (s/m).")
            # Typed, the punch hits let the card compare the two methods.
            hits = int(st.columns(4)[0].number_input(
                "Levarin iskut (vertailuun)", min_value=0, value=0, step=1,
                key=f"work_hits_{key}",
                help="Jos annat iskujen määrän, kortti kertoo, olisiko levari halvempi."))
        else:
            hits = int(c[1].number_input("Iskuja (kpl/osa)", min_value=0, value=0, step=1,
                                         key=f"work_hits_{key}"))
            # The laser's price for the comparison, from the table.
            if table is not None:
                laser = LaserSpeed(table.s_per_m, table.eur_per_s)

        c = st.columns(4)
        n_bends = int(_auto(c[0], "Taivutuksia (kpl/osa)", f"work_bends_{key}", bends,
                            step=1.0, fmt="%.0f"))
        eur_per_bend = c[1].number_input("€ / taivutus", min_value=0.0, value=0.20,
                                         step=0.05, key=f"work_bend_eur_{key}")
        n_threads = int(_auto(c[2], "Kierteitä (kpl/osa)", f"work_threads_{key}", threads,
                              step=1.0, fmt="%.0f"))
        eur_per_thread = c[3].number_input("€ / kierre", min_value=0.0, value=0.20,
                                           step=0.05, key=f"work_thread_eur_{key}")

        if in_assembly:
            n_fasteners, eur_per_fastener, eur_per_insert, weld_m = 0, 0.0, 0.0, 0.0
        else:
            c = st.columns(4)
            n_fasteners = int(_auto(
                c[0], "Kiinnikkeet (kpl/osa)", f"work_fasteners_{key}", fasteners,
                step=1.0, fmt="%.0f",
                help="Puristemutterit, -ruuvit, pultit, niitit… Luetaan piirustuksen "
                     "teksteistä, esim. \"M5-PURISTEMUTTERIT X4KPL\"."))
            eur_per_fastener = _auto(c[1], "Kiinnikkeen hinta (€/kpl)",
                                     f"work_fastener_eur_{key}", fastener_price(material),
                                     step=0.05, help="Yhden kiinnikkeen hinta ilman asennusta.")
            eur_per_insert = c[2].number_input("Asennus (€/kiinnike)", min_value=0.0,
                                               value=FASTENER_INSERT_EUR, step=0.05,
                                               key=f"work_insert_eur_{key}",
                                               help="Yhden kiinnikkeen asennus (puristus).")
            weld_m = c[3].number_input("Hitsaus (m/kpl)", min_value=0.0, value=0.0, step=0.1,
                                       key=f"work_weld_{key}")

        c = st.columns(4)
        subcontract = c[0].number_input("Alihankinta (€/kpl)", min_value=0.0, value=0.0,
                                        step=0.5, key=f"work_sub_{key}",
                                        help="Esim. anodisointi tai sinkitys.")
        programming = c[1].number_input("Ohjelmointi (€/erä)", min_value=0.0, value=0.0,
                                        step=10.0, key=f"work_prog_{key}",
                                        help="Esim. uusi tuote.")
        inputs = WorkInputs(method=method, cut_length_m=cut_length_m, laser=laser, hits=hits,
                            bends=n_bends, eur_per_bend=eur_per_bend, threads=n_threads,
                            eur_per_thread=eur_per_thread, fasteners=n_fasteners,
                            eur_per_fastener=eur_per_fastener,
                            eur_per_insert=eur_per_insert, weld_m=weld_m,
                            subcontract_eur=subcontract,
                            programming_eur=programming)
        setup = _auto(c[2], "Asetus (€/erä)", f"work_setup_{key}",
                      default_setup(inputs, rates), step=5.0, fmt="%.0f",
                      help="Oletus työvaiheiden määrän mukaan (sivupalkin Työn hinnat).")
        inputs = WorkInputs(**{**inputs.__dict__, "setup_eur": setup})

    cheaper = cheaper_method(inputs, rates)
    if inputs.method == PUNCH:  # the laser was only there for the comparison
        inputs = WorkInputs(**{**inputs.__dict__, "laser": None})
    cost = work_cost(inputs, qty, rates)
    st.caption(_card_summary(inputs, cost))
    if cheaper is not None:
        method, saving = cheaper
        st.caption(f":green[{_METHODS[method]} olisi halvempi: "
                   f"{saving:.2f} €/kpl vähemmän leikkausta.]")
    return inputs


def _speed_help(material: str | None) -> str:
    return (f"Taulukosta ({laser_family(material)[0]}): 1 metrin leikkausaika, kolmen "
            "leikkuuteknologian keskiarvo (Platino 6 kW ja Genius 6 kW).")


def _card_summary(inp: WorkInputs, cost: WorkCost) -> str:
    """One line under the card: the work and what it costs per piece."""
    bits = []
    if inp.method == PUNCH:
        bits.append(f"levari {inp.hits} iskua")
    elif inp.laser is not None:
        bits.append(f"laser {inp.cut_length_m:.2f} m × {inp.laser.s_per_m:g} s/m "
                    f"= {cost.cut_seconds:.0f} s")
    if inp.bends:
        bits.append("1 taivutus" if inp.bends == 1 else f"{inp.bends} taivutusta")
    if inp.threads:
        bits.append("1 kierre" if inp.threads == 1 else f"{inp.threads} kierrettä")
    if inp.fasteners:
        bits.append("1 kiinnike" if inp.fasteners == 1 else f"{inp.fasteners} kiinnikettä")
    if inp.weld_m:
        bits.append(f"hitsaus {inp.weld_m:g} m")
    bits.append(f"asetus {cost.setup:.0f} €/erä")
    if not cost.priced:
        return "Työ: " + " · ".join(bits) + " · laserin hinta puuttuu"
    return "Työ: " + " · ".join(bits) + f" → {cost.per_piece:.2f} €/kpl"


# ── Costs for the pieces summary ──────────────────────────────────────────────

def work_costs(products: list[dict], groups: dict[tuple, list[dict]],
               sheets: dict[tuple, int]) -> dict[str, WorkCost]:
    """Each product's work cost by id, the cutting raised to the per-sheet
    minimum over its group's sheets (groups not yet calculated get none)."""
    rates = current_rates()
    out = {p["id"]: work_cost(p["work"], p["qty"], rates) for p in products if "work" in p}
    for key, prods in groups.items():
        ids = [p["id"] for p in prods if p["id"] in out]
        if key in sheets and ids:
            for pid, c in zip(ids, apply_sheet_minimum([out[i] for i in ids],
                                                       sheets[key], rates)):
                out[pid] = c
    return out
