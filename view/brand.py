"""Stremet company look: the logo in the sidebar and a few touches the theme
in ``.streamlit/config.toml`` can't set (the blue stripe along the top, the
headings in the logo blue). Colours and font are the ones on stremet.fi."""

from pathlib import Path

import streamlit as st

ASSETS = Path(__file__).resolve().parent.parent / "assets"
LOGO = ASSETS / "stremet-logo.png"
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
/* The logo (white on blue, with "Levystä tuotteeksi") big enough to read */
[data-testid="stSidebarHeader"] {{ height: auto; padding-top: 1rem; }}
img[data-testid="stSidebarLogo"], img[data-testid="stLogo"] {{
    height: 3.5rem; max-width: 100%; border-radius: 6px; }}
/* The open tab's name a little bolder */
button[data-baseweb="tab"][aria-selected="true"] p {{ font-weight: 600; }}
</style>
"""


def apply() -> None:
    """Draw the logo and add the company styling; call once, right after
    ``st.set_page_config``."""
    st.logo(str(LOGO), size="large", icon_image=str(ICON))
    st.html(_CSS)
