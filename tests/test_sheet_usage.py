"""The sheet-size table shown under each material group."""

import pandas as pd
import pyarrow as pa

from core.sheet_cost import SheetOption
from view.sheet_usage import _table_rows


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
