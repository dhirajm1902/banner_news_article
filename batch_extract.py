import sys
import json
import re
import requests
import extruct
import trafilatura
from newspaper import Article
from pathlib import Path
from playwright.sync_api import sync_playwright
from datetime import datetime

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


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
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()
        if HAS_STEALTH:
            stealth_sync(page)
        else:
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(url, wait_until="load", timeout=60000)
        page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
        return html


def is_blocked(html):
    blocked_signals = ["just a moment", "performing security verification",
                       "cf-browser-verification", "enable javascript and cookies"]
    return len(html) < 5000 or any(s in html.lower() for s in blocked_signals)


def fetch_html(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # Step 1: plain requests
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200 and not is_blocked(r.text):
            return r.text, "requests"
    except Exception:
        pass

    # Step 2: curl_cffi (Cloudflare TLS fingerprint bypass)
    if HAS_CURL_CFFI:
        try:
            r = cf_requests.get(url, impersonate="chrome124", timeout=20)
            if r.status_code == 200 and not is_blocked(r.text):
                return r.text, "curl_cffi"
        except Exception:
            pass

    # Step 3: Playwright headless
    try:
        html = fetch_with_playwright(url)
        if not is_blocked(html):
            return html, "playwright"
    except Exception:
        pass

    # Step 4: archive.org
    archive_url = get_archive_url(url)
    if archive_url:
        try:
            html = fetch_with_playwright(archive_url)
            if not is_blocked(html):
                return html, f"archive.org ({archive_url})"
        except Exception:
            pass

    return None, "failed"


def extract_content(url, html):
    result = {"extruct": {}, "newspaper": {}, "trafilatura": ""}

    # extruct
    try:
        data = extruct.extract(html, base_url=url, syntaxes=["json-ld", "opengraph", "microdata"])
        result["extruct"] = {k: v for k, v in data.items() if v}
    except Exception as e:
        result["extruct"] = {"error": str(e)}

    # newspaper4k
    try:
        a = Article(url)
        a.download(input_html=html)
        a.parse()
        a.nlp()
        result["newspaper"] = {
            "title": a.title,
            "date": str(a.publish_date) if a.publish_date else "",
            "authors": a.authors,
            "summary": a.summary,
            "keywords": a.keywords,
            "text": a.text[:2000] + ("..." if len(a.text) > 2000 else ""),
        }
    except Exception as e:
        result["newspaper"] = {"error": str(e)}

    # trafilatura
    try:
        text = trafilatura.extract(html, include_tables=True, with_metadata=True, output_format="txt")
        result["trafilatura"] = text or ""
    except Exception as e:
        result["trafilatura"] = f"Error: {e}"

    return result


def to_markdown(idx, url, fetch_method, data):
    lines = [f"## Article {idx}", f"", f"**URL:** {url}  ", f"**Fetched via:** {fetch_method}", ""]

    # extruct
    lines.append("### Structured Metadata (extruct)")
    ext = data["extruct"]
    if "error" in ext:
        lines.append(f"_Error: {ext['error']}_")
    elif not ext:
        lines.append("_No structured data found._")
    else:
        for syntax, items in ext.items():
            if items:
                lines.append(f"**{syntax}:**")
                lines.append("```json")
                lines.append(json.dumps(items, indent=2)[:1500])
                lines.append("```")
    lines.append("")

    # newspaper4k
    lines.append("### Article Info (newspaper4k)")
    np = data["newspaper"]
    if "error" in np:
        lines.append(f"_Error: {np['error']}_")
    else:
        if np.get("title"):   lines.append(f"- **Title:** {np['title']}")
        if np.get("date"):    lines.append(f"- **Date:** {np['date']}")
        if np.get("authors"): lines.append(f"- **Authors:** {', '.join(np['authors'])}")
        if np.get("keywords"):lines.append(f"- **Keywords:** {', '.join(np['keywords'])}")
        if np.get("summary"): lines.append(f"\n**Summary:**  \n{np['summary']}")
        if np.get("text"):    lines.append(f"\n**Text (first 2000 chars):**  \n{np['text']}")
    lines.append("")

    # trafilatura
    lines.append("### Full Text (trafilatura)")
    traf = data["trafilatura"]
    if traf:
        lines.append(traf[:3000] + ("..." if len(traf) > 3000 else ""))
    else:
        lines.append("_No text extracted._")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def read_urls(filepath):
    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            # strip leading "1. " or "- " numbering
            line = re.sub(r"^\d+\.\s+", "", line)
            line = re.sub(r"^[-*]\s+", "", line)
            if line.startswith("http"):
                urls.append(line)
    return urls


def main():
    input_file = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Lenovo\Downloads\Links50.txt"
    output_file = Path(r"C:\Users\Lenovo\Downloads") / f"extracted_articles.md"

    urls = read_urls(input_file)
    total = len(urls)
    print(f"Found {total} URLs in {input_file}")
    print(f"Output will be saved to: {output_file}\n")

    md_lines = [
        f"# Article Extractions",
        f"",
        f"**Source:** {input_file}  ",
        f"**Total articles:** {total}  ",
        f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"",
        "---",
        "",
    ]

    for i, url in enumerate(urls, 1):
        print(f"[{i}/{total}] {url[:80]}...")
        html, method = fetch_html(url)
        if html is None:
            print(f"  -> FAILED to fetch")
            block = f"## Article {i}\n\n**URL:** {url}  \n**Status:** FAILED — could not fetch (blocked/paywalled)\n\n---\n"
            md_lines.append(block)
        else:
            print(f"  -> Fetched via {method}, extracting...")
            data = extract_content(url, html)
            md_lines.append(to_markdown(i, url, method, data))
            print(f"  -> Done")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))

    print(f"\nDone! Saved to: {output_file}")


if __name__ == "__main__":
    main()
