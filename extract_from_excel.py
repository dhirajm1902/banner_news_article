"""
Batch-extract article text for the first N rows of an Excel sheet whose
URLs live in column E (0-indexed col 4).

Reuses the same fetch chain and extractors as batch_extract.py:
    requests -> curl_cffi (TLS impersonation) -> Playwright+stealth -> archive.org

Anything that comes back CAPTCHA-gated or paywalled is NOT bypassed —
it's written to a separate "needs manual review" CSV so it can go through
the existing manual paste-into-Claude step described in CLAUDE.md, or be
opened by hand in a browser with a valid subscription.

Usage:
    python extract_from_excel.py "C:\\path\\to\\Articles_yet_to_be_extracted.xlsx" --limit 80
"""
import argparse
import base64
import json
import os
import re
import time
from pathlib import Path

import extruct
import pandas as pd
import requests
import trafilatura
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


def classify_block(html: str) -> str:
    low = html.lower()
    if any(s in low for s in CAPTCHA_SIGNALS):
        return "captcha"
    if any(s in low for s in PAYWALL_SIGNALS):
        return "paywall"
    if any(s in low for s in BOT_BLOCK_SIGNALS) or len(html) < 5000:
        return "bot_block"
    return "unknown_block"


def _decode_gnews_blob(encoded: str):
    """Ported from fetch_banner_store_news.py — decodes the base64 blob in
    a news.google.com/rss/articles/<blob> URL to recover the real publisher URL."""
    rem = len(encoded) % 4
    if rem:
        encoded += "=" * (4 - rem)
    try:
        data = base64.urlsafe_b64decode(encoded)
    except Exception:
        return None

    i = 0
    while i < len(data) - 4:
        if data[i] == 0x0A:
            length_byte = data[i + 1]
            if length_byte & 0x80:
                if i + 2 < len(data):
                    length = (length_byte & 0x7F) | ((data[i + 2] & 0x7F) << 7)
                    start = i + 3
                else:
                    i += 1
                    continue
            else:
                length = length_byte
                start = i + 2
            end = start + length
            if end <= len(data):
                candidate = data[start:end]
                try:
                    s = candidate.decode("utf-8")
                    if s.startswith("http"):
                        return s
                except UnicodeDecodeError:
                    pass
        i += 1

    idx = data.find(b"http")
    if idx != -1:
        chunk = data[idx:]
        end = next((j for j, b in enumerate(chunk) if b < 32), len(chunk))
        try:
            return chunk[:end].decode("utf-8", errors="ignore") or None
        except Exception:
            pass
    return None


def resolve_google_news_url(url: str) -> str:
    """Google News RSS links no longer embed the raw publisher URL in the
    base64 blob (newer format wraps an opaque token instead) — the only
    reliable resolution is to let a real browser follow the client-side
    redirect Google's interstitial page performs."""
    if "news.google.com" not in url:
        return url

    match = re.search(r"/articles/([A-Za-z0-9_=-]+)", url)
    if match:
        decoded = _decode_gnews_blob(match.group(1))
        if decoded:
            return decoded

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(3000)
            final_url = page.url
            browser.close()
        if final_url and "news.google.com" not in final_url:
            return final_url
    except Exception:
        pass

    return url


def get_archive_url(url):
    try:
        r = requests.get(f"https://archive.org/wayback/available?url={url}", timeout=10)
        snap = r.json().get("archived_snapshots", {}).get("closest", {})
        if snap.get("available"):
            return snap["url"]
    except Exception:
        pass
    return None


def fetch_with_playwright(url, use_zyte=False):
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
        if use_zyte:
            if not ZYTE_API_KEY:
                raise RuntimeError("ZYTE_API_KEY not set — cannot use Zyte proxy fallback")
            ctx_kwargs["proxy"] = {
                "server": "http://api.zyte.com:8011",
                "username": ZYTE_API_KEY,
                "password": "",
            }

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


def is_blocked(html):
    low = html.lower()
    return len(html) < 5000 or any(s in low for s in BOT_BLOCK_SIGNALS) \
        or any(s in low for s in CAPTCHA_SIGNALS) or any(s in low for s in PAYWALL_SIGNALS)


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
            html = fetch_with_playwright(url, use_zyte=True)
            if not is_blocked(html):
                return html, "playwright+zyte", None
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


def extract_content(url, html):
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

    if not result["text"]:
        try:
            data = extruct.extract(html, base_url=url, syntaxes=["opengraph"])
            og = data.get("opengraph", [])
            if og:
                props = dict(og[0].get("properties", []))
                result["text"] = props.get("og:description", "")
        except Exception:
            pass

    return result


def read_urls(xlsx_path, limit, url_col=4, date_col=0, category_col=5):
    df = pd.read_excel(xlsx_path, engine="openpyxl", header=None)
    rows = []
    for idx, row in df.iterrows():
        url = row.get(url_col)
        if isinstance(url, str) and url.strip().startswith("http"):
            rows.append({
                "row": idx + 1,
                "date": row.get(date_col, ""),
                "url": url.strip(),
                "category": row.get(category_col, ""),
            })
        if len(rows) >= limit:
            break
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx_path")
    ap.add_argument("--limit", type=int, default=80)
    ap.add_argument("--outdir", default=None)
    args = ap.parse_args()

    xlsx_path = Path(args.xlsx_path)
    outdir = Path(args.outdir) if args.outdir else xlsx_path.parent / "extraction_output"
    outdir.mkdir(parents=True, exist_ok=True)

    rows = read_urls(xlsx_path, args.limit)
    print(f"Loaded {len(rows)} URLs from {xlsx_path}")

    extracted_rows = []
    manual_review_rows = []

    MIN_TEXT_LEN = 200

    for i, item in enumerate(rows, 1):
        orig_url = item["url"]
        url = resolve_google_news_url(orig_url)
        print(f"[{i}/{len(rows)}] {url[:90]}")
        html, method, reason = fetch_html(url)

        if html is None:
            print(f"  -> BLOCKED ({reason})")
            manual_review_rows.append({**item, "resolved_url": url, "block_reason": reason})
            continue

        data = extract_content(url, html)
        if len(data["text"]) < MIN_TEXT_LEN:
            print(f"  -> fetched via {method} but only {len(data['text'])} chars — treating as failed")
            manual_review_rows.append({**item, "resolved_url": url, "block_reason": "thin_extraction"})
            continue

        print(f"  -> fetched via {method}, {len(data['text'])} chars extracted")
        extracted_rows.append({
            **item,
            "resolved_url": url,
            "fetch_method": method,
            "title": data["title"],
            "pub_date": data["date"],
            "authors": ", ".join(data["authors"]),
            "text": data["text"],
        })
        time.sleep(1)  # be polite between requests

    if extracted_rows:
        pd.DataFrame(extracted_rows).to_csv(outdir / "extracted.csv", index=False, encoding="utf-8-sig")
    if manual_review_rows:
        pd.DataFrame(manual_review_rows).to_csv(outdir / "needs_manual_review.csv", index=False, encoding="utf-8-sig")

    print(f"\nDone. {len(extracted_rows)} extracted, {len(manual_review_rows)} need manual review.")
    print(f"Output written to: {outdir}")


if __name__ == "__main__":
    main()
