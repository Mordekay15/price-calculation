"""
Stremet Price Tool — Streamlit App
===================================
Parsed price data is saved to disk (price_data.json) so it persists across
sessions. Upload a new PDF only when the monthly price list is updated — the
upload widget lives in the sidebar.

Install & run:
    pip install streamlit pdfplumber
    streamlit run app.py
"""

import io
import csv
import json
import datetime
import pathlib
import pdfplumber
import streamlit as st

DATA_FILE = pathlib.Path("price_data.json")

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Stremet Price Tool",
    page_icon="🏗️",
    layout="wide",
)

st.title("🏗️ Stremet Price Tool")

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean(value):
    return (value or "").replace("\n", " ").strip()

def to_float(value):
    v = clean(value).replace(" ", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None

def rows_to_csv_bytes(rows):
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")

# ── Persistent storage ────────────────────────────────────────────────────────

def load_stored_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def save_data(data, filename):
    payload = {
        "data": data,
        "source_file": filename,
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload

# ── PDF parser ────────────────────────────────────────────────────────────────

def parse_pdf(file_bytes):
    thin_rows   = []
    thick_rows  = []
    forecast    = []
    surcharges  = []
    special     = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        num_pages = len(pdf.pages)

        # Page 2 — quantity forecast
        if num_pages >= 2:
            tables = pdf.pages[1].extract_tables()
            if tables:
                for row in tables[0][1:]:
                    cells = clean(row[0]).split()
                    if len(cells) >= 2 and not cells[0].isdigit():
                        forecast.append({
                            "Materiaali":  cells[0],
                            "+1kk (tn)":   to_float(cells[1]) if len(cells) > 1 else None,
                            "+2kk (tn)":   to_float(cells[2]) if len(cells) > 2 else None,
                            "+3kk (tn)":   to_float(cells[3]) if len(cells) > 3 else None,
                            "Summa (tn)":  to_float(cells[4]) if len(cells) > 4 else None,
                        })

        # Page 3 — price tables
        if num_pages >= 3:
            tables = pdf.pages[2].extract_tables()

            # Thin sheets
            if len(tables) > 0:
                for row in tables[0][1:]:
                    thickness = clean(row[0])
                    if not thickness:
                        continue
                    thin_rows.append({
                        "Paksuus (mm)":                                       thickness,
                        "Kylmävalssattu DC01 | 1000x2000":                    to_float(row[1]),
                        "Kylmävalssattu DC01 | 1250x2500/1500x3000":          to_float(row[2]),
                        "Kuumasinkitty Z275 | 1000x2000":                     to_float(row[3]),
                        "Kuumasinkitty Z275 | 1250x2500/1500x3000":           to_float(row[4]),
                        "Sähkösinkitty ZE | 1000x2000":                       to_float(row[5]),
                        "Sähkösinkitty ZE | 1250x2500/1500x3000":             to_float(row[6]),
                    })

            # Thick sheets
            if len(tables) > 1:
                for row in tables[1][1:]:
                    thickness = clean(row[0])
                    if not thickness:
                        continue
                    thick_rows.append({
                        "Paksuus (mm)":                    thickness,
                        "S355MC P+O | 1250x2500":          to_float(row[1]),
                        "S355MC P+O | 1500x3000":          to_float(row[2]),
                        "S355MC | 1500x3000":              to_float(row[3]),
                        "S650MC P+O | 1500x3000":          to_float(row[4]),
                        "S650MC | 1500x3000":              to_float(row[5]),
                    })

            # Surcharges
            if len(tables) > 2:
                for row in tables[2]:
                    desc = clean(row[0])
                    val  = clean(row[1]) if len(row) > 1 else ""
                    if desc and desc != "LISÄVELOITUKSET":
                        surcharges.append({"Kuvaus": desc, "€/tn": val})

            # Special reserved item
            if len(tables) > 3:
                for row in tables[3][1:]:
                    thickness = clean(row[0])
                    price     = to_float(row[1]) if len(row) > 1 else None
                    if thickness:
                        special.append({
                            "Paksuus (mm)": thickness,
                            "Kuumasinkitty DX51D+Z100MAC (€/tn)": price,
                        })

    return {
        "thin":       thin_rows,
        "thick":      thick_rows,
        "forecast":   forecast,
        "surcharges": surcharges,
        "special":    special,
    }

# ── Build flat price lookup for the calculator ────────────────────────────────

def build_price_lookup(data):
    lookup = {}
    for row in data["thin"]:
        t = row["Paksuus (mm)"]
        for col, val in row.items():
            if col != "Paksuus (mm)" and val is not None:
                lookup[(t, col)] = val
    for row in data["thick"]:
        t = row["Paksuus (mm)"]
        for col, val in row.items():
            if col != "Paksuus (mm)" and val is not None:
                lookup[(t, col)] = val
    for row in data["special"]:
        t = row["Paksuus (mm)"]
        for col, val in row.items():
            if col != "Paksuus (mm)" and val is not None:
                lookup[(t, col)] = val
    return lookup

# ── Sidebar — monthly PDF update ──────────────────────────────────────────────

with st.sidebar:
    st.header("Update price list")
    st.caption("Upload a new PDF when the monthly price list arrives.")
    uploaded = st.file_uploader("PDF price list", type="pdf", label_visibility="collapsed")

    if uploaded:
        with st.spinner("Parsing PDF…"):
            parsed = parse_pdf(uploaded.read())
        payload = save_data(parsed, uploaded.name)
        st.success(f"Saved — {uploaded.name}")
        st.rerun()

# ── Load data (from disk or prompt first upload) ──────────────────────────────

stored = load_stored_data()

if stored is None:
    st.info("No price data found. Upload a PDF in the sidebar to get started.")
    st.stop()

data   = stored["data"]
lookup = build_price_lookup(data)

updated_at  = stored.get("updated_at", "unknown")
source_file = stored.get("source_file", "unknown")
st.caption(f"Price list: **{source_file}** — last updated {updated_at}")
st.divider()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_calc, tab_thin, tab_thick, tab_forecast, tab_extra = st.tabs([
    "🧮 Price calculator",
    "📋 Thin sheets",
    "📋 Thick sheets",
    "📦 Forecast",
    "➕ Surcharges",
])

# ── TAB: Price calculator ─────────────────────────────────────────────────────

with tab_calc:
    st.subheader("Price calculator")
    st.caption("Pick a material, enter quantity, and apply any adjustments.")

    all_products    = sorted(set(label for (_, label) in lookup.keys()))
    all_thicknesses = sorted(set(t for (t, _) in lookup.keys()),
                             key=lambda x: float(x.replace(",", ".").split("x")[0]) if x[0].isdigit() else 999)

    col1, col2 = st.columns(2)

    with col1:
        product   = st.selectbox("Material / product", all_products)
        available = sorted(
            [t for (t, p) in lookup.keys() if p == product],
            key=lambda x: float(x.replace(",", ".").split("x")[0]) if x[0].isdigit() else 999
        )
        thickness = st.selectbox("Thickness (mm)", available if available else ["—"])

    with col2:
        quantity = st.number_input("Quantity (tonnes)", min_value=0.0, value=10.0, step=0.5)

    st.divider()
    st.markdown("**Adjustments**")

    col3, col4, col5 = st.columns(3)
    with col3:
        margin_pct = st.number_input("Margin %", min_value=0.0, max_value=200.0, value=0.0, step=0.5,
                                     help="Added on top of the base price")
    with col4:
        surcharge  = st.number_input("Extra surcharge (€/tn)", min_value=0.0, value=0.0, step=5.0,
                                     help="E.g. custom cut sizes +30 €/tn")
    with col5:
        fx_rate    = st.number_input("Currency multiplier", min_value=0.01, value=1.0, step=0.01,
                                     help="1.0 = keep EUR. Use e.g. 1.08 to convert to another currency.")

    # ── Calculation ───────────────────────────────────────────────────────────

    base_price = lookup.get((thickness, product))

    if base_price is None:
        st.warning("No price found for this combination.")
    else:
        price_after_margin    = base_price * (1 + margin_pct / 100)
        price_with_surcharge  = price_after_margin + surcharge
        price_converted       = price_with_surcharge * fx_rate
        total_cost            = price_converted * quantity

        st.divider()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Base price (€/tn)",       f"{base_price:,.2f}")
        m2.metric("After margin & surcharge", f"{price_with_surcharge:,.2f}")
        m3.metric("After currency adj.",      f"{price_converted:,.2f}")
        m4.metric(f"Total for {quantity} tn", f"{total_cost:,.2f}")

        st.divider()

        st.markdown(f"**All thicknesses for** *{product}*")
        comp_rows = []
        for t in available:
            bp = lookup.get((t, product))
            if bp is None:
                continue
            adj   = (bp * (1 + margin_pct / 100) + surcharge) * fx_rate
            total = adj * quantity
            comp_rows.append({
                "Thickness": t,
                "Base (€/tn)": f"{bp:,.2f}",
                "Adjusted (€/tn)": f"{adj:,.2f}",
                f"Total ({quantity} tn)": f"{total:,.2f}",
                "Selected": "◀" if t == thickness else "",
            })
        st.dataframe(comp_rows, use_container_width=True, hide_index=True)

# ── TAB: Thin sheets ──────────────────────────────────────────────────────────

with tab_thin:
    st.subheader("Thin sheet prices  (€/tn, alv 0%)")
    if data["thin"]:
        st.dataframe(data["thin"], use_container_width=True, hide_index=True)
        st.download_button("⬇ Download CSV", rows_to_csv_bytes(data["thin"]),
                           "hinnat_ohutlevy.csv", "text/csv")
    else:
        st.info("No thin sheet data found.")

# ── TAB: Thick sheets ─────────────────────────────────────────────────────────

with tab_thick:
    st.subheader("Thick sheet prices  (€/tn, alv 0%)")
    if data["thick"]:
        st.dataframe(data["thick"], use_container_width=True, hide_index=True)
        st.download_button("⬇ Download CSV", rows_to_csv_bytes(data["thick"]),
                           "hinnat_paksulevy.csv", "text/csv")
    else:
        st.info("No thick sheet data found.")

# ── TAB: Forecast ─────────────────────────────────────────────────────────────

with tab_forecast:
    st.subheader("Quantity forecast  (tonnes)")
    if data["forecast"]:
        st.dataframe(data["forecast"], use_container_width=True, hide_index=True)
        st.download_button("⬇ Download CSV", rows_to_csv_bytes(data["forecast"]),
                           "maaraennuste.csv", "text/csv")
    else:
        st.info("No forecast data found.")

# ── TAB: Surcharges ───────────────────────────────────────────────────────────

with tab_extra:
    st.subheader("Surcharges & special items")
    if data["surcharges"]:
        st.markdown("**Lisäveloitukset**")
        st.dataframe(data["surcharges"], use_container_width=True, hide_index=True)
    if data["special"]:
        st.markdown("**Korvamerkitty varastoitava nimike**")
        st.dataframe(data["special"], use_container_width=True, hide_index=True)
    if not data["surcharges"] and not data["special"]:
        st.info("No surcharge data found.")
