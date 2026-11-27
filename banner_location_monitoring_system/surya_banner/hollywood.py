"""
Hollywood Feed - store locations scraper (patchright, gets past Cloudflare).

FAST version:
  1. Open the home page ONCE in a real browser to pass the Cloudflare check.
  2. Inside that page, use JavaScript fetch() (same browser, same cookies) to
     download many pages in parallel instead of navigating to each one.
  3. Store URLs come from the sitemaps (/sitemap.xml -> per-state sitemaps),
     falling back to home page -> city pages if the sitemap is unavailable.
  4. Each store page's <script type="application/ld+json"> LocalBusiness
     block has address, phone, geo, hours, store id, services.
"""
import json
import re
import time
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from patchright.sync_api import sync_playwright

BASE_URL = "https://locations.hollywoodfeed.com/"
OUTPUT_CSV = "hollywood_feed_locations.csv"
BATCH_SIZE = 10      # pages fetched in parallel per batch (be polite)
BATCH_PAUSE = 0.5    # seconds between batches

DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
STORE_PATH = re.compile(r"^/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+/?$")
CITY_PATH = re.compile(r"^/[a-z0-9-]+/[a-z0-9-]+/?$")

# Runs in the browser: fetch a batch of URLs in parallel and return either the
# raw text (mode "text") or only the JSON-LD script contents (mode "ld").
FETCH_JS = """
async ([urls, mode]) => Promise.all(urls.map(async (u) => {
    for (let attempt = 1; attempt <= 3; attempt++) {
        try {
            const r = await fetch(u, {credentials: "include"});
            if (!r.ok) throw new Error("HTTP " + r.status);
            const text = await r.text();
            if (mode === "text") return [u, text];
            const doc = new DOMParser().parseFromString(text, "text/html");
            const ld = [...doc.querySelectorAll('script[type="application/ld+json"]')]
                .map(s => s.textContent);
            const links = [...doc.querySelectorAll("a[href]")].map(a => a.getAttribute("href"));
            return [u, JSON.stringify({ld, links})];
        } catch (e) {
            if (attempt === 3) return [u, null];
            await new Promise(res => setTimeout(res, 1000 * attempt));
        }
    }
}))
"""


# --------------------------------------------------------------------------- #
# Fetch helpers
# --------------------------------------------------------------------------- #
def fetch_many(page, urls, mode="ld", label=""):
    """Fetch URLs in parallel batches from inside the browser page."""
    results = {}
    urls = list(urls)
    for i in range(0, len(urls), BATCH_SIZE):
        batch = urls[i:i + BATCH_SIZE]
        for u, body in page.evaluate(FETCH_JS, [batch, mode]):
            results[u] = body
        done = min(i + BATCH_SIZE, len(urls))
        print(f"  {label} {done}/{len(urls)}")
        time.sleep(BATCH_PAUSE)
    return results


def path_of(href):
    href = urljoin(BASE_URL, (href or "").strip())
    if not href.startswith(BASE_URL):
        return None
    return "/" + href[len(BASE_URL):].split("?")[0].split("#")[0].lstrip("/")


def parse_ld(body):
    """body is the JSON string returned by FETCH_JS in 'ld' mode."""
    data = json.loads(body)
    blocks = []
    for raw in data["ld"]:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        blocks.extend(obj if isinstance(obj, list) else [obj])
    return blocks, data["links"]


# --------------------------------------------------------------------------- #
# Store URL discovery
# --------------------------------------------------------------------------- #
def store_urls_from_sitemaps(page):
    index = fetch_many(page, [urljoin(BASE_URL, "sitemap.xml")], mode="text", label="sitemap index")
    xml = next(iter(index.values())) or ""
    sub_maps = [u.strip() for u in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.S)]
    if not sub_maps:
        return set()

    stores = set()
    for body in fetch_many(page, sub_maps, mode="text", label="state sitemaps").values():
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", body or "", re.S):
            p = path_of(loc)
            if p and STORE_PATH.match(p) and "sitemap" not in p:
                stores.add(urljoin(BASE_URL, p))
    return stores


def store_urls_from_cities(page):
    """Fallback: home page city links -> city pages -> store links."""
    home_html = page.content()
    soup = BeautifulSoup(home_html, "html.parser")
    cities, stores = set(), set()
    for a in soup.find_all("a", href=True):
        p = path_of(a["href"])
        if p and STORE_PATH.match(p):
            stores.add(urljoin(BASE_URL, p))
        elif p and CITY_PATH.match(p):
            cities.add(urljoin(BASE_URL, p))

    for body in fetch_many(page, sorted(cities), label="city pages").values():
        if not body:
            continue
        blocks, links = parse_ld(body)
        for b in blocks:
            links = links + list(b.get("significantLink", []) or [])
        for link in links:
            p = path_of(link)
            if p and STORE_PATH.match(p):
                stores.add(urljoin(BASE_URL, p))
    return stores


# --------------------------------------------------------------------------- #
# Store parsing
# --------------------------------------------------------------------------- #
def fmt_time(t):
    """'09:00:00' -> '9:00 AM'."""
    try:
        h, m = int(t[:2]), int(t[3:5])
    except (TypeError, ValueError):
        return t or ""
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def fmt_hours(specs):
    by_day = {}
    for s in specs or []:
        day = (s.get("dayOfWeek") or "").rsplit("/", 1)[-1]
        if day and s.get("opens") and s.get("closes"):
            by_day[day] = f"{fmt_time(s['opens'])}-{fmt_time(s['closes'])}"
    return "; ".join(f"{d[:3]}: {by_day.get(d, 'Closed')}" for d in DAY_ORDER if by_day)


def store_status(name):
    """Name is 'Hollywood Feed', 'Hollywood Feed - Opening Soon', 'Hollywood Feed Headquarters'."""
    n = (name or "").lower()
    if "soon" in n or "coming" in n:
        return "Opening Soon"
    if "headquarter" in n:
        return "Headquarters"
    return "Open"


def parse_store(blocks, url):
    biz = next((b for b in blocks if b.get("@type") == "LocalBusiness"), None)
    if not biz:
        return None

    props = {p.get("propertyID"): p.get("value") for p in biz.get("additionalProperty", [])}
    addr = biz.get("address", {}) or {}
    geo = biz.get("geo", {}) or {}

    services = props.get("location-more-info-text") or ""
    services = re.sub(r"^\s*Services Include:\s*", "", services, flags=re.I)

    street = addr.get("streetAddress", "")
    if props.get("unitAddress"):
        street = f"{street}, {props['unitAddress']}"

    name = biz.get("name", "Hollywood Feed")
    return {
        "Store ID": props.get("externalReferenceId", ""),
        "Store Name": name,
        "Status": store_status(name),
        "Address": street,
        "City": addr.get("addressLocality", ""),
        "State": addr.get("addressRegion", ""),
        "Zip": addr.get("postalCode", ""),
        "Country": addr.get("addressCountry", ""),
        "Phone": biz.get("telephone", ""),
        "Grooming Phone": props.get("groomingPhone") or "",
        "Latitude": geo.get("latitude", ""),
        "Longitude": geo.get("longitude", ""),
        "Hours": fmt_hours(biz.get("openingHoursSpecification")),
        "Services": services,
        "Grooming": "Yes" if "groom" in services.lower() else "No",
        "Dog Wash": "Yes" if "wash" in services.lower() else "No",
        "Store Page": url,
        "Google Maps": props.get("googleUrl") or "",
    }


# --------------------------------------------------------------------------- #
def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        # 1. Pass Cloudflare once
        print("Opening home page (Cloudflare check)...")
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_function("document.title !== 'Just a moment...'", timeout=45000)

        # 2. Store URLs: sitemap first, city pages as fallback
        store_urls = store_urls_from_sitemaps(page)
        if not store_urls:
            print("Sitemap empty/unavailable - falling back to city pages")
            store_urls = store_urls_from_cities(page)
        store_urls = sorted(store_urls)
        print(f"Found {len(store_urls)} store pages")

        # 3. Store details (parallel)
        bodies = fetch_many(page, store_urls, label="store pages")
        browser.close()

    records, failed = [], []
    for url in store_urls:
        body = bodies.get(url)
        store = parse_store(parse_ld(body)[0], url) if body else None
        if store:
            records.append(store)
        else:
            failed.append(url)

    if failed:
        print(f"{len(failed)} page(s) had no store data (may be non-store pages):")
        for u in failed:
            print("   ", u)

    df = pd.DataFrame(records)
    if not df.empty:
        df = df.drop_duplicates(subset="Store Page").sort_values(["State", "City", "Address"])
    # The monitor flags removals for review when some store pages failed to load.
    df.attrs["failed_pages"] = len(failed)
    return df


def main():
    start = time.time()
    df = scrape()
    if not df.empty:
        df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\nScraped {len(df)} locations -> {OUTPUT_CSV} in {time.time() - start:.0f}s")
    if not df.empty:
        print(df["Status"].value_counts().to_string())


if __name__ == "__main__":
    main()
