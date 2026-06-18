"""
scraper.py
==========
Fetches ADR book closure data from all three sources and writes
the result to data/adr_books.json.

Run manually:  python scraper.py
Run via CI:    triggered by GitHub Actions on a schedule

Sources
-------
• Citi  → standard HTTP + BeautifulSoup (no JS needed)
• DB    → Playwright headless Chrome (JS-rendered + date filter)
• JPM   → Playwright headless Chrome (React SPA)
"""

import asyncio
import json
import time
import re
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── CONFIG ────────────────────────────────────────────────────────────────────
OUTPUT_FILE = Path("data/adr_books.json")
OUTPUT_FILE.parent.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

LETTERS = list("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
CITI_BASE = (
    "https://depositaryreceipts.citi.com/adr/guides/pgm_dispbook.aspx"
    "?pageId=5&subPageId=48&company="
)

REASON_MAP = {
    "DIVIDEND DATE RECONCILIATION": "Dividend Recon",
    "Dividend Date Reconciliation": "Dividend Recon",
    "CORPORATE ACTION": "Corp Action",
    "Corporate Action": "Corp Action",
    "OTHER": "Other",
    "Other": "Other",
}

TODAY = date.today()


# ── SHARED LOGIC ──────────────────────────────────────────────────────────────

def parse_date_safe(val: str):
    if not val or val.strip().upper() in ("TBD", "N/A", "—", ""):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y",
                "%b %d, %Y", "%d-%b-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(val.strip(), fmt).date()
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
    if row.get("derived_status") != "Closed":
        return False
    od = parse_date_safe(row.get("open_date", ""))
    return od is None or od >= TODAY


def normalise_key(row: dict) -> str:
    cusip = str(row.get("cusip", "")).strip().replace(" ", "")
    if len(cusip) >= 7:
        return f"CUSIP:{cusip}"
    ticker = str(row.get("ticker", "")).strip().upper()
    if len(ticker) >= 2:
        return f"TICK:{ticker}"
    return f"NAME:{str(row.get('company', '')).strip().upper()[:25]}"


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
        scraped_at=datetime.utcnow().isoformat(),
    )
    r["actively_closed"] = is_actively_closed(r)
    return r


def deduplicate(records: list) -> list:
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
                carry = {k2: ex[k2] for k2 in ("conflict", "conflict_note", "all_sources")}
                seen[k] = {**r.copy(), **carry}
    return list(seen.values())


# ── CITI ──────────────────────────────────────────────────────────────────────

def fetch_citi() -> tuple[list, list]:
    """Standard HTTP scrape — no JS needed."""
    records, errors = [], []
    for letter in LETTERS:
        url = CITI_BASE + letter
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            recs = _parse_citi_page(resp.text)
            records.extend(recs)
            print(f"  Citi [{letter}]: {len(recs)} records")
            time.sleep(0.3)
        except Exception as e:
            msg = f"Citi [{letter}]: {e}"
            errors.append(msg)
            print(f"  ⚠ {msg}")
    return records, errors


def _parse_citi_page(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    records = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        first_cells = rows[1].find_all("td") if len(rows) > 1 else []
        if len(first_cells) < 8:
            continue
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 10:
                continue
            a = cells[0].find("a")
            company = a.text.strip() if a else cells[0].text.strip()
            source_url = ""
            if a and a.get("href"):
                href = a["href"]
                source_url = (
                    "https://depositaryreceipts.citi.com" + href
                    if href.startswith("/") else href
                )
            ticker    = cells[2].text.strip()
            cusip     = cells[3].text.strip()
            country   = cells[4].text.strip()
            exchange  = cells[5].text.strip()
            status    = cells[6].text.strip()
            closed_for = cells[7].text.strip()
            reason    = cells[8].text.strip()
            close_dt  = cells[9].text.strip()
            open_dt   = cells[10].text.strip() if len(cells) > 10 else ""

            if not company or len(company) < 2:
                continue
            if ticker.lower() == "ticker" and company.lower() == "company":
                continue

            records.append(build_record(
                company, ticker, cusip, country, exchange,
                status, closed_for, reason, close_dt, open_dt,
                source="Citi", source_url=source_url,
            ))
    return records


# ── DEUTSCHE BANK ─────────────────────────────────────────────────────────────
# DB exposes a JSON REST API — no Playwright needed.
# Endpoint: POST https://adr.db.com/api/corporateactions/search
# actionTypeId=10 → Books Closed/Open
# Dates use epoch milliseconds; -2208970800000 is their sentinel for "not set"

DB_NULL_EPOCH = -2208970800000   # sentinel = 1900-01-01, means no date set
DB_API = "https://adr.db.com/api/corporateactions/search"
DB_PAGE_SIZE = 100


def _epoch_ms_to_date_str(ms) -> str:
    """Convert DB epoch-ms timestamp to YYYY-MM-DD string, or '' if null."""
    if ms is None or ms == DB_NULL_EPOCH:
        return ""
    try:
        return datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    except Exception:
        return ""


def fetch_db() -> tuple[list, list]:
    """
    Fetch all Books Closed/Open records from DB's JSON API.
    Iterates pages until all results retrieved.
    No Playwright required — pure HTTP POST.
    """
    records, errors = [], []

    # First fetch to get total pages
    page_num = 0
    total_pages = 1

    # Need CSRF token — get it from the page first
    csrf_token = ""
    try:
        resp = requests.get(
            "https://adr.db.com/drwebrebrand/dr-universe/books-open-close",
            headers=HEADERS, timeout=15
        )
        soup = BeautifulSoup(resp.text, "lxml")
        meta = soup.find("meta", attrs={"name": "_csrf"})
        if meta:
            csrf_token = meta.get("content", "")
            print(f"  DB: got CSRF token")
        else:
            print("  DB: no CSRF token found — trying without")
    except Exception as e:
        errors.append(f"DB CSRF fetch error: {e}")

    # Date range: 1 year back to 1 year forward
    date_from = (datetime(TODAY.year - 1, 1, 1)).strftime("%Y-%m-%d")
    date_to   = (datetime(TODAY.year + 1, 12, 31)).strftime("%Y-%m-%d")

    while page_num < total_pages:
        payload = {
            "page": page_num,
            "size": DB_PAGE_SIZE,
            "query": "",
            "actionTypeId": 10,
            "countryId": 0,
            "dateFrom": date_from,
            "dateTo": date_to,
            "exchange": "",
            "regionId": 0,
        }
        req_headers = {**HEADERS, "Content-Type": "application/json"}
        if csrf_token:
            req_headers["X-CSRF-TOKEN"] = csrf_token

        try:
            resp = requests.post(
                f"{DB_API}?page={page_num}&size={DB_PAGE_SIZE}",
                json=payload,
                headers=req_headers,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()

            total_pages = data.get("numberOfPages", 1)
            results = data.get("results", [])
            recs = _parse_db_api(results)
            records.extend(recs)
            print(f"  DB page {page_num+1}/{total_pages}: {len(recs)} records")
            page_num += 1
            time.sleep(0.3)

        except Exception as e:
            errors.append(f"DB API page {page_num} error: {e}")
            print(f"  ⚠ DB error: {e}")
            break

    return records, errors


def _parse_db_api(results: list) -> list:
    """Parse DB JSON API results into standard records."""
    records = []
    for item in results:
        company = item.get("companyName", "").strip()
        if not company:
            continue

        close_date = _epoch_ms_to_date_str(item.get("offerOpenDate"))   # books CLOSE on offerOpenDate
        open_date  = _epoch_ms_to_date_str(item.get("offerCloseDate"))  # books REOPEN on offerCloseDate

        # Determine status: if open_date is empty or in future → Closed; else Open
        status = "Closed"
        if open_date:
            od = parse_date_safe(open_date)
            if od and od < TODAY:
                status = "Open"

        records.append(build_record(
            company=company,
            ticker=item.get("drSymbol", ""),
            cusip=item.get("cusip", ""),
            country=item.get("countryName", ""),
            exchange="",
            status=status,
            closed_for="Issuance & Cancellation",
            reason=item.get("terms", "Book Close/Open"),
            close_date=close_date,
            open_date=open_date if open_date else "TBD",
            source="Deutsche Bank",
            source_url="https://adr.db.com/drwebrebrand/dr-universe/books-open-close",
        ))
    return records


# ── J.P. MORGAN ───────────────────────────────────────────────────────────────

async def fetch_jpm(page) -> tuple[list, list]:
    """
    JPM adr.com is a React SPA. Strategy:
      1. Navigate and wait for JS to render
      2. Intercept XHR/fetch API calls to capture JSON directly (most reliable)
      3. Fall back to DOM scraping if no API call found
    """
    url = "https://adr.com/dr/drdirectory/bookClosures"
    records, errors = [], []
    api_data = []

    # ── Intercept network responses for JSON data ─────────────────────────────
    async def handle_response(response):
        try:
            ct = response.headers.get("content-type", "")
            if "json" in ct and response.status == 200:
                url_r = response.url
                # JPM API likely at /api/ endpoint
                if any(k in url_r for k in ["/api/", "book", "closure", "directory"]):
                    body = await response.json()
                    api_data.append(body)
        except Exception:
            pass

    page.on("response", handle_response)

    try:
        print("  JPM: navigating...")
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)   # let React render

        # ── Check if we got API data via intercept ────────────────────────────
        if api_data:
            print(f"  JPM: intercepted {len(api_data)} API response(s)")
            for payload in api_data:
                recs = _parse_jpm_api(payload)
                records.extend(recs)
            if records:
                print(f"  JPM: {len(records)} records from API intercept")
                return records, errors

        # ── Fall back: DOM scraping ───────────────────────────────────────────
        print("  JPM: falling back to DOM scraping")
        table_selectors = [
            "table",
            "[class*='table']",
            "[class*='grid']",
            "[role='grid']",
            "[class*='DataTable']",
        ]
        for sel in table_selectors:
            try:
                await page.wait_for_selector(sel, timeout=8000)
                break
            except PWTimeout:
                pass

        # Handle pagination
        page_num = 0
        while True:
            page_num += 1
            html = await page.content()
            recs = _parse_jpm_html(html)
            records.extend(recs)
            print(f"  JPM page {page_num}: {len(recs)} records")

            next_selectors = [
                'button:has-text("Next")',
                '[aria-label="Next"]',
                '[aria-label="Next page"]',
                'button[class*="next"]:not([disabled])',
            ]
            clicked = False
            for sel in next_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_enabled():
                        await btn.click()
                        await page.wait_for_timeout(2500)
                        clicked = True
                        break
                except Exception:
                    pass
            if not clicked:
                break

    except Exception as e:
        errors.append(f"JPM fetch error: {e}")
        print(f"  ⚠ JPM error: {e}")

    return records, errors


def _parse_jpm_api(payload) -> list:
    """
    Parse JPM JSON API response. Structure unknown — defensive multi-path.
    Common patterns: list of dicts, or {"data": [...], "items": [...]}
    """
    records = []
    rows = []

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "items", "results", "bookClosures", "records"):
            if key in payload and isinstance(payload[key], list):
                rows = payload[key]
                break

    for row in rows:
        if not isinstance(row, dict):
            continue

        def g(*keys):
            for k in keys:
                # Try exact, then case-insensitive
                if k in row:
                    return str(row[k]).strip()
                for rk in row:
                    if rk.lower() == k.lower():
                        return str(row[rk]).strip()
            return ""

        company = g("issuerName", "company", "name", "issuer")
        if not company:
            continue

        records.append(build_record(
            company=company,
            ticker=g("symbol", "ticker", "adrSymbol", "drSymbol"),
            cusip=g("cusip", "isin"),
            country=g("country", "countryName"),
            exchange=g("exchange", "listedOn"),
            status=g("bookStatus", "status"),
            closed_for=g("closureType", "closedFor"),
            reason=g("reason", "eventType", "event"),
            close_date=g("closeDate", "bookCloseDate", "closedDate"),
            open_date=g("openDate", "bookOpenDate", "reopenDate"),
            source="J.P. Morgan",
            source_url="https://adr.com/dr/drdirectory/bookClosures",
        ))
    return records


def _parse_jpm_html(html: str) -> list:
    """DOM fallback for JPM — same defensive approach as DB."""
    soup = BeautifulSoup(html, "lxml")
    records = []

    for tbl in soup.find_all("table"):
        headers_el = tbl.find_all("th")
        if not headers_el:
            continue
        headers = [h.text.strip().lower() for h in headers_el]

        def col(candidates):
            for c in candidates:
                for i, h in enumerate(headers):
                    if c in h:
                        return i
            return None

        idx_company = col(["issuer", "company", "name"])
        idx_ticker  = col(["symbol", "ticker"])
        idx_country = col(["country"])
        idx_status  = col(["status"])
        idx_close   = col(["close"])
        idx_open    = col(["open"])
        idx_reason  = col(["reason", "event"])

        if idx_company is None:
            continue

        for row in tbl.find_all("tr")[1:]:
            cells = row.find_all("td")
            if not cells:
                continue

            def g(idx, default=""):
                if idx is None or idx >= len(cells):
                    return default
                return cells[idx].text.strip()

            company = g(idx_company)
            if not company or len(company) < 2:
                continue

            records.append(build_record(
                company=company,
                ticker=g(idx_ticker),
                cusip="",
                country=g(idx_country),
                exchange="",
                status=g(idx_status),
                closed_for="",
                reason=g(idx_reason),
                close_date=g(idx_close),
                open_date=g(idx_open),
                source="J.P. Morgan",
                source_url="https://adr.com/dr/drdirectory/bookClosures",
            ))
    return records


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print(f"ADR Books Scraper — {datetime.utcnow().isoformat()} UTC")
    print("=" * 55)

    all_records = []
    all_errors  = []
    source_status = {}

    # ── Citi (no Playwright needed) ───────────────────────────────────────────
    print("\n[1/3] Fetching Citi...")
    citi_recs, citi_errs = fetch_citi()
    all_records.extend(citi_recs)
    all_errors.extend(citi_errs)
    source_status["Citi"] = len(citi_recs) > 0
    print(f"  → {len(citi_recs)} records, {len(citi_errs)} errors")

    # ── Deutsche Bank (pure HTTP API — no Playwright needed) ─────────────────
    print("\n[2/3] Fetching Deutsche Bank...")
    db_recs, db_errs = fetch_db()
    all_records.extend(db_recs)
    all_errors.extend(db_errs)
    source_status["Deutsche Bank"] = len(db_recs) > 0
    print(f"  → {len(db_recs)} records, {len(db_errs)} errors")

    # ── J.P. Morgan (still needs Playwright — React SPA) ─────────────────────
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=HEADERS["User-Agent"],
            java_script_enabled=True,
        )

        print("\n[3/3] Fetching J.P. Morgan...")
        jpm_page = await ctx.new_page()
        jpm_recs, jpm_errs = await fetch_jpm(jpm_page)
        await jpm_page.close()
        all_records.extend(jpm_recs)
        all_errors.extend(jpm_errs)
        source_status["J.P. Morgan"] = len(jpm_recs) > 0
        print(f"  → {len(jpm_recs)} records, {len(jpm_errs)} errors")

        await browser.close()

    # ── Deduplicate ────────────────────────────────────────────────────────────
    print(f"\nDeduplicating {len(all_records)} raw records...")
    deduped = deduplicate(all_records)
    print(f"→ {len(deduped)} unique records after dedup")

    # ── Write output ──────────────────────────────────────────────────────────
    output = {
        "scraped_at": datetime.utcnow().isoformat() + "Z",
        "source_status": source_status,
        "errors": all_errors,
        "record_count": len(deduped),
        "records": deduped,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))
    print(f"\n✓ Written to {OUTPUT_FILE}")
    print(f"  Citi: {source_status.get('Citi')}")
    print(f"  DB:   {source_status.get('Deutsche Bank')}")
    print(f"  JPM:  {source_status.get('J.P. Morgan')}")
    if all_errors:
        print(f"\n⚠ {len(all_errors)} errors:")
        for e in all_errors:
            print(f"  {e}")


if __name__ == "__main__":
    asyncio.run(main())
