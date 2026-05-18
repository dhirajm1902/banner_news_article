"""
WARN Act Unified Scraper
========================
Add new states by implementing a scrape_<state>() function and
registering it in SCRAPERS at the bottom of the file.

Each scraper must return a pd.DataFrame with at least these columns
(add state-specific columns freely — they'll be preserved):
    state, company, city, notice_date, layoff_date,
    employees_affected, closure_type, notes
"""

import asyncio
import sys
import time
import logging
import requests
import pandas as pd
from datetime import date
from pathlib import Path
from io import StringIO

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("warn_scraper")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

def _run_async(coro):
    """
    Run an async coroutine safely from both plain Python scripts and
    Jupyter notebooks (which already have a running event loop).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import nest_asyncio
        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    else:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        return asyncio.run(coro)


OUTPUT_COLS = [
    "state", "company", "city", "notice_date", "layoff_date",
    "employees_affected", "closure_type", "notes",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _filter_from_2025(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows whose notice_date is before 2025. Rows with unparseable dates are kept."""
    if df.empty or "notice_date" not in df.columns:
        return df
    parsed = pd.to_datetime(df["notice_date"], errors="coerce")
    keep = parsed.isna() | (parsed.dt.year >= 2025)
    return df[keep].reset_index(drop=True)


def _normalise(df: pd.DataFrame, state: str) -> pd.DataFrame:
    """Ensure every required column exists, state is set, and rows are 2025+."""
    df = df.copy()
    df["state"] = state
    for col in OUTPUT_COLS:
        if col not in df.columns:
            df[col] = ""
    extras = [c for c in df.columns if c not in OUTPUT_COLS]
    return _filter_from_2025(df[OUTPUT_COLS + extras])


# ── State scrapers ────────────────────────────────────────────────────────────

async def _scrape_alabama_async() -> list[dict]:
    data = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://workforce.alabama.gov/warn-list/")
        await page.wait_for_selector('tr.fw-warn-list__items[data-year="2026"]', timeout=15000)

        rows = await page.query_selector_all(
            'tr.fw-warn-list__items[data-year="2025"], tr.fw-warn-list__items[data-year="2026"]'
        )
        for row in rows:
            status = await row.query_selector('td[data-label="Closing or Layoff"]')
            if not status:
                continue
            status_text = (await status.inner_text()).strip().lower()
            if status_text != "closure":
                continue

            async def _txt(label):
                el = await row.query_selector(f'td[data-label="{label}"]')
                return (await el.inner_text()).strip() if el else ""

            data.append({
                "company":            await _txt("Company"),
                "city":               await _txt("City"),
                "notice_date":        await _txt("Initial Report Date"),
                "layoff_date":        await _txt("Planned Starting Date"),
                "employees_affected": await _txt("Planned # of Affected Employees"),
                "closure_type":       "closure",
                "notes":              "",
            })
        await browser.close()
    return data


def scrape_alabama() -> pd.DataFrame:
    """
    Scrapes Alabama WARN list (2026, closure events only) via async Playwright.
    URL: https://workforce.alabama.gov/warn-list/
    """
    log.info("Scraping Alabama...")
    data = _run_async(_scrape_alabama_async())
    df = pd.DataFrame(data)
    log.info(f"  Alabama: {len(df)} rows")
    return _normalise(df, "Alabama")


def scrape_alaska() -> pd.DataFrame:
    """
    Scrapes Alaska WARN notices via requests + BeautifulSoup.
    URL: https://jobs.alaska.gov/rr/WARN_notices.htm
    """
    log.info("Scraping Alaska...")
    resp = requests.get("https://jobs.alaska.gov/rr/WARN_notices.htm", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    data = []
    for row in soup.find_all("tr")[1:]:   # [1:] skips the single header row
        cols = row.find_all("td")
        if len(cols) < 6:
            continue
        data.append({
            "company":            cols[0].get_text(strip=True),
            "city":               cols[1].get_text(strip=True),
            "notice_date":        cols[2].get_text(strip=True),
            "layoff_date":        cols[3].get_text(strip=True),
            "employees_affected": cols[4].get_text(strip=True),
            "closure_type":       "",
            "notes":              cols[5].get_text(strip=True),
        })

    df = pd.DataFrame(data)
    log.info(f"  Alaska: {len(df)} rows")
    return _normalise(df, "Alaska")


async def _scrape_dc_async() -> list[dict]:
    """
    Parse DC WARN table by scraping <tr>/<td> directly via Playwright,
    bypassing pd.read_html entirely.
    Fetches both current year and previous year pages to get full data.
    """
    current_year = date.today().year
    URLS = [
        f"https://does.dc.gov/page/industry-closings-and-layoffs-warn-notifications-{current_year}",
        f"https://does.dc.gov/page/industry-closings-and-layoffs-warn-notifications-{current_year - 1}",
    ]
    SELECTOR = ".field-name-body table, .field-items table, article table"

    col_map = {
        "organization name": "company",
        "company name":      "company",
        "company":           "company",
        "notice date":       "notice_date",
        "date":              "notice_date",
        "effective layoff date": "layoff_date",
        "layoff date":       "layoff_date",
        "number toemployees affected": "employees_affected",
        "number of employees affected": "employees_affected",
        "employees affected": "employees_affected",
        "# employees":       "employees_affected",
        "affected":          "employees_affected",
        "code type":         "closure_type",
        "type":              "closure_type",
        "location":          "city",
        "city":              "city",
    }

    all_records = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for url in URLS:
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_selector(SELECTOR, timeout=20000)

                rows_data = await page.eval_on_selector(
                    SELECTOR,
                    """tbl => {
                        const rows = Array.from(tbl.querySelectorAll('tr'));
                        return rows.map(r =>
                            Array.from(r.querySelectorAll('th, td'))
                                .map(c => c.innerText.trim())
                        );
                    }"""
                )
                await page.close()

                if not rows_data:
                    continue

                header_idx = next(
                    (i for i, r in enumerate(rows_data) if sum(bool(c) for c in r) >= 3),
                    0
                )
                headers = [h.strip() for h in rows_data[header_idx]]
                data_rows = rows_data[header_idx + 1:]
                norm_headers = [col_map.get(h.lower(), h.lower()) for h in headers]

                for row in data_rows:
                    if not any(row):
                        continue
                    padded = row + [""] * (len(norm_headers) - len(row))
                    all_records.append(dict(zip(norm_headers, padded)))

            except Exception as exc:
                log.warning(f"  DC: failed to scrape {url} — {exc}")

        await browser.close()

    return all_records


def scrape_dc() -> pd.DataFrame:
    """
    Scrapes Washington DC WARN notifications via async Playwright.
    URL: https://does.dc.gov/page/industry-closings-and-layoffs-warn-notifications-2025
    """
    log.info("Scraping DC...")
    records = _run_async(_scrape_dc_async())
    raw = pd.DataFrame(records)
    raw.dropna(how="all", inplace=True)
    raw = raw[raw.apply(lambda r: r.astype(str).str.strip().ne("").any(), axis=1)]

    log.info(f"  DC: {len(raw)} rows")
    return _normalise(raw, "DC")


def scrape_washington() -> pd.DataFrame:
    """
    Scrapes Washington State WARN notices via ASP.NET postback pagination.
    URL: https://fortress.wa.gov/esd/file/WARN/Public/SearchWARN.aspx
    """
    log.info("Scraping Washington...")
    URL = "https://fortress.wa.gov/esd/file/WARN/Public/SearchWARN.aspx"
    session = requests.Session()

    def _get_payload(soup):
        return {i.get("name"): i.get("value", "") for i in soup.select("input") if i.get("name")}

    def _extract_rows(soup):
        rows = []
        for r in soup.select("#ucPSW_gvMain tr"):
            cols = [c.get_text(strip=True) for c in r.select("td")]
            if len(cols) >= 7 and not cols[0].isdigit() and cols[0] not in ("Company", ""):
                rows.append(cols[:7])
        return rows

    res = session.get(URL, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(res.text, "html.parser")
    payload = _get_payload(soup)
    payload["ucPSW$btnSearchCompany"] = "Search"

    res = session.post(URL, data=payload, timeout=15)
    soup = BeautifulSoup(res.text, "html.parser")

    data = []
    page = 1

    while True:
        log.info(f"  Washington page {page}")
        rows = _extract_rows(soup)
        if not rows:
            log.info("  No rows found, stopping.")
            break
        data.extend(rows)

        pager_links = soup.select("#ucPSW_gvMain a")
        page_numbers = [a.get_text(strip=True) for a in pager_links if a.get_text(strip=True).isdigit()]
        next_page = page + 1

        if str(next_page) not in page_numbers:
            log.info(f"  No more pages after page {page}.")
            break

        payload = _get_payload(soup)
        payload["__EVENTTARGET"] = "ucPSW$gvMain"
        payload["__EVENTARGUMENT"] = f"Page${next_page}"
        payload.pop("ucPSW$btnSearchCompany", None)

        res = session.post(URL, data=payload, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")
        page += 1

    raw_cols = ["company", "city", "layoff_date", "employees_affected", "closure_type", "type_detail", "notice_date"]
    df = pd.DataFrame(data, columns=raw_cols)
    log.info(f"  Washington: {len(df)} rows")
    return _normalise(df, "Washington")


# ── Washington standalone runner (mirrors original script behaviour) ──────────

def run_washington_standalone(
    output_dir: str = ".",
    max_pages: int | None = None,
) -> pd.DataFrame:
    """
    Reproduces the original standalone Washington script exactly:
    scrapes all pages (or up to max_pages), prints progress, saves a
    dated CSV, and returns the raw DataFrame with the original column names.
    """
    URL = "https://fortress.wa.gov/esd/file/WARN/Public/SearchWARN.aspx"
    session = requests.Session()

    def _get_payload(soup):
        return {i.get("name"): i.get("value", "") for i in soup.select("input") if i.get("name")}

    def _extract_rows(soup):
        rows = []
        for r in soup.select("#ucPSW_gvMain tr"):
            cols = [c.get_text(strip=True) for c in r.select("td")]
            if len(cols) >= 7 and not cols[0].isdigit() and cols[0] not in ("Company", ""):
                rows.append(cols[:7])
        return rows

    res = session.get(URL, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(res.text, "html.parser")
    payload = _get_payload(soup)
    payload["ucPSW$btnSearchCompany"] = "Search"

    res = session.post(URL, data=payload, timeout=15)
    soup = BeautifulSoup(res.text, "html.parser")

    data = []
    page = 1

    while True:
        print(f"Scraping page: {page}")

        if max_pages and page > max_pages:
            print(f"Reached maximum pages ({max_pages}), stopping.")
            break

        rows = _extract_rows(soup)
        if not rows:
            print("No rows found, stopping.")
            break
        data.extend(rows)

        pager_links = soup.select("#ucPSW_gvMain a")
        page_numbers = [a.get_text(strip=True) for a in pager_links if a.get_text(strip=True).isdigit()]
        next_page = page + 1

        if str(next_page) not in page_numbers:
            print(f"No more pages after page {page}.")
            break

        payload = _get_payload(soup)
        payload["__EVENTTARGET"] = "ucPSW$gvMain"
        payload["__EVENTARGUMENT"] = f"Page${next_page}"
        payload.pop("ucPSW$btnSearchCompany", None)

        res = session.post(URL, data=payload, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")
        page += 1

    columns = ["Company", "Location", "Layoff Start Date", "# Workers", "Closure/Layoff", "Type", "Received Date"]
    df = pd.DataFrame(data, columns=columns)

    print(df.head())
    print("Total rows:", len(df))

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"warn_layoffs_{date.today().strftime('%Y-%m-%d')}.csv"
    df.to_csv(csv_path, index=False)
    print(f"Saved to {csv_path}")

    return df


def scrape_maryland() -> pd.DataFrame:
    """
    Scrapes Maryland WARN/ESA table via requests + pd.read_html.
    URL: https://labor.maryland.gov/employment/warn.shtml
    """
    log.info("Scraping Maryland...")
    resp = requests.get("https://labor.maryland.gov/employment/warn.shtml", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table")
    if not table:
        raise RuntimeError("Maryland: no table found on page.")

    raw = pd.read_html(StringIO(str(table)), header=0)[0]
    raw.columns = [c.strip().replace("\n", " ") for c in raw.columns]
    raw.dropna(how="all", inplace=True)
    raw = raw.loc[:, raw.columns.notna()]

    col_map = {
        "Company Name":           "company",
        "Company":                "company",
        "City":                   "city",
        "Location":               "city",
        "Notice Date":            "notice_date",
        "Effective Date":         "layoff_date",
        "Layoff Date":            "layoff_date",
        "Number of Employees":    "employees_affected",
        "Employees Affected":     "employees_affected",
        "Type":                   "closure_type",
        "Notes":                  "notes",
    }
    raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns}, inplace=True)

    log.info(f"  Maryland: {len(raw)} rows")
    return _normalise(raw, "Maryland")


# ── Vermont ───────────────────────────────────────────────────────────────────

_VT_RESULTS_URL = (
    "https://www.vermontjoblink.com/search/warn_lookups"
    "?commit=Search"
    "&q%5Bemployer_name_cont%5D="
    "&q%5Bmain_contact_contact_info_addresses_full_location_city_matches%5D="
    "&q%5Bnotice_eq%5D=true"
    "&q%5Bnotice_on_gteq%5D="
    "&q%5Bnotice_on_lteq%5D="
    "&q%5Bservice_delivery_area_id_eq%5D="
    "&q%5Bzipcode_code_start%5D="
)

_VT_COLUMNS = ["Employer", "City", "ZIP", "LWIB Area", "Notice Date", "WARN Type"]


def _vt_parse_rows(soup: BeautifulSoup) -> list[dict]:
    import re
    table = soup.find("table", {"id": re.compile(r"^a11y_table_")})
    if not table:
        return []
    tbody = table.find("tbody")
    if not tbody:
        return []

    rows = []
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip() for td in tds]
        while len(cells) < len(_VT_COLUMNS):
            cells.append("")
        row = dict(zip(_VT_COLUMNS, cells[: len(_VT_COLUMNS)]))
        if row["Employer"]:
            rows.append(row)
    return rows


def _vt_find_next_url(soup: BeautifulSoup, current_url: str) -> str | None:
    import re
    from urllib.parse import urljoin

    nav = soup.find("div", class_=re.compile(r"pagination", re.I)) or \
          soup.find("nav", attrs={"aria-label": re.compile(r"Page controls", re.I)})
    if not nav:
        return None

    a = nav.find("a", attrs={"rel": "next"}) or \
        nav.find("a", class_=re.compile(r"next_page", re.I))
    if a and a.get("href"):
        return urljoin(current_url, a["href"])

    for link in nav.find_all("a", href=True):
        if "next" in link.get_text(" ", strip=True).lower():
            return urljoin(current_url, link["href"])
    return None


def scrape_vermont(max_pages: int | None = None) -> pd.DataFrame:
    """
    Scrapes Vermont WARN notices via requests + BeautifulSoup with pagination.
    URL: https://www.vermontjoblink.com/search/warn_lookups/new
    """
    log.info("Scraping Vermont...")
    session = requests.Session()
    session.headers.update(HEADERS)

    resp = session.get(_VT_RESULTS_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    current_url = resp.url

    all_rows: list[dict] = []
    seen: set[tuple] = set()
    page_num = 1

    while True:
        rows = _vt_parse_rows(soup)
        for r in rows:
            key = tuple(r.get(col, "") for col in _VT_COLUMNS)
            if key not in seen:
                seen.add(key)
                all_rows.append(r)

        log.info(f"  Vermont page {page_num}: {len(rows)} rows | total: {len(all_rows)}")

        if max_pages and page_num >= max_pages:
            break

        next_url = _vt_find_next_url(soup, current_url)
        if not next_url:
            break

        time.sleep(1.0)
        resp = session.get(next_url, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        current_url = resp.url
        page_num += 1

    raw = pd.DataFrame(all_rows)
    if raw.empty:
        raw = pd.DataFrame(columns=_VT_COLUMNS)

    # Map Vermont columns → standard schema
    raw.rename(columns={
        "Employer":    "company",
        "City":        "city",
        "Notice Date": "notice_date",
        "WARN Type":   "closure_type",
        "ZIP":         "zip",
        "LWIB Area":   "lwib_area",
    }, inplace=True)

    raw["layoff_date"] = ""
    raw["employees_affected"] = ""
    raw["notes"] = ""

    log.info(f"  Vermont: {len(raw)} rows total")
    return _normalise(raw, "Vermont")


# ── Texas ─────────────────────────────────────────────────────────────────────

_TX_CSV_URL = (
    "https://data.texas.gov/api/views/8w53-c4f6/rows.csv?accessType=DOWNLOAD"
)


def scrape_texas() -> pd.DataFrame:
    """
    Downloads the full Texas WARN dataset directly from the Socrata open-data API.
    URL: https://data.texas.gov/dataset/Worker-Adjustment-and-Retraining-Notification-WARN/8w53-c4f6
    """
    log.info("Scraping Texas (direct CSV download)...")
    resp = requests.get(_TX_CSV_URL, headers=HEADERS, timeout=120)
    resp.raise_for_status()

    raw = pd.read_csv(StringIO(resp.text), low_memory=False)
    raw.columns = [c.strip() for c in raw.columns]
    log.info(f"  Texas raw columns: {raw.columns.tolist()}")

    # Actual Texas Socrata CSV uses ALL_CAPS_UNDERSCORE column names.
    # Map both the real names and common title-case variants as fallback.
    col_map = {
        # company — actual Socrata name
        "JOB_SITE_NAME":            "company",
        # city
        "CITY_NAME":                "city",
        "City":                     "city",
        # notice date
        "NOTICE_DATE":              "notice_date",
        "Notice Date":              "notice_date",
        # layoff / effective date
        "LayOff_Date":              "layoff_date",
        "LAYOFF_DATE":              "layoff_date",
        "Layoff Date":              "layoff_date",
        "Effective Date":           "layoff_date",
        # employees
        "TOTAL_LAYOFF_NUMBER":      "employees_affected",
        "# Employees Affected":     "employees_affected",
        "Employees Affected":       "employees_affected",
        "Number of Affected Workers": "employees_affected",
        # closure type (Texas dataset has no dedicated type column; leave blank)
        "Type of Layoff/Closure":   "closure_type",
        "Type of Layoff":           "closure_type",
        "Closure/Layoff":           "closure_type",
        # notes
        "Notes":                    "notes",
        "Comments":                 "notes",
    }
    raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns}, inplace=True)

    log.info(f"  Texas: {len(raw)} rows")
    return _normalise(raw, "Texas")


# ── Virginia ──────────────────────────────────────────────────────────────────

async def _scrape_virginia_async(max_pages: int | None = None) -> list[dict]:
    import re

    URL = "https://virginiaworks.gov/im-an-employer/retain-and-grow/warn-notices/"
    REQUEST_DELAY_MS = 800

    def _split_company_address(first_td_html: str) -> tuple[str, str]:
        """Split the first cell into company name and address."""
        from bs4 import BeautifulSoup as BS
        td = BS(first_td_html, "html.parser")
        text = td.get_text("\n", strip=True)
        parts = [re.sub(r"\s+", " ", p).strip() for p in text.split("\n") if p.strip()]
        if not parts:
            return "", ""
        return parts[0], ", ".join(parts[1:]) if len(parts) > 1 else ""

    def _parse_table(html: str) -> list[dict]:
        from bs4 import BeautifulSoup as BS
        soup = BS(html, "html.parser")
        table = soup.find("table", {"id": "warn-notice-table"})
        if not table:
            return []
        tbody = table.find("tbody")
        if not tbody:
            return []

        rows = []
        for tr in tbody.find_all("tr", recursive=False):
            tds = tr.find_all("td", recursive=False)
            if not tds:
                continue

            company, address = _split_company_address(str(tds[0]))
            other = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip() for td in tds[1:]]
            while len(other) < 7:
                other.append("")

            row = {
                "company":            company,
                "address":            address,
                "notice_date":        other[0],
                "layoff_date":        other[1],
                "employees_affected": other[2],
                "city":               other[3],
                "contact_person":     other[4],
                "closure_type":       other[5],
                "collective_bargaining_unit": other[6],
            }
            if row["company"]:
                rows.append(row)
        return rows

    all_rows: list[dict] = []
    seen: set[tuple] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 1200})

        log.info(f"  Virginia: loading {URL}")
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_selector("#warn-notice-table tbody tr", timeout=60000)

        page_num = 1
        while True:
            html = await page.content()
            rows = _parse_table(html)

            added = 0
            for r in rows:
                key = (r["company"], r["notice_date"], r["layoff_date"])
                if key not in seen:
                    seen.add(key)
                    all_rows.append(r)
                    added += 1

            log.info(f"  Virginia page {page_num}: {len(rows)} rows | new: {added} | total: {len(all_rows)}")

            if max_pages and page_num >= max_pages:
                break

            next_btn = page.locator('button.dt-paging-button.next[aria-label="Next"]')
            if await next_btn.count() == 0:
                break
            aria_disabled = await next_btn.first.get_attribute("aria-disabled")
            if aria_disabled == "true":
                break

            prev_first = rows[0]["company"] if rows else ""
            await next_btn.first.click()
            await page.wait_for_timeout(REQUEST_DELAY_MS)
            await page.wait_for_load_state("networkidle")

            try:
                await page.wait_for_function(
                    """(prev) => {
                        const cell = document.querySelector('#warn-notice-table tbody tr td');
                        return cell && cell.textContent.trim() !== prev;
                    }""",
                    arg=prev_first,
                    timeout=15000,
                )
            except Exception:
                pass

            page_num += 1

        await browser.close()

    return all_rows


def scrape_virginia(max_pages: int | None = None) -> pd.DataFrame:
    """
    Scrapes Virginia WARN notices via async Playwright (DataTables pagination).
    URL: https://virginiaworks.gov/im-an-employer/retain-and-grow/warn-notices/
    """
    log.info("Scraping Virginia...")
    data = _run_async(_scrape_virginia_async(max_pages=max_pages))
    df = pd.DataFrame(data) if data else pd.DataFrame()
    log.info(f"  Virginia: {len(df)} rows")
    return _normalise(df, "Virginia")


# ── Ohio ─────────────────────────────────────────────────────────────────────
# Uses Selenium (lazy imports so the rest of warn.py works without selenium installed).

_OH_URLS = {
    "2025": (
        "https://jfs.ohio.gov/job-services-and-unemployment/"
        "job-services/job-programs-and-services/submit-a-warn-notice/"
        "2025-Public-Notice-of-Layoffs-and-Closures"
    ),
    "2026": (
        "https://jfs.ohio.gov/job-services-and-unemployment/"
        "job-services/job-programs-and-services/submit-a-warn-notice/"
        "current-public-notices-of-layoffs-and-closures-sa"
    ),
}
_OH_TABLE_SELS = (
    "table#js-table-visualization",
    "table.dataTable",
    "table",
)
_OH_ROW_SELS  = tuple(f"{s} tbody tr" for s in _OH_TABLE_SELS)
_OH_NEXT_SELS = (
    "button.dt-paging-button.next",
    "a.paginate_button.next",
    "button[aria-label='Next']",
    "a[aria-label='Next']",
    "#js-table-visualization_next",
)


def _oh_build_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1600,1200")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
    )
    return driver


def _oh_count_rows(driver) -> int:
    from selenium.webdriver.common.by import By
    for sel in _OH_ROW_SELS:
        try:
            n = len(driver.find_elements(By.CSS_SELECTOR, sel))
            if n:
                return n
        except Exception:
            pass
    return 0


def _oh_switch_iframe(driver) -> bool:
    from selenium.webdriver.common.by import By
    driver.switch_to.default_content()
    for frame in driver.find_elements(By.TAG_NAME, "iframe"):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            if driver.find_elements(By.CSS_SELECTOR, "table"):
                return True
        except Exception:
            continue
    driver.switch_to.default_content()
    return False


def _oh_find_table(driver, timeout=30):
    from selenium.webdriver.common.by import By
    end = time.time() + timeout
    while time.time() < end:
        for sel in _OH_TABLE_SELS:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed():
                        return el
                if els and sel == "table#js-table-visualization":
                    return els[0]
            except Exception:
                pass
        time.sleep(0.3)
    return None


def _oh_set_page_length(driver) -> None:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC

    for sel in ("select#dt-length-0", "select[name$='_length']", "select[aria-controls]"):
        try:
            el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
            s = Select(el)
            values = [o.get_attribute("value") for o in s.options]
            for target in ("-1", "100"):
                if target in values:
                    s.select_by_value(target)
                    time.sleep(2.5)
                    return
            s.select_by_index(len(s.options) - 1)
            time.sleep(2.5)
            return
        except Exception:
            continue


def _oh_rows_from_embedded_json(driver, url: str) -> list[dict]:
    import json as _json
    src = driver.page_source or ""
    start = src.find('{"data":')
    if start < 0:
        return []
    depth, in_str, esc, end = 0, False, False, None
    for i in range(start, len(src)):
        ch = src[i]
        if in_str:
            esc = (ch == "\\") and not esc
            if not esc and ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if not end:
        return []
    try:
        payload = _json.loads(src[start:end])
    except Exception:
        return []
    data = payload.get("data") or []
    if len(data) < 3:
        return []
    rows = []
    for rec in data[2:]:
        if not isinstance(rec, list) or len(rec) < 9:
            continue
        company = str(rec[0] or "").strip()
        if not company:
            continue
        phone = str(rec[7] or "").strip()
        union = str(rec[8] or "").strip()
        notes_parts = [f"Phone: {phone}" if phone else "", f"Union: {union}" if union else ""]
        rows.append({
            "company":            company,
            "notice_date":        str(rec[1] or "").strip(),
            "city":               str(rec[3] or "").strip(),
            "closure_type":       str(rec[4] or "").strip(),
            "employees_affected": str(rec[5] or "").strip(),
            "layoff_date":        str(rec[6] or "").strip(),
            "notes":              " | ".join(p for p in notes_parts if p),
        })
    return rows


def _oh_rows_from_page(driver, url: str) -> list[dict]:
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

    trs = []
    for sel in _OH_ROW_SELS:
        try:
            trs = driver.find_elements(By.CSS_SELECTOR, sel)
            if trs:
                break
        except Exception:
            pass

    rows = []
    for tr in trs:
        try:
            tds = tr.find_elements(By.TAG_NAME, "td")
            if len(tds) < 2:
                continue
            company = (tds[0].text or "").strip()
            if not company or company.lower() in ("company", "employer"):
                continue

            pdf_url = ""
            try:
                a = tds[0].find_element(By.CSS_SELECTOR, "a[href]")
                pdf_url = (a.get_attribute("href") or "").strip()
            except NoSuchElementException:
                pass

            def cell(i):
                return (tds[i].text or "").strip() if i < len(tds) else ""

            phone, union = cell(6), cell(7)
            notes_parts = [
                f"Phone: {phone}" if phone else "",
                f"Union: {union}" if union else "",
                f"PDF: {pdf_url}" if pdf_url else "",
            ]
            rows.append({
                "company":            company,
                "notice_date":        cell(1),
                "city":               cell(2),
                "closure_type":       cell(3),
                "employees_affected": cell(4),
                "layoff_date":        cell(5),
                "notes":              " | ".join(p for p in notes_parts if p),
            })
        except StaleElementReferenceException:
            continue
        except Exception:
            continue
    return rows


def _oh_click_next(driver) -> bool:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait

    before = _oh_count_rows(driver)
    for sel in _OH_NEXT_SELS:
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            btn = next((b for b in btns if b.is_displayed()), None)
            if not btn:
                continue
            cls = (btn.get_attribute("class") or "").lower()
            if "disabled" in cls or btn.get_attribute("aria-disabled") == "true":
                continue
            driver.execute_script("arguments[0].click();", btn)
            try:
                WebDriverWait(driver, 15).until(lambda d: _oh_count_rows(d) != before)
            except Exception:
                time.sleep(2.5)
            time.sleep(1.0)
            return True
        except Exception:
            continue
    return False


def _oh_scrape_year(driver, year: str, url: str) -> list[dict]:
    from selenium.webdriver.support.ui import WebDriverWait

    log.info(f"  Ohio [{year}] loading {url}")
    driver.switch_to.default_content()
    driver.get(url)
    try:
        WebDriverWait(driver, 45).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except Exception:
        pass
    time.sleep(5.0 if year == "2025" else 3.0)

    _oh_switch_iframe(driver)
    table = _oh_find_table(driver, timeout=30)
    if not table:
        driver.refresh()
        time.sleep(5.0)
        _oh_switch_iframe(driver)
        table = _oh_find_table(driver, timeout=30)

    if not table:
        fallback = _oh_rows_from_embedded_json(driver, url)
        if fallback:
            log.info(f"  Ohio [{year}] {len(fallback)} rows from embedded JSON fallback")
            return fallback
        log.warning(f"  Ohio [{year}] no table found, skipping")
        return []

    _oh_set_page_length(driver)
    time.sleep(1.0)

    all_rows, seen, page_no = [], set(), 1
    while True:
        for r in _oh_rows_from_page(driver, url):
            key = (r["company"], r["notice_date"], r["city"])
            if key not in seen:
                seen.add(key)
                all_rows.append(r)
        log.info(f"  Ohio [{year}] page {page_no}: {len(all_rows)} total")
        if not _oh_click_next(driver):
            break
        page_no += 1
        if page_no > 100:
            log.warning(f"  Ohio [{year}] safety-stopped at 100 pages")
            break

    return all_rows


def scrape_ohio() -> pd.DataFrame:
    """
    Scrapes Ohio WARN notices for 2025 and 2026 via Selenium.
    2025: https://jfs.ohio.gov/.../2025-Public-Notice-of-Layoffs-and-Closures
    2026: https://jfs.ohio.gov/.../current-public-notices-of-layoffs-and-closures-sa
    """
    log.info("Scraping Ohio...")
    driver = None
    try:
        driver = _oh_build_driver()
        rows: list[dict] = []
        for year, url in _OH_URLS.items():
            rows.extend(_oh_scrape_year(driver, year, url))
    except Exception as e:
        log.error(f"  Ohio scraping failed: {e}")
        rows = []
    finally:
        if driver:
            driver.quit()

    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    log.info(f"  Ohio: {len(df)} rows")
    return _normalise(df, "Ohio")


# ── Oregon ───────────────────────────────────────────────────────────────────
# Pure requests + BeautifulSoup — no Selenium needed.
# Flow: GET form → grab hidden tokens → POST form → parse download link → GET xlsx.

_OR_BASE_URL     = "https://ccwd.hecc.oregon.gov"
_OR_DOWNLOAD_URL = f"{_OR_BASE_URL}/Layoff/WARN/Download"
_OR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": _OR_DOWNLOAD_URL,
}


def _or_get_form_tokens(session: requests.Session) -> dict:
    r = session.get(_OR_DOWNLOAD_URL, headers=_OR_HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return {
        inp.get("name"): inp.get("value", "")
        for inp in soup.find_all("input", {"type": "hidden"})
        if inp.get("name")
    }


def _or_submit_form(session: requests.Session, tokens: dict) -> BeautifulSoup:
    form_data = {**tokens, "WARNFormat": "xlsx", "WARNSort": "NoticeDate"}
    r = session.post(
        _OR_DOWNLOAD_URL,
        data=form_data,
        headers={**_OR_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
        allow_redirects=True,
    )
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def _or_find_download_link(soup: BeautifulSoup) -> str | None:
    import re as _re
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _re.search(r"/Layoff/Reports/WARN.*\.(xlsx|xls|csv)", href, _re.I):
            return (_OR_BASE_URL + href) if href.startswith("/") else href
    m = _re.search(r'href="(/Layoff/Reports/WARN[^"]+\.(xlsx|xls|csv))"', str(soup), _re.I)
    if m:
        return _OR_BASE_URL + m.group(1)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "WARN" in href.upper() and _re.search(r"\.(xlsx|xls|csv)$", href, _re.I):
            return (_OR_BASE_URL + href) if href.startswith("/") else href
    return None


def scrape_oregon() -> pd.DataFrame:
    """
    Downloads Oregon WARN data via form POST (no Selenium required).
    URL: https://ccwd.hecc.oregon.gov/Layoff/WARN/Download
    """
    import re as _re
    from io import BytesIO

    log.info("Scraping Oregon...")
    session = requests.Session()
    try:
        tokens  = _or_get_form_tokens(session)
        soup    = _or_submit_form(session, tokens)
        dl_link = _or_find_download_link(soup)
        if not dl_link:
            log.warning("  Oregon: download link not found in response")
            return _normalise(pd.DataFrame(), "Oregon")

        log.info(f"  Oregon: downloading {dl_link}")
        r = session.get(dl_link, headers=_OR_HEADERS, timeout=120)
        r.raise_for_status()

        ext_m = _re.search(r"\.(xlsx|xls|csv)$", dl_link, _re.I)
        ext   = ext_m.group(0).lower() if ext_m else ".xlsx"
        raw   = (pd.read_excel(BytesIO(r.content), engine="openpyxl")
                 if ext in (".xlsx", ".xls")
                 else pd.read_csv(BytesIO(r.content)))

    except Exception as e:
        log.error(f"  Oregon scraping failed: {e}")
        return _normalise(pd.DataFrame(), "Oregon")

    raw.columns = [str(c).strip() for c in raw.columns]
    log.info(f"  Oregon raw columns: {raw.columns.tolist()}")

    col_map = {
        "Employer":            "company",
        "Company":             "company",
        "Company Name":        "company",
        "Firm Name":           "company",
        "City":                "city",
        "Location":            "city",
        "Notice Date":         "notice_date",
        "Received Date":       "notice_date",
        "Layoff Date":         "layoff_date",
        "Effective Date":      "layoff_date",
        "Employees Affected":  "employees_affected",
        "Number of Employees": "employees_affected",
        "Workers Affected":    "employees_affected",
        "# Employees":         "employees_affected",
        "Type":                "closure_type",
        "Closure Type":        "closure_type",
        "Layoff/Closure":      "closure_type",
        "Notice Type":         "closure_type",
        "Notes":               "notes",
        "Comments":            "notes",
    }
    raw.rename(columns={k: v for k, v in col_map.items() if k in raw.columns}, inplace=True)

    log.info(f"  Oregon: {len(raw)} rows")
    return _normalise(raw, "Oregon")


# ── Oklahoma ─────────────────────────────────────────────────────────────────
# Salesforce LWC table — reads data-cell-value attributes to bypass Shadow DOM.
# Uses Selenium (lazy imports).

_OK_URL       = "https://www.employoklahoma.gov/Participants/s/warnnotices"
_OK_BOOT_WAIT = 50   # seconds for Salesforce LWC to render
_OK_PAGE_WAIT = 8    # seconds after clicking Next
_OK_COLUMNS   = ["Employer", "City", "Zip Code", "Local Workforce Board", "Notice Date", "Notice Type"]
_OK_COL_KEY_MAP = {
    "OESC_Employer_Name__c":             "Employer",
    "OESC_Employer_City__c":             "City",
    "OESC_Employer_Zip_Code__c":         "Zip Code",
    "Select_Local_Workforce_Board__c":   "Local Workforce Board",
    "Launchpad__Notice_Date__c":         "Notice Date",
    "Launchpad__Layoff_Closure_Type__c": "Notice Type",
}


def _ok_build_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1600,1000")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
    )
    return driver


def _ok_wait_for_table(driver, timeout=_OK_BOOT_WAIT) -> bool:
    import re as _re
    from selenium.webdriver.common.by import By
    deadline = time.time() + timeout
    attempt  = 0
    while time.time() < deadline:
        rows  = driver.find_elements(By.CSS_SELECTOR, "tr[data-row-number]")
        cells = driver.find_elements(By.CSS_SELECTOR, "tr[data-row-number] [data-cell-value]")
        if rows and cells:
            log.info(f"  Oklahoma: table found after ~{attempt*2}s ({len(rows)} rows visible)")
            time.sleep(1.5)
            return True
        attempt += 1
        time.sleep(2)
    return False


def _ok_get_page_info(driver) -> str:
    from selenium.webdriver.common.by import By
    for xpath in (
        "//span[contains(text(),'Displaying') and contains(text(),'Total Records')]",
        "//span[contains(text(),'Page') and contains(text(),'of')]",
    ):
        try:
            spans = driver.find_elements(By.XPATH, xpath)
            if spans:
                return spans[0].text.strip()
        except Exception:
            pass
    return ""


def _ok_get_total_pages(driver) -> int:
    import re as _re
    from selenium.webdriver.common.by import By
    try:
        for s in driver.find_elements(By.XPATH,
                "//span[contains(text(),'Page') and contains(text(),'of')]"):
            m = _re.search(r"Page\s+\d+\s+of\s+(\d+)", s.text)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return 99


def _ok_extract_rows(driver) -> list[dict]:
    import re as _re
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import StaleElementReferenceException

    rows = []
    for tr in driver.find_elements(By.CSS_SELECTOR, "tr[data-row-number]"):
        row = {col: "" for col in _OK_COLUMNS}
        try:
            for cell in tr.find_elements(By.CSS_SELECTOR, "[data-label]"):
                label   = cell.get_attribute("data-label") or ""
                value   = (cell.get_attribute("data-cell-value") or "").strip()
                if label in _OK_COLUMNS:
                    row[label] = value
                else:
                    col_key = cell.get_attribute("data-col-key-value") or ""
                    for prefix, col_name in _OK_COL_KEY_MAP.items():
                        if col_key.startswith(prefix):
                            row[col_name] = value
                            break
            if not row.get("Employer"):
                continue
            nd = row.get("Notice Date", "")
            if _re.match(r"\d{4}-\d{2}-\d{2}", nd):
                p = nd.split("-")
                row["Notice Date"] = f"{int(p[1])}/{int(p[2])}/{p[0]}"
            rows.append(row)
        except StaleElementReferenceException:
            continue
        except Exception:
            continue
    return rows


def _ok_is_next_disabled(driver) -> bool:
    from selenium.webdriver.common.by import By
    try:
        btns = driver.find_elements(By.XPATH, "//button[contains(normalize-space(.),'Next')]")
        for btn in btns:
            if (btn.get_attribute("aria-disabled") or "").lower() == "true":
                return True
        return not bool(btns)
    except Exception:
        return True


def _ok_click_next(driver) -> bool:
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import NoSuchElementException
    try:
        for btn in driver.find_elements(By.XPATH,
                "//button[contains(normalize-space(.),'Next')]"):
            if (btn.get_attribute("aria-disabled") or "").lower() == "true":
                return False
            if btn.is_displayed():
                driver.execute_script("arguments[0].scrollIntoView(true);", btn)
                driver.execute_script("arguments[0].click();", btn)
                return True
    except (NoSuchElementException, Exception):
        pass
    return False


def _ok_wait_page_advance(driver, prev_info: str, timeout=15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ok_get_page_info(driver) != prev_info:
            return True
        time.sleep(1)
    return False


def scrape_oklahoma() -> pd.DataFrame:
    """
    Scrapes Oklahoma WARN notices via Selenium.
    Reads data-cell-value attributes to bypass the Salesforce LWC Shadow DOM.
    URL: https://www.employoklahoma.gov/Participants/s/warnnotices
    """
    log.info("Scraping Oklahoma...")
    driver = None
    try:
        driver = _ok_build_driver()
        driver.get(_OK_URL)

        if not _ok_wait_for_table(driver):
            log.warning("  Oklahoma: table did not render, returning empty")
            return _normalise(pd.DataFrame(), "Oklahoma")

        total_pages = _ok_get_total_pages(driver)
        log.info(f"  Oklahoma: {total_pages} page(s) detected")

        all_rows, seen, page_no, prev_snap = [], set(), 1, None

        while True:
            rows      = _ok_extract_rows(driver)
            page_info = _ok_get_page_info(driver)

            snap = str([r.get("Employer", "") for r in rows[:3]])
            if snap == prev_snap and page_no > 1:
                log.warning(f"  Oklahoma page {page_no}: stall detected, stopping")
                break
            prev_snap = snap

            for r in rows:
                key = (r["Employer"], r["Notice Date"], r["City"])
                if key not in seen:
                    seen.add(key)
                    all_rows.append(r)

            log.info(f"  Oklahoma page {page_no}/{total_pages}: {len(all_rows)} total | {page_info}")

            if page_no >= total_pages or _ok_is_next_disabled(driver):
                break
            if not _ok_click_next(driver):
                break
            if not _ok_wait_page_advance(driver, page_info):
                log.warning("  Oklahoma: page did not advance, stopping")
                break
            time.sleep(_OK_PAGE_WAIT)
            page_no += 1

    except Exception as e:
        log.error(f"  Oklahoma scraping failed: {e}")
        all_rows = []
    finally:
        if driver:
            driver.quit()

    raw = pd.DataFrame(all_rows) if all_rows else pd.DataFrame()
    if not raw.empty:
        raw.rename(columns={
            "Employer":              "company",
            "City":                  "city",
            "Notice Date":           "notice_date",
            "Notice Type":           "closure_type",
            "Local Workforce Board": "notes",
            "Zip Code":              "zip",
        }, inplace=True)

    log.info(f"  Oklahoma: {len(raw)} rows")
    return _normalise(raw, "Oklahoma")


# ── Pennsylvania ──────────────────────────────────────────────────────────────
# JS-rendered nested accordions: Year → Month → Company → panel text.
# Uses Selenium (lazy imports).

_PA_URL = (
    "https://www.pa.gov/agencies/dli/programs-services/"
    "workforce-development-home/warn-requirements/warn-notices"
)
_PA_YEARS = ["2025", "2026"]


def _pa_build_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,900")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    svc = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=svc, options=opts)


def _pa_safe_click(driver, element) -> bool:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        time.sleep(0.3)
        driver.execute_script("arguments[0].click();", element)
        return True
    except Exception:
        return False


def _pa_expand_if_collapsed(driver, btn) -> bool:
    from selenium.common.exceptions import StaleElementReferenceException
    try:
        if (btn.get_attribute("aria-expanded") or "false").lower() == "false":
            _pa_safe_click(driver, btn)
            time.sleep(0.5)
        return True
    except StaleElementReferenceException:
        return False


def _pa_parse_panel_text(raw_text: str) -> dict:
    lines = [l.strip() for l in raw_text.strip().splitlines() if l.strip()]
    result = {"address": "", "county": "", "employees_affected": "",
              "notice_date": "", "closure_type": ""}
    address_lines = []
    for line in lines:
        lu = line.upper()
        if "COUNTY:" in lu:
            result["county"] = re.sub(r"COUNTY\s*:\s*", "", line, flags=re.I).strip()
        elif "AFFECTED" in lu:
            result["employees_affected"] = re.sub(r"#?\s*AFFECTED\s*:\s*", "", line, flags=re.I).strip()
        elif "EFFECTIVE DATE" in lu:
            result["notice_date"] = re.sub(r"EFFECTIVE DATE\s*:\s*", "", line, flags=re.I).strip()
        elif "CLOSURE OR LAYOFF" in lu or "CLOSING OR LAYOFF" in lu:
            result["closure_type"] = re.sub(
                r"(CLOSURE OR LAYOFF|CLOSING OR LAYOFF)\s*:\s*", "", line, flags=re.I
            ).strip()
        elif not any(k in lu for k in ["COUNTY", "AFFECTED", "EFFECTIVE", "CLOSURE", "CLOSING"]):
            if not result["county"]:
                address_lines.append(line)
    result["address"] = " ".join(address_lines).strip()
    return result


def _pa_scrape_year(driver, year: str) -> list[dict]:
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import NoSuchElementException

    all_rows = []
    year_headings = driver.find_elements(
        By.XPATH,
        f"//h2[contains(@class,'cmp-accordion__main-heading') and normalize-space(text())='{year}']"
    )
    if not year_headings:
        log.warning(f"  Pennsylvania [{year}]: year heading not found")
        return all_rows

    year_section = driver.execute_script("""
        var headings = document.querySelectorAll(
            'h2.cmp-accordion__main-heading--large, h2.cmp-accordion__main-heading');
        for (var i = 0; i < headings.length; i++) {
            if (headings[i].textContent.trim() === arguments[0]) {
                var el = headings[i].parentElement;
                while (el && !el.querySelector('.cmp-accordion__button')) {
                    el = el.nextElementSibling || el.parentElement.nextElementSibling;
                    if (!el) break;
                }
                return el;
            }
        }
        return null;
    """, year)

    if not year_section:
        log.warning(f"  Pennsylvania [{year}]: accordion section not found")
        return all_rows

    # Month-level buttons
    month_btns = year_section.find_elements(
        By.CSS_SELECTOR,
        ".cmp-accordion > .cmp-accordion__item > .cmp-accordion__header > .cmp-accordion__button"
    )
    if not month_btns:
        month_btns = year_section.find_elements(
            By.XPATH,
            ".//button[contains(@class,'cmp-accordion__button')]"
            "[.//span[contains(@class,'cmp-accordion__title') and ("
            "normalize-space(text())='January' or normalize-space(text())='February' or "
            "normalize-space(text())='March' or normalize-space(text())='April' or "
            "normalize-space(text())='May' or normalize-space(text())='June' or "
            "normalize-space(text())='July' or normalize-space(text())='August' or "
            "normalize-space(text())='September' or normalize-space(text())='October' or "
            "normalize-space(text())='November' or normalize-space(text())='December'"
            ")]]"
        )

    log.info(f"  Pennsylvania [{year}]: {len(month_btns)} month(s)")

    for month_btn in month_btns:
        try:
            month_name = month_btn.find_element(By.CSS_SELECTOR, ".cmp-accordion__title").text.strip()
        except Exception:
            month_name = "Unknown"

        _pa_expand_if_collapsed(driver, month_btn)
        time.sleep(0.8)

        panel_id = month_btn.get_attribute("aria-controls")
        try:
            month_panel = (driver.find_element(By.ID, panel_id) if panel_id
                           else driver.execute_script(
                               "return arguments[0].closest('.cmp-accordion__item')"
                               ".querySelector('.cmp-accordion__panel');", month_btn))
        except NoSuchElementException:
            continue
        if not month_panel:
            continue

        company_btns = month_panel.find_elements(By.CSS_SELECTOR, ".cmp-accordion__button")

        for comp_btn in company_btns:
            try:
                company_name = comp_btn.find_element(By.CSS_SELECTOR, ".cmp-accordion__title").text.strip()
            except Exception:
                company_name = comp_btn.text.strip().split("\n")[0].strip()
            if not company_name:
                continue

            _pa_expand_if_collapsed(driver, comp_btn)
            time.sleep(0.3)

            comp_panel_id = comp_btn.get_attribute("aria-controls")
            try:
                comp_panel = (driver.find_element(By.ID, comp_panel_id) if comp_panel_id
                              else driver.execute_script(
                                  "return arguments[0].closest('.cmp-accordion__item')"
                                  ".querySelector('.cmp-accordion__panel');", comp_btn))
            except NoSuchElementException:
                comp_panel = None
            if not comp_panel:
                continue

            try:
                raw_text = comp_panel.text.strip()
                if not raw_text:
                    soup = BeautifulSoup(comp_panel.get_attribute("innerHTML"), "html.parser")
                    raw_text = soup.get_text(separator="\n", strip=True)
            except Exception:
                raw_text = ""
            if not raw_text:
                continue

            parsed = _pa_parse_panel_text(raw_text)
            all_rows.append({
                "company":            company_name,
                "city":               parsed["address"],
                "notice_date":        parsed["notice_date"],
                "layoff_date":        parsed["notice_date"],
                "employees_affected": parsed["employees_affected"],
                "closure_type":       parsed["closure_type"],
                "notes":              f"County: {parsed['county']}" if parsed["county"] else "",
            })

    log.info(f"  Pennsylvania [{year}]: {len(all_rows)} rows")
    return all_rows


def scrape_pennsylvania() -> pd.DataFrame:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException

    log.info("Scraping Pennsylvania...")
    driver = _pa_build_driver()
    rows: list[dict] = []
    try:
        driver.get(_PA_URL)
        try:
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".cmp-accordion__button"))
            )
            time.sleep(2)
        except TimeoutException:
            log.error("  Pennsylvania: accordion buttons did not appear")
            return _normalise(pd.DataFrame(), "Pennsylvania")

        for year in _PA_YEARS:
            try:
                rows.extend(_pa_scrape_year(driver, year))
            except Exception as exc:
                log.error(f"  Pennsylvania [{year}]: {exc}")
    finally:
        driver.quit()

    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    log.info(f"  Pennsylvania: {len(df)} rows total")
    return _normalise(df, "Pennsylvania")


# ── South Dakota ──────────────────────────────────────────────────────────────
# Pure requests + BeautifulSoup — no Selenium needed.
# URL: https://dlr.sd.gov/workforce_services/businesses/warn_notices.aspx

_SD_URL      = "https://dlr.sd.gov/workforce_services/businesses/warn_notices.aspx"
_SD_BASE_URL = "https://dlr.sd.gov"


def _sd_resolve_pdf(href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _SD_BASE_URL + href
    return _SD_BASE_URL + "/workforce_services/businesses/" + href


def scrape_south_dakota() -> pd.DataFrame:
    """
    Scrapes South Dakota WARN notices via requests + BeautifulSoup.
    URL: https://dlr.sd.gov/workforce_services/businesses/warn_notices.aspx
    """
    log.info("Scraping South Dakota...")
    resp = requests.get(_SD_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "lxml")

    table = soup.find("table", {"bordercolor": "003366"}) or soup.find("table")
    if not table:
        log.warning("  South Dakota: WARN table not found")
        return _normalise(pd.DataFrame(), "South Dakota")

    trs = table.find("tbody").find_all("tr") if table.find("tbody") else table.find_all("tr")
    rows = []
    for tr in trs:
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        if tds[0].get("bgcolor") == "#e7e7e7":
            continue
        if "tablecolumnheadings" in tds[0].get("class", []):
            continue

        link = tds[0].find("a")
        company = (link or tds[0]).get_text(separator=" ", strip=True)
        pdf_url = _sd_resolve_pdf(link.get("href", "") if link else "")

        if not company:
            continue
        rows.append({
            "company":            company,
            "city":               tds[1].get_text(separator=" ", strip=True),
            "notice_date":        tds[2].get_text(separator=" ", strip=True),
            "employees_affected": tds[3].get_text(separator=" ", strip=True),
            "closure_type":       "",
            "notes":              pdf_url,
        })

    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    log.info(f"  South Dakota: {len(df)} rows")
    return _normalise(df, "South Dakota")


# ── Tennessee ─────────────────────────────────────────────────────────────────
# Selenium preferred (JS-rendered accordion tables) with requests fallback.
# URL: https://www.tn.gov/workforce/.../reports.html

_TN_URL = (
    "https://www.tn.gov/workforce/general-resources/major-publications0/"
    "major-publications-redirect/reports.html"
)


def _tn_norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _tn_is_warn_table(table) -> bool:
    headers = [_tn_norm(th.get_text(" ", strip=True)).lower() for th in table.find_all("th")]
    text = " | ".join(headers)
    return bool(headers) and all(k in text for k in ["date", "company", "county"])


def _tn_infer_year(table, default="2026") -> str:
    for node in table.find_all_previous(
        ["h1","h2","h3","h4","h5","h6","button","a","span","div"], limit=30
    ):
        m = re.search(r"\b(20\d{2})\b", _tn_norm(node.get_text(" ", strip=True)))
        if m:
            return m.group(1)
    return default


def _tn_parse_table(table, year: str) -> list[dict]:
    rows = []
    tbody = table.find("tbody")
    if not tbody:
        return rows
    for tr in tbody.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 6:
            continue
        link    = tds[1].find("a", href=True)
        company = _tn_norm(tds[1].get_text(" ", strip=True))
        cells   = [_tn_norm(td.get_text(" ", strip=True)) for td in tds]
        if not company:
            continue
        from urllib.parse import urljoin
        rows.append({
            "company":            company,
            "city":               cells[2],   # County
            "notice_date":        cells[0],   # Date of Posting
            "layoff_date":        cells[4],   # Closure/Layoff Date
            "employees_affected": cells[3],   # Affected Workers
            "closure_type":       cells[5],   # Notice/Type
            "notes":              urljoin("https://www.tn.gov", link["href"]) if link else "",
        })
    return rows


def _tn_fetch_soup() -> BeautifulSoup:
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
        try:
            driver.get(_TN_URL)
            WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            try:
                for btn in driver.find_elements(
                    By.CSS_SELECTOR, "button[data-tn-action='accordion:expandcollapse']"
                ):
                    aria = (btn.get_attribute("aria-label") or "").lower()
                    cls  = (btn.get_attribute("class") or "").lower()
                    if "expand" in aria or "icon-plus" in cls:
                        driver.execute_script("arguments[0].click();", btn)
                        time.sleep(1.0)
                for btn in driver.find_elements(
                    By.CSS_SELECTOR, "button[aria-expanded='false'][aria-controls*='collapse']"
                ):
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.2)
            except Exception:
                pass
            WebDriverWait(driver, 15).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tr"))
            )
            time.sleep(1.5)
            html = driver.page_source
        finally:
            driver.quit()
        return BeautifulSoup(html, "html.parser")

    except Exception as exc:
        log.warning(f"  Tennessee: Selenium unavailable ({exc}), falling back to requests")
        resp = requests.get(_TN_URL, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")


def scrape_tennessee() -> pd.DataFrame:
    """
    Scrapes Tennessee WARN notices (all years) from JS-rendered accordion tables.
    URL: https://www.tn.gov/workforce/.../reports.html
    """
    log.info("Scraping Tennessee...")
    try:
        soup = _tn_fetch_soup()
    except Exception as exc:
        log.error(f"  Tennessee: failed to fetch page — {exc}")
        return _normalise(pd.DataFrame(), "Tennessee")

    rows: list[dict] = []
    seen: set[tuple] = set()
    table_count = 0

    for table in soup.find_all("table"):
        if not _tn_is_warn_table(table):
            continue
        year = _tn_infer_year(table, default="2026")
        for r in _tn_parse_table(table, year):
            key = (r["company"], r["notice_date"], r["city"])
            if key not in seen:
                seen.add(key)
                rows.append(r)
        table_count += 1

    df = pd.DataFrame(rows) if rows else pd.DataFrame()
    log.info(f"  Tennessee: {len(df)} rows from {table_count} table(s)")
    return _normalise(df, "Tennessee")


# ── Registry ──────────────────────────────────────────────────────────────────
# To add a new state: implement scrape_<state>() above and add it here.

SCRAPERS: dict[str, callable] = {
    "Alabama":    scrape_alabama,
    "Alaska":     scrape_alaska,
    "DC":         scrape_dc,
    "Maryland":   scrape_maryland,
    "Ohio":         scrape_ohio,
    "Oklahoma":     scrape_oklahoma,
    "Oregon":         scrape_oregon,
    "Pennsylvania":   scrape_pennsylvania,
    "South Dakota":   scrape_south_dakota,
    "Tennessee":      scrape_tennessee,
    "Texas":          scrape_texas,
    "Vermont":    scrape_vermont,
    "Virginia":   scrape_virginia,
    "Washington": scrape_washington,
    # "Arizona": scrape_arizona,   ← add new states here
}


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all(
    states: list[str] | None = None,
    output_dir: str = ".",
    combined_filename: str | None = None,
) -> pd.DataFrame:
    """
    Run scrapers for all (or selected) states, save individual CSVs,
    and write a combined CSV ready for website import.

    Args:
        states:            List of state keys to run (default: all).
        output_dir:        Folder to write CSVs into.
        combined_filename: Override the combined CSV filename.

    Returns:
        Combined DataFrame.
    """
    today = date.today().strftime("%Y-%m-%d")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    targets = states or list(SCRAPERS.keys())
    frames: list[pd.DataFrame] = []

    for name in targets:
        if name not in SCRAPERS:
            log.warning(f"No scraper registered for '{name}' — skipping.")
            continue
        try:
            df = SCRAPERS[name]()
            frames.append(df)

            state_slug = name.lower().replace(" ", "_")
            path = out / f"warn_{state_slug}_{today}.csv"
            df.to_csv(path, index=False)
            log.info(f"  Saved {path}")

        except Exception as exc:
            log.error(f"  {name} failed: {exc}")

    if not frames:
        log.warning("No data collected.")
        return pd.DataFrame(columns=OUTPUT_COLS)

    combined = pd.concat(frames, ignore_index=True)
    combined_path = out / (combined_filename or f"warn_combined_{today}.csv")
    combined.to_csv(combined_path, index=False)
    log.info(f"\nCombined file saved → {combined_path}  ({len(combined)} total rows)")

    # ── Master file — accumulates all daily results ───────────────────────────
    MASTER_FILE = out / "warn_master.csv"

    df_new = combined.copy()
    df_new['Date_Appended'] = today

    if MASTER_FILE.exists():
        df_master = pd.read_csv(MASTER_FILE, encoding='utf-8')
        df_master = pd.concat([df_master, df_new], ignore_index=True)
    else:
        df_master = df_new

    df_master = df_master.drop_duplicates(subset=['state', 'company', 'notice_date'])
    _sort_dt = pd.to_datetime(df_master['notice_date'], errors='coerce')
    df_master = df_master.iloc[_sort_dt.argsort()[::-1].values]
    df_master.to_csv(MASTER_FILE, index=False, encoding='utf-8')
    log.info(f"Master file updated → {MASTER_FILE}  ({len(df_master)} total rows)")

    # ── Mirror to master_file/ alongside other project master CSVs ───────────
    master_folder = Path("master_file")
    master_folder.mkdir(exist_ok=True)
    df_master.to_csv(master_folder / "warn_master.csv", index=False, encoding='utf-8')
    log.info(f"Master file mirrored → {master_folder / 'warn_master.csv'}")

    return combined


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    df = run_all(output_dir="warn_output")

    print(f"\n{'='*60}")
    print(f"TOTAL ROWS: {len(df)}")
    print(f"STATES:     {df['state'].unique().tolist()}")
    print(f"{'='*60}\n")
    print(df.to_string(index=False, max_rows=30))