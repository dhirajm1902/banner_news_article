"""
bizjournals_scraper.py
Scrapes bizjournals.com for multiple search queries using Playwright + stealth.
Falls back to Zyte proxy if Cloudflare blocks the plain request.
Then fetches each article URL and extracts full content via:
  - trafilatura  → clean body text
  - newspaper4k  → auto summary + keywords
  - extruct      → JSON-LD structured data (name, date, address) + OpenGraph

Install extraction libraries:
  pip install trafilatura newspaper4k extruct w3lib

Output:
  data/bizjournals/Daily_bizjournals_YYYY-MM-DD.csv
  master_file/bizjournals_master.csv
  bizjournals_latest.json
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Force UTF-8 output so emojis/special chars work on Windows cp1252 consoles
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    from playwright_stealth import Stealth as _Stealth
    _STEALTH = _Stealth()
    HAS_STEALTH = True
except Exception:
    _STEALTH = None
    HAS_STEALTH = False
    print("⚠️  playwright-stealth not available — running without stealth mode")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests
import concurrent.futures

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
    print("⚠️  trafilatura not available — pip install trafilatura")

try:
    import newspaper
    HAS_NEWSPAPER = True
except ImportError:
    HAS_NEWSPAPER = False
    print("⚠️  newspaper4k not available — pip install newspaper4k")

try:
    import extruct
    from w3lib.html import get_base_url as _get_base_url
    HAS_EXTRUCT = True
except ImportError:
    HAS_EXTRUCT = False
    print("⚠️  extruct not available — pip install extruct w3lib")

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_QUERIES = ["restaurant opening", "store opening", "grocery opening"]
DAYS_BACK      = 1        # look back N days (1 = yesterday → today)
HEADLESS       = True     # set False to watch the browser during debugging

ZYTE_API_KEY = os.environ.get("ZYTE_API_KEY", "")

# ── Date range ────────────────────────────────────────────────────────────────
today      = datetime.now()
date_end   = today.strftime("%Y-%m-%d")
date_begin = (today - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%d")


def _build_search_url(query: str) -> str:
    return (
        "https://www.bizjournals.com/search"
        f"?q={query.replace(' ', '+')}"
        f"&db={date_begin}&de={date_end}&s=2"
    )


print(f"🔍 Queries: {', '.join(repr(q) for q in SEARCH_QUERIES)}  |  {date_begin} → {date_end}\n")


# ── Cloudflare detection ──────────────────────────────────────────────────────
def is_cloudflare_blocked(html: str) -> bool:
    """Return True if the page is a Cloudflare challenge / bot-block page."""
    markers = [
        "just a moment",
        "checking your browser",
        "enable javascript",
        "cf-browser-verification",
        "ray id",
        "cloudflare",
        "please wait",
    ]
    snippet = html[:3000].lower()
    return any(m in snippet for m in markers)


# ── Core browser context factory ──────────────────────────────────────────────
def _make_context(playwright_instance, use_zyte: bool = False):
    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-web-security",
    ]

    browser = playwright_instance.chromium.launch(
        headless=HEADLESS,
        args=launch_args,
    )

    ctx_kwargs = dict(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
        ignore_https_errors=True,
    )

    if use_zyte:
        if not ZYTE_API_KEY:
            raise RuntimeError("ZYTE_API_KEY not set — cannot use Zyte proxy fallback")
        ctx_kwargs["proxy"] = {
            "server": "http://api.zyte.com:8011",
            "username": ZYTE_API_KEY,
            "password": "",
        }
        print("  Using Zyte proxy for Cloudflare bypass")

    context = browser.new_context(**ctx_kwargs)
    return browser, context


# ── Navigate a page within an existing Playwright page object ────────────────
def _navigate_and_get_html(page, url: str) -> str:
    """Go to url, wait for results, scroll, return HTML."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except PlaywrightTimeout:
        print("  ⚠️  Page load timed out — reading partial content")

    try:
        page.wait_for_selector("a.item.item--flag", timeout=15_000)
    except PlaywrightTimeout:
        print("  ⚠️  Result selector timed out")

    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    page.wait_for_timeout(1_000)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1_000)
    return page.content()


# ── Parse one page of results, return (rows, next_page_url | None) ────────────
def parse_page(html: str):
    """
    bizjournals.com search result structure (confirmed from live HTML):
      <a class="item item--flag" href="/market/news/...">
        <div class="item__body">
          <div class="meta">
            <span class="meta-item">News</span>          ← [0] category
            <span class="meta-item">15 hours ago</span>  ← [1] time ago
            <span class="meta-item">Nashville BJ</span>  ← [2] publication
          </div>
          <h3 class="item__title">Headline…</h3>
          <p class="item__teaser">Snippet…</p>
        </div>
      </a>
    Pagination: <ol class="row pagination"> … <a href="…&pl=N">Next</a> …
    """
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("a.item.item--flag")

    rows = []
    for item in items:
        href = item.get("href", "")
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.bizjournals.com" + href

        title_el = item.select_one("h3.item__title, h2.item__title")
        title = (
            title_el.get_text(separator=" ", strip=True)
            if title_el
            else item.get_text(separator=" ", strip=True)[:120]
        )

        meta_spans = item.select("span.meta-item")
        pub_date = meta_spans[1].get_text(strip=True) if len(meta_spans) > 1 else ""
        source   = meta_spans[2].get_text(strip=True) if len(meta_spans) > 2 else ""

        snippet_el = item.select_one("p.item__teaser")
        snippet = snippet_el.get_text(separator=" ", strip=True) if snippet_el else ""

        rows.append({
            "title":    title,
            "url":      href,
            "source":   source,
            "pub_date": pub_date,
            "snippet":  snippet,
        })

    # Find "Next page" link — the one whose text is "Next" or has rel="next"
    next_url = None
    pag = soup.select_one("ol.pagination, nav.pagination")
    if pag:
        for a in pag.select("a[href]"):
            txt = a.get_text(strip=True).lower()
            if "next" in txt or a.get("rel") == ["next"]:
                href = a.get("href", "")
                if href.startswith("/"):
                    next_url = "https://www.bizjournals.com" + href
                elif href.startswith("http"):
                    next_url = href
                break

    return rows, next_url


# ── Single Playwright session — fetches all pages without closing browser ─────
def scrape_all_pages(query: str, use_zyte: bool = False) -> list:
    """Open one browser session and paginate through all result pages for a single query."""
    all_rows = []
    seen_urls: set = set()

    with sync_playwright() as p:
        browser, context = _make_context(p, use_zyte=use_zyte)
        page_obj = context.new_page()

        if HAS_STEALTH:
            _STEALTH.apply_stealth_sync(page_obj)

        page_obj.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
        })

        # Start at page 1 (no pl param = default first page)
        start_url = _build_search_url(query) + "&pl=1"
        current_url = start_url
        page_num = 1

        while current_url:
            print(f"  Page {page_num}: {current_url}")
            html = _navigate_and_get_html(page_obj, current_url)

            if is_cloudflare_blocked(html):
                print(f"  ❌ Cloudflare blocked page {page_num} — aborting")
                Path("bizjournals_debug.html").write_text(html, encoding="utf-8")
                break

            page_rows, next_url = parse_page(html)
            print(f"    → {len(page_rows)} results")

            if not page_rows and page_num == 1:
                print("  ⚠️  No results on page 1 — saving debug HTML")
                Path("bizjournals_debug.html").write_text(html, encoding="utf-8")

            for row in page_rows:
                if row["url"] not in seen_urls:
                    seen_urls.add(row["url"])
                    all_rows.append(row)

            current_url = next_url
            page_num += 1

        browser.close()
    return all_rows


# ── Article content extraction ────────────────────────────────────────────────

def _fetch_html_requests(url, use_zyte=False):
    """Fetch raw HTML via requests, optionally through Zyte proxy."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    proxies = None
    if use_zyte and ZYTE_API_KEY:
        proxies = {
            "http":  f"http://{ZYTE_API_KEY}:@api.zyte.com:8011",
            "https": f"http://{ZYTE_API_KEY}:@api.zyte.com:8011",
        }
    try:
        resp = requests.get(url, headers=headers, proxies=proxies,
                            timeout=25, verify=not bool(proxies))
        if resp.status_code == 200:
            return resp.text
    except Exception:
        pass
    return None


def extract_article_content(url, use_zyte=False):
    """
    Fetch a single article URL and extract content with three libraries:
      - trafilatura  → clean full body text
      - newspaper4k  → auto summary + keywords
      - extruct      → JSON-LD structured data (name, date, address) + OpenGraph description
    Returns a dict of enrichment fields (all default to empty string on failure).
    """
    result = {
        "full_text":      "",
        "summary":        "",
        "keywords":       "",
        "jsonld_name":    "",
        "jsonld_date":    "",
        "jsonld_address": "",
        "og_description": "",
    }

    # Try direct fetch first; fall back to Zyte if blocked or too short
    html = _fetch_html_requests(url, use_zyte=False)
    if not html or len(html) < 300 or is_cloudflare_blocked(html):
        if ZYTE_API_KEY:
            html = _fetch_html_requests(url, use_zyte=True)
    if not html:
        return result

    # trafilatura — clean full body text (best general extractor)
    if HAS_TRAFILATURA:
        text = trafilatura.extract(html, include_comments=False, include_tables=False)
        result["full_text"] = text or ""

    # newspaper4k — NLP summary + keywords
    if HAS_NEWSPAPER:
        try:
            art = newspaper.Article(url)
            art.set_html(html)
            art.parse()
            try:
                art.nlp()
                result["summary"]  = art.summary or ""
                result["keywords"] = ", ".join(art.keywords) if art.keywords else ""
            except Exception:
                result["summary"] = (art.text or "")[:500]
        except Exception:
            pass

    # extruct — JSON-LD structured data + OpenGraph
    if HAS_EXTRUCT:
        try:
            base_url = _get_base_url(html, url)
            data = extruct.extract(html, base_url=base_url,
                                   syntaxes=["json-ld", "opengraph"],
                                   errors="ignore")
            # Find first Article/NewsArticle JSON-LD block
            for item in data.get("json-ld", []):
                t = item.get("@type", "")
                if isinstance(t, list):
                    t = " ".join(t)
                if any(k in t for k in ("Article", "NewsArticle", "BlogPosting", "Report")):
                    result["jsonld_name"] = item.get("headline") or item.get("name") or ""
                    result["jsonld_date"] = (
                        item.get("datePublished") or item.get("dateCreated") or ""
                    )
                    loc = item.get("contentLocation") or item.get("locationCreated") or {}
                    if isinstance(loc, dict):
                        result["jsonld_address"] = loc.get("name") or loc.get("address") or ""
                    elif isinstance(loc, str):
                        result["jsonld_address"] = loc
                    break
            og = data.get("opengraph", [])
            if og:
                result["og_description"] = og[0].get("og:description") or ""
        except Exception:
            pass

    return result


# ── Determine whether Zyte is needed (probe with first query) ─────────────────
_probe_url = _build_search_url(SEARCH_QUERIES[0]) + "&pl=1"
print("Attempt 1: Playwright + stealth (no proxy)…")
with sync_playwright() as _p:
    _browser, _ctx = _make_context(_p, use_zyte=False)
    _page = _ctx.new_page()
    if HAS_STEALTH:
        _STEALTH.apply_stealth_sync(_page)
    _probe_html = _navigate_and_get_html(_page, _probe_url)
    _browser.close()

USE_ZYTE = False
if is_cloudflare_blocked(_probe_html):
    print("  ❌ Cloudflare detected")
    if not ZYTE_API_KEY:
        print("  ⚠️  No ZYTE_API_KEY — saving debug HTML and exiting")
        Path("bizjournals_debug.html").write_text(_probe_html, encoding="utf-8")
        raise SystemExit(1)
    print("Attempt 2: Playwright + Zyte proxy…")
    USE_ZYTE = True
else:
    print("  ✅ No Cloudflare — proceeding without proxy")

# ── Run all queries and merge, deduplicating by URL ───────────────────────────
_all_rows = []
_seen_urls: set = set()
for _query in SEARCH_QUERIES:
    print(f"\n🔍 Scraping query: \"{_query}\"")
    _qrows = scrape_all_pages(_query, use_zyte=USE_ZYTE)
    _new = 0
    for _r in _qrows:
        if _r["url"] not in _seen_urls:
            _seen_urls.add(_r["url"])
            _r["query"] = _query
            _all_rows.append(_r)
            _new += 1
    print(f"   → {len(_qrows)} results, {_new} unique new  ({len(_all_rows)} total so far)")

rows = _all_rows

print(f"\n📋 Total unique articles: {len(rows)}")
for r in rows[:5]:
    print(f"  • [{r['query']}] {r['title'][:60]}")
if len(rows) > 5:
    print(f"  … and {len(rows) - 5} more")

# ── Enrich each article with full content extraction ──────────────────────────
ENRICH_WORKERS = 5  # concurrent HTTP workers

def _enrich_row(row):
    content = extract_article_content(row["url"], use_zyte=USE_ZYTE)
    return {**row, **content}

if HAS_TRAFILATURA or HAS_NEWSPAPER or HAS_EXTRUCT:
    print(f"\n📰 Extracting article content ({len(rows)} URLs, {ENRICH_WORKERS} workers)…")
    with concurrent.futures.ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        rows = list(pool.map(_enrich_row, rows))
    extracted_count = sum(1 for r in rows if r.get("full_text") or r.get("summary"))
    print(f"  ✅ Content extracted for {extracted_count}/{len(rows)} articles")
else:
    print("\n⚠️  No extraction libraries found — run: pip install trafilatura newspaper4k extruct w3lib")

# ── Output directories ────────────────────────────────────────────────────────
BJ_DIR     = Path("data/bizjournals")
MASTER_DIR = Path("master_file")
BJ_DIR.mkdir(parents=True, exist_ok=True)
MASTER_DIR.mkdir(parents=True, exist_ok=True)

today_str = today.strftime("%Y-%m-%d")

# ── Daily CSV ─────────────────────────────────────────────────────────────────
csv_file = BJ_DIR / f"Daily_bizjournals_{today_str}.csv"
df = pd.DataFrame(
    rows,
    columns=[
        "title", "url", "source", "pub_date", "snippet", "query",
        "full_text", "summary", "keywords",
        "jsonld_name", "jsonld_date", "jsonld_address", "og_description",
    ],
)
df.to_csv(csv_file, index=False, encoding="utf-8")
print(f"\n✅ CSV  saved → {csv_file}  ({len(df)} rows)")

# ── JSON ──────────────────────────────────────────────────────────────────────
json_payload = {
    "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "queries":      SEARCH_QUERIES,
    "date_begin":   date_begin,
    "date_end":     date_end,
    "total":        len(rows),
    "data":         rows,
}
with open("bizjournals_latest.json", "w", encoding="utf-8") as f:
    json.dump(json_payload, f, ensure_ascii=False, indent=2)
print(f"✅ JSON saved → bizjournals_latest.json  ({len(rows)} records)")

# ── Master file ───────────────────────────────────────────────────────────────
MASTER_FILE = MASTER_DIR / "bizjournals_master.csv"

df_new = df.copy()
df_new["Date_Appended"] = today_str

if MASTER_FILE.exists():
    df_master = pd.read_csv(MASTER_FILE, encoding="utf-8")
    df_master = pd.concat([df_master, df_new], ignore_index=True)
else:
    df_master = df_new

df_master = df_master.drop_duplicates(subset=["url"])
df_master.to_csv(MASTER_FILE, index=False, encoding="utf-8")
print(f"✅ Master file updated: {MASTER_FILE}  ({len(df_master)} total rows)")
