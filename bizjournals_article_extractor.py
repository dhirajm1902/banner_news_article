"""
bizjournals_article_extractor.py
Extracts the FULL article text (not just title/snippet) from a list of URLs.

Works two ways:

  1) Direct — pass URLs straight on the command line:
       python bizjournals_article_extractor.py --urls "https://www.bizjournals.com/..." "https://..."

  2) From a file — load URLs out of a CSV or Excel file (e.g. the CSV that
     bizjournals_scraper.py already produces, or any spreadsheet with a
     URL/link column):
       python bizjournals_article_extractor.py --input data/bizjournals/Daily_bizjournals_2026-07-14.csv
       python bizjournals_article_extractor.py --input urls.xlsx --url-column "Article Link" --limit 50

Fetch chain (same one already proven in extract_from_excel.py):
    requests -> curl_cffi (TLS impersonation) -> Playwright + stealth
    -> Zyte API browserHtml (if ZYTE_API_KEY set; solves Cloudflare's JS
       challenge server-side, not just an IP proxy) -> archive.org snapshot

Text extraction:
    trafilatura (primary body text) + newspaper3k (title/date/authors),
    with a BeautifulSoup selector fallback for bizjournals' own markup.

Output (written to --outdir, default extraction_output/):
    bizjournals_articles_extracted.csv   — url, title, pub_date, authors, fetch_method, text
    bizjournals_articles_extracted.json  — same data as JSON
    bizjournals_needs_manual_review.csv  — url, block_reason (paywalled / captcha / bot-blocked / thin)

Anything paywalled or CAPTCHA-gated is NOT bypassed — bizjournals.com is a
subscription site, so paywalled articles land in the manual-review CSV
instead of being silently skipped.
"""

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd
import requests
import trafilatura
from bs4 import BeautifulSoup
from newspaper import Article
from playwright.sync_api import sync_playwright

try:
    from playwright_stealth import Stealth as _Stealth
    _STEALTH = _Stealth()
    HAS_STEALTH = True
except Exception:
    _STEALTH = None
    HAS_STEALTH = False

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ZYTE_API_KEY = os.environ.get("ZYTE_API_KEY", "")
MIN_TEXT_LEN = 200

BOT_BLOCK_SIGNALS = [
    "just a moment", "performing security verification",
    "cf-browser-verification", "enable javascript and cookies",
]
CAPTCHA_SIGNALS = [
    "recaptcha", "hcaptcha", "g-recaptcha", "captcha-delivery",
    "verify you are human", "are you a robot",
]
PAYWALL_SIGNALS = [
    "subscribe to continue", "subscribe to read", "this content is reserved for subscribers",
    "already a subscriber", "to continue reading", "meter-paywall", "paywall-message",
    "create an account to continue", "subscribe now to keep reading",
]
# bizjournals often renders a full, large (100KB+) page around just a short
# teaser snippet of the real article — the raw-HTML paywall scan above misses
# it because the marker sits way past BLOCK_SCAN_CHARS/BLOCK_PAGE_MAX_LEN on
# a page that size. Checked separately against the extracted article TEXT
# (which is short regardless of page size) in extract_urls().
TEASER_TEXT_SIGNALS = ["preview this article"]


# ── Block classification ────────────────────────────────────────────────────────
# Real block/paywall/captcha interstitials are short — the marker phrase shows
# up near the top AND the whole page is small, because the block message IS
# the page. Long, fully-loaded articles can still mention these words deep in
# unrelated JS/CSS (a comment-form reCAPTCHA widget, a site config blob) —
# e.g. Wikipedia's edit-abuse-filter config mentions "hcaptcha" near the top
# of a 230KB page that isn't blocked at all. Gating on page size as well as
# keyword position keeps that from being misread as a block.
BLOCK_SCAN_CHARS = 4000
BLOCK_PAGE_MAX_LEN = 20_000


def classify_block(html: str) -> str:
    if len(html) < 5000:
        return "bot_block"
    low = html[:BLOCK_SCAN_CHARS].lower()
    # Bot-block signals (Cloudflare "Just a moment" challenge pages etc.) are
    # checked regardless of page size: they're specific enough phrases that a
    # genuine article won't contain them, and Cloudflare's challenge bundle
    # itself can exceed BLOCK_PAGE_MAX_LEN (its JS/CSS alone runs 25-30KB).
    if any(s in low for s in BOT_BLOCK_SIGNALS):
        return "bot_block"
    if len(html) < BLOCK_PAGE_MAX_LEN:
        if any(s in low for s in CAPTCHA_SIGNALS):
            return "captcha"
        if any(s in low for s in PAYWALL_SIGNALS):
            return "paywall"
    return "unknown_block"


def is_blocked(html: str) -> bool:
    if len(html) < 5000:
        return True
    low = html[:BLOCK_SCAN_CHARS].lower()
    if any(s in low for s in BOT_BLOCK_SIGNALS):
        return True
    if len(html) >= BLOCK_PAGE_MAX_LEN:
        return False
    return any(s in low for s in CAPTCHA_SIGNALS) or any(s in low for s in PAYWALL_SIGNALS)


# ── Fetch chain ────────────────────────────────────────────────────────────────
def get_archive_url(url):
    try:
        r = requests.get(f"https://archive.org/wayback/available?url={url}", timeout=10)
        snap = r.json().get("archived_snapshots", {}).get("closest", {})
        if snap.get("available"):
            return snap["url"]
    except Exception:
        pass
    return None


def fetch_with_playwright(url):
    with sync_playwright() as p:
        launch_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]
        browser = p.chromium.launch(headless=True, args=launch_args)

        ctx_kwargs = dict(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )

        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        if HAS_STEALTH:
            _STEALTH.apply_stealth_sync(page)
        else:
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        html = page.content()
        browser.close()
        return html


def fetch_with_zyte_api(url):
    """Zyte API's browserHtml mode: Zyte renders the page in their own
    anti-bot-hardened browser infra and hands back the resulting HTML.
    Unlike the legacy Smart Proxy Manager (a raw IP proxy plugged into our own
    Playwright browser), this actually gets past Cloudflare's JS challenge —
    the challenge-solving happens on Zyte's side, not ours."""
    if not ZYTE_API_KEY:
        raise RuntimeError("ZYTE_API_KEY not set — cannot use Zyte API fallback")
    r = requests.post(
        "https://api.zyte.com/v1/extract",
        auth=(ZYTE_API_KEY, ""),
        json={"url": url, "browserHtml": True},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["browserHtml"]


def fetch_html(url):
    """Returns (html_or_None, method, block_reason_or_None)."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    last_html = None

    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            if not is_blocked(r.text):
                return r.text, "requests", None
            last_html = r.text
    except Exception:
        pass

    if HAS_CURL_CFFI:
        try:
            r = cf_requests.get(url, impersonate="chrome124", timeout=20)
            if r.status_code == 200:
                if not is_blocked(r.text):
                    return r.text, "curl_cffi", None
                last_html = r.text
        except Exception:
            pass

    try:
        html = fetch_with_playwright(url)
        if not is_blocked(html):
            return html, "playwright", None
        last_html = html
    except Exception:
        pass

    if ZYTE_API_KEY:
        try:
            html = fetch_with_zyte_api(url)
            if not is_blocked(html):
                return html, "zyte_api", None
            last_html = html
        except Exception:
            pass

    archive_url = get_archive_url(url)
    if archive_url:
        try:
            html = fetch_with_playwright(archive_url)
            if not is_blocked(html):
                return html, f"archive.org ({archive_url})", None
            last_html = html
        except Exception:
            pass

    reason = classify_block(last_html) if last_html else "no_response"
    return None, "failed", reason


# ── Text extraction ────────────────────────────────────────────────────────────
def _beautifulsoup_fallback(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    body = (
        soup.select_one("div.article-body, section.article-body, div.item__content")
        or soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=lambda c: c and "content" in c.lower())
        or soup
    )
    return body.get_text(separator=" ", strip=True)


def extract_content(url: str, html: str) -> dict:
    result = {"title": "", "date": "", "authors": [], "text": ""}

    try:
        a = Article(url)
        a.download(input_html=html)
        a.parse()
        result["title"] = a.title
        result["date"] = str(a.publish_date) if a.publish_date else ""
        result["authors"] = a.authors
    except Exception:
        pass

    try:
        text = trafilatura.extract(html, include_tables=True, with_metadata=False, output_format="txt")
        result["text"] = text or ""
    except Exception:
        pass

    if not result["text"] or len(result["text"]) < MIN_TEXT_LEN:
        fallback = _beautifulsoup_fallback(html)
        if len(fallback) > len(result["text"]):
            result["text"] = fallback

    return result


# ── Input loading (mode 2: CSV / Excel) ────────────────────────────────────────
URL_COLUMN_CANDIDATES = ["url", "link", "article link", "article_link", "resolved_url"]


def _looks_like_url(val) -> bool:
    return isinstance(val, str) and val.strip().startswith("http")


def _url_ratio(series: pd.Series) -> float:
    sample = series.dropna()
    if not len(sample):
        return 0.0
    return sum(_looks_like_url(v) for v in sample) / len(sample)


def _best_content_column(df: pd.DataFrame):
    """Pick whichever column's non-null values mostly look like URLs — works
    whether or not the sheet has a header row, and survives blank spacer rows."""
    best_col, best_ratio = None, 0.0
    for col in df.columns:
        ratio = _url_ratio(df[col])
        if ratio > best_ratio:
            best_col, best_ratio = col, ratio
    if best_col is not None and best_ratio > 0.4:
        return best_col
    return None


def _read_raw(path: Path) -> pd.DataFrame:
    """Read without assuming a header row exists — some sheets in this project
    (e.g. Articles_yet_to_be_extracted.xlsx) store URLs in column 4 with no header."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path, engine="openpyxl" if path.suffix.lower() == ".xlsx" else None, header=None)
    return pd.read_csv(path, encoding="utf-8-sig", header=None)


def load_urls_from_file(path: Path, url_column=None, limit: int = None) -> list:
    # Pass 1: assume a header row and look for a column named like "url"/"link".
    if path.suffix.lower() in (".xlsx", ".xls"):
        df_named = pd.read_excel(path, engine="openpyxl" if path.suffix.lower() == ".xlsx" else None)
    else:
        df_named = pd.read_csv(path, encoding="utf-8-sig")

    lower_cols = {str(c).strip().lower(): c for c in df_named.columns}
    named_col = None
    if url_column is not None and str(url_column) in df_named.columns:
        named_col = url_column
    elif url_column is None:
        for candidate in URL_COLUMN_CANDIDATES:
            if candidate in lower_cols and _url_ratio(df_named[lower_cols[candidate]]) > 0.4:
                named_col = lower_cols[candidate]
                break

    if named_col is not None:
        df, col = df_named, named_col
    else:
        # Pass 2: no confident header match — fall back to positional detection
        # over every row (including row 0), so a header-less sheet or a blank
        # spacer first row doesn't throw off detection.
        df_raw = _read_raw(path)
        if df_raw.empty:
            return []
        if url_column is not None:
            idx = int(url_column) if str(url_column).strip().lstrip("-").isdigit() else None
            if idx is None or idx not in df_raw.columns:
                raise ValueError(
                    f"Column '{url_column}' not found. Named columns: {list(df_named.columns)}; "
                    f"or pass a 0-indexed column number for header-less files."
                )
            df, col = df_raw, idx
        else:
            col = _best_content_column(df_raw)
            if col is None:
                raise ValueError(
                    f"Could not find a URL column automatically. Named columns: {list(df_named.columns)}. "
                    f"Pass --url-column to specify it explicitly (a header name, or a 0-indexed "
                    f"column number for header-less files)."
                )
            df = df_raw

    urls = []
    for val in df[col]:
        if _looks_like_url(val):
            urls.append(val.strip())
        if limit and len(urls) >= limit:
            break
    return urls


# ── Main extraction loop ───────────────────────────────────────────────────────
def extract_urls(urls: list, delay: float = 1.0) -> tuple:
    extracted_rows = []
    manual_review_rows = []

    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url[:90]}")
        html, method, reason = fetch_html(url)

        if html is None:
            print(f"  -> BLOCKED ({reason})")
            manual_review_rows.append({"url": url, "block_reason": reason})
            continue

        data = extract_content(url, html)
        if len(data["text"]) < MIN_TEXT_LEN:
            print(f"  -> fetched via {method} but only {len(data['text'])} chars — treating as failed")
            manual_review_rows.append({"url": url, "block_reason": "thin_extraction"})
            continue

        text_low = data["text"].lower()
        if any(s in text_low for s in TEASER_TEXT_SIGNALS):
            print(f"  -> fetched via {method} but text is a paywalled preview snippet — treating as paywall")
            manual_review_rows.append({"url": url, "block_reason": "paywall"})
            continue

        print(f"  -> fetched via {method}, {len(data['text'])} chars extracted")
        extracted_rows.append({
            "url": url,
            "title": data["title"],
            "pub_date": data["date"],
            "authors": ", ".join(data["authors"]),
            "fetch_method": method,
            "text": data["text"],
        })
        if i < len(urls):
            time.sleep(delay)  # be polite between requests

    return extracted_rows, manual_review_rows


DEFAULT_INPUT = r"C:\Users\Lenovo\Downloads\bizzjournal project\Sample.xlsx"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=False)
    src.add_argument("--urls", nargs="+", help="One or more article URLs, scrape directly")
    src.add_argument("--input", default=DEFAULT_INPUT,
                      help=f"Path to a CSV or Excel file containing article URLs (default: {DEFAULT_INPUT})")

    ap.add_argument("--url-column", default=None, help="Column name holding URLs (auto-detected if omitted)")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N URLs from --input")
    ap.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between article fetches")
    ap.add_argument("--outdir", default="extraction_output", help="Directory to write results into")
    args = ap.parse_args()

    if args.urls:
        urls = [u.strip() for u in args.urls if u.strip()]
        print(f"Loaded {len(urls)} URL(s) directly from the command line")
    else:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"❌  Input file not found: {input_path}")
            sys.exit(1)
        # Structure of the input file is auto-detected — header or no header,
        # any column name/position, any number of other (non-URL) columns —
        # via load_urls_from_file()'s named-column then best-content-column passes.
        urls = load_urls_from_file(input_path, url_column=args.url_column, limit=args.limit)
        print(f"Loaded {len(urls)} URL(s) from {input_path}")

    if not urls:
        print("❌  No URLs to process.")
        sys.exit(1)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    extracted_rows, manual_review_rows = extract_urls(urls, delay=args.delay)

    if extracted_rows:
        df = pd.DataFrame(extracted_rows)
        df.to_csv(outdir / "bizjournals_articles_extracted.csv", index=False, encoding="utf-8-sig")
        df.to_json(outdir / "bizjournals_articles_extracted.json", orient="records", indent=2, force_ascii=False)
    if manual_review_rows:
        pd.DataFrame(manual_review_rows).to_csv(
            outdir / "bizjournals_needs_manual_review.csv", index=False, encoding="utf-8-sig"
        )

    print(f"\n✅ Done. {len(extracted_rows)} extracted, {len(manual_review_rows)} need manual review.")
    print(f"Output written to: {outdir}")


if __name__ == "__main__":
    main()
