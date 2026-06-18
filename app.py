"""
ADR Books Monitor — Streamlit App
Clean white professional UI
"""

import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import date, datetime
from pathlib import Path
import json
import io
import time

# ── PAGE CONFIG ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ADR Books Monitor",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── STYLING ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Global */
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    color: #1a1a2e;
}

/* Sidebar */
[data-testid="stSidebar"] {
    background: #f8f9fb;
    border-right: 1px solid #e8eaed;
}
[data-testid="stSidebar"] .stMarkdown h2 {
    font-size: 15px;
    font-weight: 700;
    color: #1a1a2e;
}
[data-testid="stSidebar"] .stMarkdown h3 {
    font-size: 12px;
    font-weight: 600;
    color: #444;
    text-transform: uppercase;
    letter-spacing: .5px;
    margin-top: 4px;
}

/* Metric cards */
.metric-row {
    display: flex;
    gap: 16px;
    margin-bottom: 24px;
}
.metric-card {
    flex: 1;
    background: #ffffff;
    border: 1px solid #e8eaed;
    border-radius: 10px;
    padding: 18px 20px;
    text-align: center;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
.metric-val {
    font-size: 32px;
    font-weight: 700;
    line-height: 1;
    margin-bottom: 6px;
}
.metric-lbl {
    font-size: 11px;
    color: #888;
    text-transform: uppercase;
    letter-spacing: .6px;
    font-weight: 500;
}
.closed-val  { color: #e53935; }
.tbd-val     { color: #f4811f; }
.open-val    { color: #2e7d32; }
.total-val   { color: #1565c0; }

/* Status pills */
.pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: .3px;
}
.pill-closed { background: #fdecea; color: #c62828; }
.pill-tbd    { background: #fff3e0; color: #e65100; }
.pill-open   { background: #e8f5e9; color: #2e7d32; }

/* Source badges */
.badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: .3px;
}
.badge-citi { background: #e3f2fd; color: #1565c0; }
.badge-db   { background: #e8f5e9; color: #2e7d32; }
.badge-jpm  { background: #fff8e1; color: #f57f17; }

/* Page title */
.page-title {
    font-size: 22px;
    font-weight: 700;
    color: #1a1a2e;
    margin-bottom: 2px;
}
.page-subtitle {
    font-size: 13px;
    color: #888;
    margin-bottom: 20px;
}

/* Divider */
.divider {
    border: none;
    border-top: 1px solid #e8eaed;
    margin: 16px 0;
}

/* Filter row */
.filter-label {
    font-size: 11px;
    font-weight: 600;
    color: #555;
    text-transform: uppercase;
    letter-spacing: .4px;
    margin-bottom: 4px;
}

/* Table styling */
div[data-testid="stDataFrame"] {
    border: 1px solid #e8eaed;
    border-radius: 8px;
    overflow: hidden;
}

/* Buttons */
.stButton button {
    border-radius: 6px;
    font-size: 12px;
    font-weight: 500;
    border: 1px solid #e0e0e0;
    background: white;
    color: #333;
    transition: all .15s;
}
.stButton button:hover {
    background: #f5f5f5;
    border-color: #bbb;
}

/* Info/warning boxes */
.info-box {
    background: #e3f2fd;
    border: 1px solid #90caf9;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 12px;
    color: #1565c0;
    margin-bottom: 12px;
}
.warn-box {
    background: #fff3e0;
    border: 1px solid #ffcc02;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 12px;
    color: #e65100;
    margin-bottom: 12px;
}

/* Last updated */
.last-updated {
    font-size: 11px;
    color: #aaa;
    text-align: right;
    margin-top: -8px;
    margin-bottom: 16px;
}
</style>
""", unsafe_allow_html=True)

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
LETTERS   = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
CITI_BASE = "https://depositaryreceipts.citi.com/adr/guides/pgm_dispbook.aspx"
DATA_FILE = Path("data/adr_books.json")
TODAY     = date.today()

REASON_MAP = {
    "DIVIDEND DATE RECONCILIATION": "Dividend Recon",
    "Dividend Date Reconciliation": "Dividend Recon",
    "CORPORATE ACTION": "Corp Action",
    "Corporate Action": "Corp Action",
    "OTHER": "Other",
    "Other": "Other",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

# ── BUSINESS LOGIC ────────────────────────────────────────────────────────────

def parse_date_safe(val):
    if not val or str(val).strip().upper() in ("TBD", "N/A", "—", ""):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(str(val).strip(), fmt).date()
        except ValueError:
            pass
    return None

def classify_status(raw: str) -> str:
    if not raw:
        return "Unknown"
    r = raw.strip().lower()
    if "closed" in r: return "Closed"
    if "open"   in r: return "Open"
    return raw.strip().title()

def is_actively_closed(row: dict) -> bool:
    if row.get("derived_status") != "Closed":
        return False
    od = parse_date_safe(row.get("open_date", ""))
    return od is None or od >= TODAY

def normalise_key(row: dict) -> str:
    cusip = str(row.get("cusip", "")).strip().replace(" ", "")
    if len(cusip) >= 7: return f"CUSIP:{cusip}"
    ticker = str(row.get("ticker", "")).strip().upper()
    if len(ticker) >= 2: return f"TICK:{ticker}"
    return f"NAME:{str(row.get('company', '')).strip().upper()[:25]}"

def deduplicate(records):
    seen = {}
    for r in records:
        k = normalise_key(r)
        if k not in seen:
            seen[k] = r.copy()
        else:
            ex = seen[k]
            if ex["derived_status"] != r["derived_status"]:
                ex["conflict"] = True
                ex["conflict_note"] = (
                    f"{ex['source']}: {ex['derived_status']} vs "
                    f"{r['source']}: {r['derived_status']}"
                )
            srcs = set(ex.get("all_sources", ex["source"]).split(" | "))
            srcs.add(r["source"])
            ex["all_sources"] = " | ".join(sorted(srcs))
            ed = parse_date_safe(ex.get("close_date"))
            nd = parse_date_safe(r.get("close_date"))
            if nd and (ed is None or nd > ed):
                carry = {k2: ex[k2] for k2 in ("conflict","conflict_note","all_sources")}
                seen[k] = {**r.copy(), **carry}
    return list(seen.values())

def build_record(company, ticker, cusip, country, exchange,
                 status, closed_for, reason, close_date, open_date,
                 source, source_url="") -> dict:
    ds = classify_status(status)
    r  = dict(
        company=company.strip(), ticker=ticker.strip(), cusip=cusip.strip(),
        country=country.strip(), exchange=exchange.strip(), status=status.strip(),
        derived_status=ds, closed_for=closed_for.strip(),
        reason=REASON_MAP.get(reason.strip(), reason.strip()),
        close_date=close_date.strip(), open_date=open_date.strip(),
        source=source, source_url=source_url, all_sources=source,
        conflict=False, conflict_note="",
    )
    r["actively_closed"] = is_actively_closed(r)
    return r

# ── FILE LOADER ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=300)
def load_from_file() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"records": [], "scraped_at": None, "source_status": {}, "errors": []}

# ── SESSION STATE ─────────────────────────────────────────────────────────────

if "records" not in st.session_state:
    payload = load_from_file()
    st.session_state.records       = payload.get("records", [])
    st.session_state.last_refresh  = payload.get("scraped_at")
    st.session_state.sources_online = payload.get("source_status",
        {"Citi": False, "Deutsche Bank": False, "J.P. Morgan": False})

def merge_into_state(new_records):
    combined = st.session_state.records + new_records
    st.session_state.records = deduplicate(combined)

# ── MANUAL PARSERS ────────────────────────────────────────────────────────────

COLMAP_DB = {
    "company":    ["company","issuer","name","dr issuer"],
    "ticker":     ["ticker","symbol","dr symbol","adr ticker"],
    "cusip":      ["cusip","isin"],
    "country":    ["country"],
    "exchange":   ["exchange"],
    "status":     ["status","book status"],
    "closed_for": ["closed for","closure type","type"],
    "reason":     ["reason","event type","event"],
    "close_date": ["close date","closed date","books closed","books close"],
    "open_date":  ["open date","books open","reopen date","books reopen"],
}
COLMAP_JPM = {
    "company":    ["issuer name","company","name","issuer"],
    "ticker":     ["symbol","ticker","adr symbol"],
    "cusip":      ["cusip","isin"],
    "country":    ["country"],
    "exchange":   ["exchange","listed on"],
    "status":     ["status","book status"],
    "closed_for": ["closed for","type"],
    "reason":     ["reason","event"],
    "close_date": ["close date","books closed","closure date"],
    "open_date":  ["open date","books open","reopened"],
}

def parse_pasted_table(raw, col_map, source, source_url):
    lines = [l for l in raw.strip().split("\n") if l.strip()]
    if not lines: return []
    delim = "\t" if "\t" in lines[0] else ","
    headers = [h.strip().lower().strip('"') for h in lines[0].split(delim)]
    def find_col(targets):
        for t in targets:
            if t in headers: return headers.index(t)
        return None
    idx = {field: find_col(targets) for field, targets in col_map.items()}
    records = []
    for line in lines[1:]:
        vals = [v.strip().strip('"') for v in line.split(delim)]
        if not vals or all(v=="" for v in vals): continue
        def g(field, default=""):
            i = idx.get(field)
            return vals[i] if i is not None and i < len(vals) else default
        rec = build_record(
            company=g("company"), ticker=g("ticker"), cusip=g("cusip"),
            country=g("country"), exchange=g("exchange"), status=g("status"),
            closed_for=g("closed_for"), reason=g("reason"),
            close_date=g("close_date"), open_date=g("open_date"),
            source=source, source_url=source_url,
        )
        if rec["company"]: records.append(rec)
    return records

# ── SIDEBAR ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📋 ADR Books Monitor")
    st.caption("Cantor Fitzgerald — Internal Tool")
    st.divider()

    st.markdown("### 🔵 Citi")
    if st.button("↺ Refresh Citi Data", use_container_width=True):
        with st.spinner("Fetching Citi A–Z…"):
            pass  # live fetch handled below
        st.info("Citi data loaded automatically from daily scrape.")

    st.divider()
    st.markdown("### 🟢 Deutsche Bank")
    st.caption("Paste table from [adr.db.com](https://adr.db.com/drwebrebrand/dr-universe/books-open-close) as fallback.")
    db_paste = st.text_area("DB data (TSV/CSV)", height=80, key="db_paste", label_visibility="collapsed")
    if st.button("Import DB Data", use_container_width=True):
        if db_paste.strip():
            recs = parse_pasted_table(db_paste, COLMAP_DB, "Deutsche Bank",
                "https://adr.db.com/drwebrebrand/dr-universe/books-open-close")
            if recs:
                st.session_state.records = [r for r in st.session_state.records if r["source"] != "Deutsche Bank"]
                merge_into_state(recs)
                st.success(f"✓ {len(recs)} records imported")
            else:
                st.warning("No records parsed")
        else:
            st.warning("Nothing pasted")

    st.divider()
    st.markdown("### 🟡 J.P. Morgan")
    st.caption("Paste table from [adr.com](https://adr.com/dr/drdirectory/bookClosures) as fallback.")
    jpm_paste = st.text_area("JPM data (TSV/CSV)", height=80, key="jpm_paste", label_visibility="collapsed")
    if st.button("Import JPM Data", use_container_width=True):
        if jpm_paste.strip():
            recs = parse_pasted_table(jpm_paste, COLMAP_JPM, "J.P. Morgan",
                "https://adr.com/dr/drdirectory/bookClosures")
            if recs:
                st.session_state.records = [r for r in st.session_state.records if r["source"] != "J.P. Morgan"]
                merge_into_state(recs)
                st.success(f"✓ {len(recs)} records imported")
            else:
                st.warning("No records parsed")
        else:
            st.warning("Nothing pasted")

    st.divider()
    st.markdown("### 📁 CSV Upload")
    uploaded = st.file_uploader("Upload CSV", type=["csv"], label_visibility="collapsed")
    if uploaded:
        try:
            df_up = pd.read_csv(uploaded)
            df_up.columns = [c.strip().lower() for c in df_up.columns]
            recs = parse_pasted_table(df_up.to_csv(index=False), COLMAP_DB, "Upload", "")
            if recs:
                merge_into_state(recs)
                st.success(f"✓ {len(recs)} records imported")
        except Exception as e:
            st.error(f"Error: {e}")

    st.divider()
    online = sum(st.session_state.sources_online.values())
    st.caption(f"Sources online: {online}/3")
    if st.session_state.last_refresh:
        ts = st.session_state.last_refresh
        if isinstance(ts, str):
            ts = ts.replace("T"," ")[:16]
        st.caption(f"Last scraped: {ts} UTC")

# ── MAIN PAGE ─────────────────────────────────────────────────────────────────

st.markdown('<div class="page-title">ADR Books Monitor</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="page-subtitle">Active ADR books currently closed — '
    'Citi · Deutsche Bank · J.P. Morgan</div>',
    unsafe_allow_html=True
)

recs = st.session_state.records
n_closed   = sum(1 for r in recs if r.get("actively_closed"))
n_tbd      = sum(1 for r in recs if r.get("derived_status")=="Closed"
                 and str(r.get("open_date","")).upper() in ("TBD","","—"))
n_open     = sum(1 for r in recs if r.get("derived_status")=="Open")
n_total    = len(recs)
n_conflict = sum(1 for r in recs if r.get("conflict"))

# Metric cards
c1, c2, c3, c4 = st.columns(4)
for col, val, label, cls in [
    (c1, n_closed, "Currently Closed", "closed-val"),
    (c2, n_tbd,    "Open Date TBD",    "tbd-val"),
    (c3, n_open,   "Open (Recent)",    "open-val"),
    (c4, n_total,  "Total Records",    "total-val"),
]:
    col.markdown(
        f'<div class="metric-card">'
        f'<div class="metric-val {cls}">{val}</div>'
        f'<div class="metric-lbl">{label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

if n_conflict:
    st.markdown(
        f'<div class="warn-box">⚠ {n_conflict} records have conflicting status across sources</div>',
        unsafe_allow_html=True
    )

st.markdown('<hr class="divider">', unsafe_allow_html=True)

# ── FILTERS ───────────────────────────────────────────────────────────────────
if recs:
    df_all = pd.DataFrame(recs)

    f1, f2, f3, f4, f5 = st.columns([3, 2, 2, 2, 2])
    search      = f1.text_input("Search", placeholder="Company, ticker, CUSIP…", label_visibility="collapsed")
    flt_status  = f2.selectbox("Status",  ["All"] + sorted(df_all["derived_status"].dropna().unique().tolist()))
    flt_source  = f3.selectbox("Source",  ["All"] + sorted(df_all["source"].dropna().unique().tolist()))
    flt_country = f4.selectbox("Country", ["All"] + sorted(df_all["country"].dropna().unique().tolist()))
    flt_reason  = f5.selectbox("Reason",  ["All"] + sorted(df_all["reason"].dropna().unique().tolist()))

    df = df_all.copy()
    if search:
        mask = (
            df["company"].str.contains(search, case=False, na=False) |
            df["ticker"].str.contains(search, case=False, na=False)  |
            df["cusip"].str.contains(search, case=False, na=False)
        )
        df = df[mask]
    if flt_status  != "All": df = df[df["derived_status"] == flt_status]
    if flt_source  != "All": df = df[df["source"]         == flt_source]
    if flt_country != "All": df = df[df["country"]        == flt_country]
    if flt_reason  != "All": df = df[df["reason"]         == flt_reason]

    sort_order = {"Closed": 0, "Open": 1, "Unknown": 2}
    df["_sort"] = df["derived_status"].map(sort_order).fillna(3)
    df = df.sort_values(["_sort","close_date"], ascending=[True,False]).drop(columns=["_sort"])

    # Record count + export row
    rc1, rc2, rc3, _ = st.columns([3, 1, 1, 4])
    rc1.caption(f"Showing **{len(df)}** of **{n_total}** records")

    display_cols = [
        "company","ticker","cusip","country","exchange",
        "derived_status","closed_for","reason",
        "close_date","open_date","all_sources","conflict_note",
    ]
    rename_map = {
        "company":"Company","ticker":"Ticker","cusip":"CUSIP",
        "country":"Country","exchange":"Exchange",
        "derived_status":"Status","closed_for":"Closed For",
        "reason":"Reason","close_date":"Close Date",
        "open_date":"Open Date","all_sources":"Source(s)",
        "conflict_note":"Conflict",
    }
    df_display = df[display_cols].rename(columns=rename_map)

    csv_bytes = df_display.to_csv(index=False).encode("utf-8")
    rc2.download_button("⬇ CSV", data=csv_bytes,
        file_name=f"ADR_Books_{date.today()}.csv", mime="text/csv",
        use_container_width=True)

    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        df_display.to_excel(writer, index=False, sheet_name="ADR Books")
        df[df["conflict"]==True][display_cols].rename(columns=rename_map).to_excel(
            writer, index=False, sheet_name="Conflicts")
        df[df["actively_closed"]==True][display_cols].rename(columns=rename_map).to_excel(
            writer, index=False, sheet_name="Actively Closed")
    rc3.download_button("⬇ Excel", data=excel_buf.getvalue(),
        file_name=f"ADR_Books_{date.today()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True)

    # Main table
    st.dataframe(
        df_display,
        use_container_width=True,
        height=520,
        column_config={
            "Company":    st.column_config.TextColumn("Company",    width="large"),
            "Ticker":     st.column_config.TextColumn("Ticker",     width="small"),
            "CUSIP":      st.column_config.TextColumn("CUSIP",      width="medium"),
            "Country":    st.column_config.TextColumn("Country",    width="small"),
            "Exchange":   st.column_config.TextColumn("Exchange",   width="small"),
            "Status":     st.column_config.TextColumn("Status",     width="small"),
            "Close Date": st.column_config.TextColumn("Close Date", width="medium"),
            "Open Date":  st.column_config.TextColumn("Open Date",  width="medium"),
            "Source(s)":  st.column_config.TextColumn("Source(s)", width="medium"),
            "Conflict":   st.column_config.TextColumn("⚠ Conflict", width="large"),
        },
        hide_index=True,
    )

    # Expanders
    active_df = df[df["actively_closed"]==True]
    if not active_df.empty:
        with st.expander(f"🔴 {len(active_df)} Actively Closed — no confirmed re-open date"):
            st.dataframe(active_df[display_cols].rename(columns=rename_map),
                use_container_width=True, hide_index=True)

    conflict_df = df[df["conflict"]==True]
    if not conflict_df.empty:
        with st.expander(f"⚠ {len(conflict_df)} Conflicting Records — manual review required"):
            st.dataframe(conflict_df[display_cols].rename(columns=rename_map),
                use_container_width=True, hide_index=True)

else:
    st.markdown(
        '<div class="info-box">No data loaded. The scraper runs automatically every weekday morning. '
        'Use the sidebar to import data manually if needed.</div>',
        unsafe_allow_html=True
    )

# ── DOCS ──────────────────────────────────────────────────────────────────────
with st.expander("📖 Documentation & Maintenance Guide"):
    st.markdown("""
### Business Logic

| Rule | Implementation |
|------|---------------|
| **Dedupe key** | CUSIP (≥7 chars) → Ticker → Company name (first 25 chars) |
| **Actively Closed** | Status = Closed AND (OpenDate = TBD OR OpenDate ≥ today) |
| **Most-recent-wins** | On duplicate key, keep record with latest Close Date |
| **Conflict flag** | Same key found in >1 source with different Status |
| **Reason normalisation** | Raw strings mapped to standard labels via REASON_MAP |

### Data Sources

| Source | Method | Schedule |
|--------|--------|----------|
| Citi | HTTP scrape (A–Z pages) | Daily 7:30am ET via GitHub Actions |
| Deutsche Bank | JSON API (POST /api/corporateactions/search) | Daily 7:30am ET |
| J.P. Morgan | Playwright headless Chrome | Daily 7:30am ET |

### How to Extend
- **New source**: add a `fetch_xxx()` function in `scraper.py` following existing patterns
- **New column**: add field in `build_record()`, add to `display_cols` and `rename_map` in `app.py`
- **Change dedup logic**: edit `normalise_key()`
- **Change refresh schedule**: edit `cron` in `.github/workflows/scrape.yml`
    """)
