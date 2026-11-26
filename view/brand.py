"""Stremet company look: "Stremet" in the sidebar, the full logo in the
quote bar (``quote_logo``), and a few touches the theme
in ``.streamlit/config.toml`` can't set (the blue stripe along the top, the
headings in the logo blue). Colours and font are the ones on stremet.fi."""

from pathlib import Path

import streamlit as st

ASSETS = Path(__file__).resolve().parent.parent / "assets"
LOGO = ASSETS / "stremet-logo.svg"          # with "Levystä tuotteeksi"; the quote bar
WORDMARK = ASSETS / "stremet-wordmark.svg"  # "Stremet" only; the sidebar
ICON = ASSETS / "stremet-icon.png"

BLUE = "#285c9f"

_CSS = f"""
<style>
/* A thin band in the logo blue along the top of the page */
header[data-testid="stHeader"] {{ border-top: 4px solid {BLUE}; }}
/* Headings in the logo blue */
[data-testid="stMain"] h1, [data-testid="stMain"] h2,
[data-testid="stMain"] h3, [data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {{ color: {BLUE}; }}
/* The sidebar's "Stremet" a little bigger than Streamlit's logo size */
img[data-testid="stSidebarLogo"] {{ height: 2rem; max-width: 100%; }}
/* The open tab's name a little bolder */
[data-testid="stTab"][aria-selected="true"] p {{ font-weight: 600; }}
/* The main menu ("Tarjous tiedostoista", "Pikalaskuri") bold and bigger */
.st-key-menu > div > [role="tablist"] [data-testid="stTab"] p {{
    font-size: 1.15rem; font-weight: 700; }}
/* The steps (1 · Osat ja tiedostot ...) a little bigger too */
.st-key-dxf_menu > div > [role="tablist"] [data-testid="stTab"] p {{
    font-size: 1.05rem; font-weight: 600; }}
/* The step strip under them in light blue with a blue edge, so it reads as
   the menu, apart from the calculations below it */
.st-key-dxf_flow {{ background: #e8f0fa; border: 1px solid #c4d5ec;
    border-left: 5px solid {BLUE}; }}
.st-key-dxf_flow p {{ color: {BLUE}; }}
/* The blue line that splits the menu from the calculations */
.stremet-split {{ border-top: 2px solid {BLUE}; margin: 0.25rem 0 0.5rem; }}
</style>
"""


def quote_logo(slot) -> None:
    """The full logo, with "Levystä tuotteeksi", in the quote bar."""
    slot.image(str(LOGO), width=240)


def apply() -> None:
    """Draw the logo and add the company styling; call once, right after
    ``st.set_page_config``."""
    st.logo(str(WORDMARK), size="large", icon_image=str(ICON))
    st.html(_CSS)
