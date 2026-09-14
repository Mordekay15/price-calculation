# Price Tool

The project is done in Python.
Deployment made with Streamlit.
The data is in supabase.
Streamlit app for parsing PDF price lists and calculating material costs.

## Run locally

```bash
pip install streamlit pdfplumber
streamlit run app.py
```

## Deploy (free)

1. Push this folder to a GitHub repo
2. Go to [streamlit.io/cloud](https://streamlit.io/cloud)
3. Connect your repo — it reads `requirements.txt` automatically
4. Share the public URL with your colleagues

## Project structure

```
/
│
├── app.py                  # Entry point — page config, upload, tab wiring
│
├── core/
│   ├── parser.py           # PDF parsing — extract tables from the PDF
│   ├── calculator.py       # Business logic — price calculations, sorting
│   └── export.py           # Export helpers — CSV (add Excel here later)
│
├── views/
│   ├── calculator.py       # Price calculator tab UI
│   └── tables.py           # Read-only table tabs (thin, thick, forecast, surcharges)
│
└── requirements.txt
```

## How to expand

| What you want to add | Where to do it |
|---|---|
| Support a new PDF layout | `core/parser.py` — add a new `parse_*` function |
| New calculation (e.g. area-based) | `core/calculator.py` — add a new function |
| New export format (e.g. Excel) | `core/export.py` — add a new function |
| New tab (e.g. price history chart) | `views/` — add a new file, import in `app.py` |
| New input field in the calculator | `views/calculator.py` — add `st.number_input`, pass to `calculate()` |
| Support more DXF entity types | `core/dxf.py` — geometry is flattened via `ezdxf` |
| Shape-aware (interlocking) nesting | `core/nesting.py` — currently packs by bounding box |

## DXF nesting

The **DXF-nestaus** tab lets you upload DXF files (one product per file). Each
drawing is parsed with [`ezdxf`](https://ezdxf.mozman.at/): the real outline is
flattened into polylines and the bounding box gives the part's width × height in
millimetres (converted from the drawing's own units). Parts then flow through
the same sheet-usage pricing and layout as the manual calculator, but each piece
is drawn as its true shape. Nesting still packs by bounding box for now —
shape-aware interlocking can be added in `core/nesting.py` later.
