# CLAUDE.md

## Project overview

**Stremet Price Tool** — a Streamlit web app that parses Tata Steel / Stremet PDF price lists and lets users browse pricing tables, calculate material costs, and export results as CSV.

## Stack

- **Python** with [Streamlit](https://streamlit.io/) for the UI
- **pdfplumber** for PDF table extraction
- No database; all state lives in-memory per session via `st.cache_data`

## Running locally

```bash
pip install streamlit pdfplumber
streamlit run app.py
```

## Key files

| File | Purpose |
|------|---------|
| `app.py` | Entire application — PDF parsing, price lookup, and Streamlit UI |
| `requirements.txt` | Python dependencies (`streamlit`, `pdfplumber`) |

## Architecture

`app.py` is intentionally a single-file app with three logical sections:

1. **PDF parser** (`parse_pdf`) — extracts five table types from a fixed page layout:
   - Page 2: quantity forecast
   - Page 3: thin sheet prices, thick sheet prices, surcharges, special reserved item
2. **Price lookup builder** (`build_price_lookup`) — flattens parsed rows into a `(thickness, product_label) -> €/tn` dict
3. **Streamlit UI** — file upload, tabbed views (calculator, thin/thick sheets, forecast, surcharges), and CSV download buttons

## PDF format assumptions

The parser is tightly coupled to a specific Stremet/Tata Steel PDF layout:
- Tables are extracted by page index and table index (not by heading text)
- Numeric values use comma as the decimal separator (converted to `.` for `float()`)
- Thickness column is always the first column; it may contain `x` (e.g. `3x1250`)

## Development notes

- All Finnish-language column names in parsed data are intentional (source documents are in Finnish)
- `@st.cache_data` on `parse_pdf` means re-uploading the same file bytes won't re-parse
- The price calculator tab shows a comparison table across all thicknesses for the selected product
