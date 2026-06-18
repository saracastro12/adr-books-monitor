"""
ADR Books Monitor — Streamlit App
==================================
Data sources:
  • Citi   : fetched live (A–Z letter pages)
  • DB     : manual paste (robots.txt blocks scraping)
  • JPM    : manual paste (JS-rendered, cannot be fetched)

Business logic translated from Excel workbook:
  • Dedupe key   → CUSIP > Ticker > Company name
  • Active Closed → Status=Closed AND (OpenDate=TBD OR OpenDate>=today)
  • Conflict flag → same dedupe key appears in >1 source with differing Status
  • Reason map   → normalise raw reason strings to standard labels
  • Most-recent-wins → on dedupe, keep record with latest CloseDate
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
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── STYLING ───────────────────────────────────────────────────────────────────
st.markdown("""
<style>
[data-testid="stSidebar"] { background: #1e2433; }
.metric-card {
    background: #1e2433; border-radius: 8px;
    padding: 14px 18px; text-align: center;
    border: 1px solid #2d3748;
}
.metric-val { font-size: 28px; font-weight: 700; }
.metric-lbl { font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: .5px; margin-top: 2px; }
.closed-val  { color: #ef4444; }
.tbd-val     { color: #f59e0b; }
.open-val    { color: #22c55e; }
.total-val   { color: #3b82f6; }
div[data-testid="stDataFrame"] table { font-size: 12px; }
</style>
""", unsafe_allow_html=True)

# ── CONSTANTS ─────────────────────────────────────────────────────────────────
LETTERS = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
CITI_BASE = "https://depositaryreceipts.citi.com/adr/guides/pgm_dispbook.aspx"

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

TODAY = date.today()

# ── BUSINESS LOGIC ────────────────────────────────────────────────────────────

def parse_date_safe(val):
    """Return date object or None; handles TBD/blank/various formats."""
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
    if "closed" in r:
        return "Closed"
    if "open" in r:
        return "Open"
    return raw.strip().title()


def is_actively_closed(row: dict) -> bool:
    """True if book is Closed and has no confirmed future re-open date."""
    if row.get("derived_status") != "Closed":
        return False
    od = parse_date_safe(row.get("open_date", ""))
    if od is None:
        return True          # TBD or blank → still closed
    return od >= TODAY       # re-open is in the future


def normalise_key(row: dict) -> str:
    """Deduplication key: CUSIP > Ticker > Company (first 25 chars)."""
    cusip = str(row.get("cusip", "")).strip().replace(" ", "")
    if len(cusip) >= 7:
        return f"CUSIP:{cusip}"
    ticker = str(row.get("ticker", "")).strip().upper()
    if len(ticker) >= 2:
        return f"TICK:{ticker}"
    return f"NAME:{str(row.get('company', '')).strip().upper()[:25]}"


def deduplicate(records: list[dict]) -> list[dict]:
    """
    Merge records with the same key.
    • Keep the record with the latest close_date (most recent info).
    • Flag conflicts where status disagrees across sources.
    • Merge source labels.
    """
    seen: dict[str, dict] = {}
    for r in records:
        k = normalise_key(r)
        if k not in seen:
            seen[k] = r.copy()
        else:
            ex = seen[k]
            # Detect status conflict
            if ex["derived_status"] != r["derived_status"]:
                ex["conflict"] = True
                ex["conflict_note"] = (
                    f"{ex['source']}: {ex['derived_status']} vs "
                    f"{r['source']}: {r['derived_status']}"
                )
            # Merge sources
            srcs = set(ex.get("all_sources", ex["source"]).split(" | "))
            srcs.add(r["source"])
            ex["all_sources"] = " | ".join(sorted(srcs))
            # Keep more recent close date
            ed = parse_date_safe(ex.get("close_date"))
            nd = parse_date_safe(r.get("close_date"))
            if nd and (ed is None or nd > ed):
                conflict_carry = ex.get("conflict", False)
                note_carry = ex.get("conflict_note", "")
                srcs_carry = ex.get("all_sources", r["source"])
                seen[k] = r.copy()
                seen[k]["conflict"] = conflict_carry
                seen[k]["conflict_note"] = note_carry
                seen[k]["all_sources"] = srcs_carry
    return list(seen.values())


def build_record(company, ticker, cusip, country, exchange,
                 status, closed_for, reason, close_date, open_date,
                 source, source_url="") -> dict:
    ds = classify_status(status)
    r = dict(
        company=company.strip(),
        ticker=ticker.strip(),
        cusip=cusip.strip(),
        country=country.strip(),
        exchange=exchange.strip(),
        status=status.strip(),
        derived_status=ds,
        closed_for=closed_for.strip(),
        reason=REASON_MAP.get(reason.strip(), reason.strip()),
        close_date=close_date.strip(),
        open_date=open_date.strip(),
        source=source,
        source_url=source_url,
        all_sources=source,
        conflict=False,
        conflict_note="",
    )
    r["actively_closed"] = is_actively_closed(r)
    return r


# ── CITI SCRAPER ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_citi_all() -> tuple[list[dict], list[str]]:
    """
    Fetch all A–Z (+ 0–9) pages from Citi.
    Returns (records, errors).
    Cached for 1 hour so re-runs don't re-scrape.
    """
    records, errors = [], []
    for letter in LETTERS:
        try:
            params = {"pageId": "5", "subPageId": "48", "company": letter}
            resp = requests.get(
                CITI_BASE.replace("books.aspx", "pgm_dispbook.aspx"),
                params=params,
                headers=HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            recs = parse_citi_page(resp.text, letter)
            records.extend(recs)
            time.sleep(0.3)          # polite delay
        except Exception as e:
            errors.append(f"Citi [{letter}]: {e}")
    return records, errors


def parse_citi_page(html: str, letter: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    records = []
    # Find the main data table (has >5 columns)
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        # Check if first data row looks like ADR data
        first_cells = rows[1].find_all("td") if len(rows) > 1 else []
        if len(first_cells) < 8:
            continue
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 10:
                continue
            company_tag = cells[0].find("a")
            company = company_tag.text.strip() if company_tag else cells[0].text.strip()
            source_url = company_tag["href"] if company_tag else ""
            if source_url and source_url.startswith("/"):
                source_url = "https://depositaryreceipts.citi.com" + source_url

            ticker   = cells[2].text.strip()
            cusip    = cells[3].text.strip()
            country  = cells[4].text.strip()
            exchange = cells[5].text.strip()
            status   = cells[6].text.strip()
            closed_for = cells[7].text.strip()
            reason   = cells[8].text.strip()
            close_dt = cells[9].text.strip()
            open_dt  = cells[10].text.strip() if len(cells) > 10 else ""

            # Skip header-like rows
            if ticker.lower() in ("ticker", "") and company.lower() in ("company", ""):
                continue
            if not company or len(company) < 2:
                continue

            records.append(build_record(
                company, ticker, cusip, country, exchange,
                status, closed_for, reason, close_dt, open_dt,
                source="Citi", source_url=source_url
            ))
    return records


# ── MANUAL PARSERS ────────────────────────────────────────────────────────────

COLMAP_DB = {
    "company": ["company", "issuer", "name", "dr issuer"],
    "ticker":  ["ticker", "symbol", "dr symbol", "adr ticker"],
    "cusip":   ["cusip", "isin"],
    "country": ["country"],
    "exchange":["exchange"],
    "status":  ["status", "book status"],
    "closed_for": ["closed for", "closure type", "type"],
    "reason":  ["reason", "event type", "event"],
    "close_date": ["close date", "closed date", "books closed", "books close"],
    "open_date":  ["open date", "books open", "reopen date", "books reopen"],
}

COLMAP_JPM = {
    "company":  ["issuer name", "company", "name", "issuer"],
    "ticker":   ["symbol", "ticker", "adr symbol"],
    "cusip":    ["cusip", "isin"],
    "country":  ["country"],
    "exchange": ["exchange", "listed on"],
    "status":   ["status", "book status"],
    "closed_for": ["closed for", "type"],
    "reason":   ["reason", "event"],
    "close_date": ["close date", "books closed", "closure date"],
    "open_date":  ["open date", "books open", "reopened"],
}


def parse_pasted_table(raw: str, col_map: dict, source: str, source_url: str) -> list[dict]:
    """Parse TSV or CSV pasted from browser; auto-detect delimiter."""
    lines = [l for l in raw.strip().split("\n") if l.strip()]
    if not lines:
        return []
    delim = "\t" if "\t" in lines[0] else ","
    headers = [h.strip().lower().strip('"') for h in lines[0].split(delim)]

    def find_col(targets):
        for t in targets:
            if t in headers:
                return headers.index(t)
        return None

    idx = {field: find_col(targets) for field, targets in col_map.items()}

    records = []
    for line in lines[1:]:
        vals = [v.strip().strip('"') for v in line.split(delim)]
        if not vals or all(v == "" for v in vals):
            continue

        def g(field, default=""):
            i = idx.get(field)
            return vals[i] if i is not None and i < len(vals) else default

        rec = build_record(
            company=g("company"), ticker=g("ticker"), cusip=g("cusip"),
            country=g("country"), exchange=g("exchange"),
            status=g("status"), closed_for=g("closed_for"),
            reason=g("reason"), close_date=g("close_date"),
            open_date=g("open_date"), source=source, source_url=source_url,
        )
        if rec["company"]:
            records.append(rec)
    return records


# ── SESSION STATE ─────────────────────────────────────────────────────────────

DATA_FILE = Path("data/adr_books.json")


@st.cache_data(ttl=300)   # re-check file every 5 min
def load_from_file() -> dict:
    """
    Primary data source: data/adr_books.json written by scraper.py
    (and auto-updated by GitHub Actions every weekday morning).
    Returns the full payload dict, or empty structure if file missing.
    """
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {"records": [], "scraped_at": None,
            "source_status": {}, "errors": []}


if "records" not in st.session_state:
    payload = load_from_file()
    st.session_state.records = payload.get("records", [])
    st.session_state.last_refresh = payload.get("scraped_at", None)
    st.session_state.sources_online = payload.get("source_status",
        {"Citi": False, "Deutsche Bank": False, "J.P. Morgan": False})
    st.session_state.file_errors = payload.get("errors", [])
if "citi_loaded" not in st.session_state:
    st.session_state.citi_loaded = bool(st.session_state.records)


def merge_into_state(new_records: list[dict]):
    """Add new_records to state, re-deduplicate entire set."""
    combined = st.session_state.records + new_records
    st.session_state.records = deduplicate(combined)


# ── SIDEBAR ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 ADR Books Monitor")
    st.caption("Cantor Fitzgerald — Internal Tool")
    st.divider()

    # ── Citi fetch
    st.markdown("### 🔵 Citi (Live)")
    if st.button("↺ Fetch Citi Data (A–Z)", use_container_width=True):
        with st.spinner("Fetching Citi A–Z pages… ~30s"):
            fetch_citi_all.clear()        # force fresh fetch
            recs, errs = fetch_citi_all()
        if recs:
            # Replace existing Citi records, keep manual ones
            st.session_state.records = [
                r for r in st.session_state.records if r["source"] != "Citi"
            ]
            merge_into_state(recs)
            st.session_state.sources_online["Citi"] = True
            st.session_state.citi_loaded = True
            st.session_state.last_refresh = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.success(f"✓ {len(recs)} Citi records loaded")
        else:
            st.error("Citi fetch failed — check network / site availability")
        for e in errs[:5]:
            st.warning(e)

    st.divider()

    # ── Deutsche Bank paste
    st.markdown("### 🟢 Deutsche Bank (Manual)")
    st.caption(
        "Go to [adr.db.com → Books Open/Close](https://adr.db.com/drwebrebrand/dr-universe/books-open-close), "
        "select-all the table, copy, paste below."
    )
    db_paste = st.text_area("Paste DB table here (TSV or CSV)", height=120, key="db_paste")
    if st.button("Import Deutsche Bank Data", use_container_width=True):
        if db_paste.strip():
            recs = parse_pasted_table(
                db_paste, COLMAP_DB,
                source="Deutsche Bank",
                source_url="https://adr.db.com/drwebrebrand/dr-universe/books-open-close"
            )
            if recs:
                st.session_state.records = [
                    r for r in st.session_state.records if r["source"] != "Deutsche Bank"
                ]
                merge_into_state(recs)
                st.session_state.sources_online["Deutsche Bank"] = True
                st.success(f"✓ {len(recs)} DB records imported")
            else:
                st.warning("No records parsed — check paste format (headers required)")
        else:
            st.warning("Nothing pasted")

    st.divider()

    # ── J.P. Morgan paste
    st.markdown("### 🟡 J.P. Morgan (Manual)")
    st.caption(
        "Go to [adr.com → Book Closures](https://adr.com/dr/drdirectory/bookClosures), "
        "copy the table, paste below."
    )
    jpm_paste = st.text_area("Paste JPM table here (TSV or CSV)", height=120, key="jpm_paste")
    if st.button("Import J.P. Morgan Data", use_container_width=True):
        if jpm_paste.strip():
            recs = parse_pasted_table(
                jpm_paste, COLMAP_JPM,
                source="J.P. Morgan",
                source_url="https://adr.com/dr/drdirectory/bookClosures"
            )
            if recs:
                st.session_state.records = [
                    r for r in st.session_state.records if r["source"] != "J.P. Morgan"
                ]
                merge_into_state(recs)
                st.session_state.sources_online["J.P. Morgan"] = True
                st.success(f"✓ {len(recs)} JPM records imported")
            else:
                st.warning("No records parsed — check paste format (headers required)")
        else:
            st.warning("Nothing pasted")

    st.divider()

    # ── Excel / CSV upload
    st.markdown("### 📁 Excel / CSV Upload")
    uploaded = st.file_uploader("Upload .csv exported from Excel workbook", type=["csv"])
    if uploaded:
        try:
            df_up = pd.read_csv(uploaded)
            df_up.columns = [c.strip().lower() for c in df_up.columns]
            recs = parse_pasted_table(
                df_up.to_csv(index=False), COLMAP_DB,
                source="Excel Upload",
                source_url=""
            )
            if recs:
                merge_into_state(recs)
                st.success(f"✓ {len(recs)} records imported from CSV")
        except Exception as e:
            st.error(f"Parse error: {e}")

    st.divider()
    if st.session_state.last_refresh:
        st.caption(f"Last refresh: {st.session_state.last_refresh}")

    online = sum(st.session_state.sources_online.values())
    st.caption(f"Sources online: {online}/3")


# ── MAIN PAGE ─────────────────────────────────────────────────────────────────

st.markdown("# 📊 ADR Books Monitor")
st.caption("Active ADR books that are currently closed — consolidated from Citi, Deutsche Bank, J.P. Morgan")

# ── Stats cards
recs = st.session_state.records
n_closed = sum(1 for r in recs if r.get("actively_closed"))
n_tbd    = sum(1 for r in recs if r.get("derived_status") == "Closed"
               and str(r.get("open_date","")).upper() in ("TBD","","—"))
n_open   = sum(1 for r in recs if r.get("derived_status") == "Open")
n_total  = len(recs)
n_conflict = sum(1 for r in recs if r.get("conflict"))

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
    st.warning(f"⚠ {n_conflict} records have conflicting status across sources — review highlighted rows")

st.divider()

# ── Filters
if recs:
    df_all = pd.DataFrame(recs)

    col_f1, col_f2, col_f3, col_f4, col_f5 = st.columns([3, 2, 2, 2, 2])

    search = col_f1.text_input("🔍 Search company / ticker / CUSIP", "")
    statuses = ["All"] + sorted(df_all["derived_status"].dropna().unique().tolist())
    flt_status = col_f2.selectbox("Status", statuses)
    sources = ["All"] + sorted(df_all["source"].dropna().unique().tolist())
    flt_source = col_f3.selectbox("Source", sources)
    countries = ["All"] + sorted(df_all["country"].dropna().unique().tolist())
    flt_country = col_f4.selectbox("Country", countries)
    reasons = ["All"] + sorted(df_all["reason"].dropna().unique().tolist())
    flt_reason = col_f5.selectbox("Reason", reasons)

    # Apply filters
    df = df_all.copy()
    if search:
        mask = (
            df["company"].str.contains(search, case=False, na=False) |
            df["ticker"].str.contains(search, case=False, na=False) |
            df["cusip"].str.contains(search, case=False, na=False)
        )
        df = df[mask]
    if flt_status != "All":
        df = df[df["derived_status"] == flt_status]
    if flt_source != "All":
        df = df[df["source"] == flt_source]
    if flt_country != "All":
        df = df[df["country"] == flt_country]
    if flt_reason != "All":
        df = df[df["reason"] == flt_reason]

    # Sort: Closed first, then by close_date desc
    sort_order = {"Closed": 0, "Open": 1, "Unknown": 2}
    df["_sort"] = df["derived_status"].map(sort_order).fillna(3)
    df = df.sort_values(["_sort", "close_date"], ascending=[True, False]).drop(columns=["_sort"])

    st.caption(f"Showing **{len(df)}** of **{n_total}** records")

    # ── Display columns
    display_cols = [
        "company", "ticker", "cusip", "country", "exchange",
        "derived_status", "closed_for", "reason",
        "close_date", "open_date", "all_sources", "conflict_note",
    ]
    rename_map = {
        "company": "Company", "ticker": "Ticker", "cusip": "CUSIP",
        "country": "Country", "exchange": "Exchange",
        "derived_status": "Status", "closed_for": "Closed For",
        "reason": "Reason", "close_date": "Close Date",
        "open_date": "Open Date", "all_sources": "Source(s)",
        "conflict_note": "Conflict Note",
    }

    df_display = df[display_cols].rename(columns=rename_map)

    # Streamlit dataframe with column config
    st.dataframe(
        df_display,
        use_container_width=True,
        height=500,
        column_config={
            "Status": st.column_config.TextColumn("Status", width="small"),
            "Close Date": st.column_config.TextColumn("Close Date", width="medium"),
            "Open Date":  st.column_config.TextColumn("Open Date",  width="medium"),
            "Conflict Note": st.column_config.TextColumn("⚠ Conflict", width="large"),
        },
        hide_index=True,
    )

    # ── Conflicts section
    conflicts_df = df[df["conflict"] == True]
    if not conflicts_df.empty:
        with st.expander(f"⚠ {len(conflicts_df)} Conflicting Records — requires manual review"):
            st.dataframe(
                conflicts_df[display_cols].rename(columns=rename_map),
                use_container_width=True,
                hide_index=True,
            )

    # ── Actively closed focus view
    active_closed_df = df[df["actively_closed"] == True]
    if not active_closed_df.empty:
        with st.expander(f"🔴 {len(active_closed_df)} Actively Closed Books (no confirmed re-open date)"):
            st.dataframe(
                active_closed_df[display_cols].rename(columns=rename_map),
                use_container_width=True,
                hide_index=True,
            )

    st.divider()

    # ── Export
    col_e1, col_e2, _ = st.columns([2, 2, 6])

    csv_bytes = df_display.to_csv(index=False).encode("utf-8")
    col_e1.download_button(
        "⬇ Export CSV",
        data=csv_bytes,
        file_name=f"ADR_Books_Monitor_{date.today()}.csv",
        mime="text/csv",
        use_container_width=True,
    )

    excel_buf = io.BytesIO()
    with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
        df_display.to_excel(writer, index=False, sheet_name="ADR Books Monitor")
        if not conflicts_df.empty:
            conflicts_df[display_cols].rename(columns=rename_map).to_excel(
                writer, index=False, sheet_name="Conflicts"
            )
        active_closed_df[display_cols].rename(columns=rename_map).to_excel(
            writer, index=False, sheet_name="Actively Closed"
        )
    col_e2.download_button(
        "⬇ Export Excel (.xlsx)",
        data=excel_buf.getvalue(),
        file_name=f"ADR_Books_Monitor_{date.today()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

else:
    st.info(
        "No data loaded yet. Use the sidebar to:\n"
        "- Click **↺ Fetch Citi Data** to load live Citi records\n"
        "- Paste Deutsche Bank or J.P. Morgan table data\n"
        "- Upload a CSV exported from your Excel workbook"
    )

# ── DOCS EXPANDER ─────────────────────────────────────────────────────────────
with st.expander("📖 Documentation — Logic, Assumptions & Maintenance Guide"):
    st.markdown("""
### Business Logic (translated from Excel workbook)

| Rule | Implementation |
|------|---------------|
| **Dedupe key** | CUSIP (if ≥7 chars) → Ticker → Company name (first 25 chars) |
| **Actively Closed** | `Status = Closed` AND (`OpenDate = TBD` OR `OpenDate ≥ today`) |
| **Most-recent-wins** | On duplicate key, keep record with latest `CloseDate` |
| **Conflict flag** | Same dedupe key found in >1 source with different Status |
| **Reason normalisation** | `DIVIDEND DATE RECONCILIATION` → `Dividend Recon`; `CORPORATE ACTION` → `Corp Action` |
| **Missing data** | Never invented — shown as blank; TBD preserved as-is |

### Data Sources

| Source | Method | Frequency |
|--------|--------|-----------|
| Citi | Live HTTP scrape (BeautifulSoup) | On-demand via sidebar button |
| Deutsche Bank | Manual paste (robots.txt blocks scraping) | Each session |
| J.P. Morgan | Manual paste (JS-rendered SPA) | Each session |

### Known Limitations / Assumptions
- **Citi polite delay**: 0.3s between letter requests to avoid rate-limiting.
- **DB & JPM**: Cannot be auto-fetched. Future fix: scheduled Python script running server-side with Playwright.
- **CUSIP as primary key**: If Citi uses a different CUSIP format than DB/JPM, duplicates may not be caught — verify manually.
- **Excel workbook**: Logic was inferred from field names and ADR industry standards. If additional formula logic exists (e.g. VLOOKUP mapping tables, issuer whitelists), add them to `REASON_MAP` or the `classify_status()` function in `app.py`.

### How to Extend
- **Add a new data source**: Write a `fetch_xxx()` function following the pattern of `fetch_citi_all()` and call `merge_into_state()`.
- **Add a column**: Add the field in `build_record()`, include it in `display_cols`, and add to `rename_map`.
- **Change dedup logic**: Edit `normalise_key()`.
- **Adjust "actively closed" definition**: Edit `is_actively_closed()`.
- **Schedule auto-refresh**: Add `st_autorefresh` from `streamlit-autorefresh` package.
    """)
