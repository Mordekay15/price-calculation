"""The sheet-size table shown under each material group."""

import re

import pandas as pd
import pyarrow as pa

from core.sheet_cost import SheetOption
from view.sheet_usage import _styled_table, _table_rows


def test_table_with_a_size_that_does_not_fit_converts_to_arrow():
    fits = SheetOption(sw=3000, sh=1500, base_ppt=1000, adjusted_ppt=1100, packing=None,
                       sheet_weight_kg=100, sheets_needed=2, total_eur=220, utilization=0.6)
    too_small = SheetOption(sw=1000, sh=500, base_ppt=1000, adjusted_ppt=1100, packing=None,
                            failed=3, reason="ei mahdu")
    rows = _table_rows([fits, too_small], n_pieces=4, cheapest_idx=0)
    # Streamlit sends tables to the browser as Arrow; a number column with a
    # text cell fails this and gets logged as a traceback.
    pa.Table.from_pandas(pd.DataFrame(rows))
    assert rows[1]["€/kpl"] is None and rows[0]["€/kpl"] == 55.0


def test_only_the_cheapest_row_is_coloured():
    fits = SheetOption(sw=3000, sh=1500, base_ppt=1000, adjusted_ppt=1100, packing=None,
                       sheet_weight_kg=100, sheets_needed=2, total_eur=220, utilization=0.6)
    too_small = SheetOption(sw=1000, sh=500, base_ppt=1000, adjusted_ppt=1100, packing=None,
                            failed=3, reason="ei mahdu")
    styled = _styled_table(_table_rows([too_small, fits], n_pieces=4, cheapest_idx=1), 1)
    html = styled.to_html()
    # Styler writes one CSS rule listing every coloured cell by its row.
    rule = re.search(r"([^}]*)\{\s*background-color: rgba\(34, 197, 94", html).group(1)
    assert "_row1_" in rule and "_row0_" not in rule
    assert "55.00" in html                 # €/kpl, two decimals
