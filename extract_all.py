import sys
import json
import requests
import extruct
import trafilatura
from newspaper import Article
from playwright.sync_api import sync_playwright
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
    """Query Wayback Machine API for the closest snapshot URL."""
    try:
        api = f"https://archive.org/wayback/available?url={url}"
        r = requests.get(api, timeout=10)
        data = r.json()
        snapshot = data.get("archived_snapshots", {}).get("closest", {})
        if snapshot.get("available"):
            return snapshot["url"]
    except Exception:
        pass
    return None

def fetch_with_playwright(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
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

def extract_all(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

    print(f"\n{'='*60}")
    print(f"URL: {url}")
    print('='*60)

    # Fetch chain: requests → curl_cffi (CF bypass) → Playwright → archive.org
    print("\nFetching page...")
    html = None
    fetch_url = url

    def is_blocked(h):
        blocked = ["just a moment", "performing security verification", "cf-browser-verification", "enable javascript"]
        return len(h) < 5000 or any(b in h.lower() for b in blocked)

    # Step 1: plain requests
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200 and not is_blocked(r.text):
            html = r.text
            print("Fetched via requests.")
    except Exception:
        pass

    # Step 2: curl_cffi — impersonates real Chrome TLS fingerprint, bypasses Cloudflare
    if html is None and HAS_CURL_CFFI:
        try:
            r = cf_requests.get(url, impersonate="chrome124", timeout=20)
            if r.status_code == 200 and not is_blocked(r.text):
                html = r.text
                print("Fetched via curl_cffi (Cloudflare bypass).")
            else:
                print(f"curl_cffi got status {r.status_code} or blocked page.")
        except Exception as e:
            print(f"curl_cffi failed: {e}")
    elif html is None and not HAS_CURL_CFFI:
        print("curl_cffi not installed (pip install curl_cffi), skipping...")

    # Step 3: Playwright headless browser
    if html is None:
        print("Trying Playwright...")
        try:
            html = fetch_with_playwright(url)
            if is_blocked(html):
                raise Exception("Cloudflare challenge page returned")
            print("Fetched via Playwright (headless browser).")
        except Exception as e2:
            print(f"Playwright failed: {e2}")

    # Step 4: archive.org
    if html is None:
        print("Trying archive.org...")
        archive_url = get_archive_url(url)
        if archive_url:
            print(f"Archive snapshot: {archive_url}")
            fetch_url = archive_url
            try:
                html = fetch_with_playwright(archive_url)
                if is_blocked(html):
                    raise Exception("Blocked on archive too")
                print("Fetched via archive.org.")
            except Exception as e3:
                print(f"Archive failed: {e3}")

    if html is None:
        print("\n[FAILED] Could not fetch article. Site may be paywalled or too aggressively protected.")
        return

    # --- EXTRUCT (structured JSON-LD metadata) ---
    print("\n[1] EXTRUCT - Structured Metadata")
    print("-"*40)
    try:
        data = extruct.extract(html, base_url=url, syntaxes=['json-ld', 'opengraph', 'microdata'])
        if data.get('json-ld'):
            print("JSON-LD:", json.dumps(data['json-ld'], indent=2))
        if data.get('opengraph'):
            print("OpenGraph:", json.dumps(data['opengraph'], indent=2))
        if data.get('microdata'):
            print("Microdata:", json.dumps(data['microdata'], indent=2))
        if not any([data.get('json-ld'), data.get('opengraph'), data.get('microdata')]):
            print("No structured data found.")
    except Exception as e:
        print(f"Error: {e}")

    # --- NEWSPAPER4K ---
    print("\n[2] NEWSPAPER4K - Article Metadata + Summary")
    print("-"*40)
    try:
        a = Article(fetch_url)
        a.download(input_html=html)
        a.parse()
        a.nlp()
        print(f"Title    : {a.title}")
        print(f"Date     : {a.publish_date}")
        print(f"Authors  : {a.authors}")
        print(f"Summary  : {a.summary}")
        print(f"Keywords : {a.keywords}")
        print(f"Text     :\n{a.text[:1000]}{'...' if len(a.text) > 1000 else ''}")
    except Exception as e:
        print(f"Error: {e}")

    # --- TRAFILATURA ---
    print("\n[3] TRAFILATURA - Full Article Text")
    print("-"*40)
    try:
        text = trafilatura.extract(html, include_tables=True, with_metadata=True, output_format='txt')
        print(text if text else "No text extracted.")
    except Exception as e:
        print(f"Error: {e}")

def main():
    if len(sys.argv) > 1:
        for url in sys.argv[1:]:
            extract_all(url)
    else:
        print("Enter URLs one per line. Empty line to process.")
        urls = []
        while True:
            try:
                line = input("> ").strip()
                if line:
                    urls.append(line)
                elif urls:
                    for url in urls:
                        extract_all(url)
                    urls = []
            except (KeyboardInterrupt, EOFError):
                break

if __name__ == "__main__":
    main()
