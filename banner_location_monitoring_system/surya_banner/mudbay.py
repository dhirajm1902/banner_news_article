"""
Mud Bay - store locations scraper (patchright, gets past Cloudflare).

mudbay.com is Salesforce Commerce Cloud behind Cloudflare. The store finder
calls a JSON endpoint:
    /on/demandware.store/Sites-MudBay-Site/default/Stores-FindStores
        ?showMap=true&radius=<miles>&postalCode=<zip>
which returns {"stores": [...]} with address, phone, lat/lng, hours, flags.

We open the store finder ONCE in a real browser (passes Cloudflare), then call
the endpoint with fetch() from inside that page. A few large-radius searches
from different ZIPs are merged by store ID so no store is missed.
"""
import html
import json
import re
import time

import pandas as pd
from patchright.sync_api import sync_playwright

SITE = "https://www.mudbay.com"
url = SITE + "/storefinder?showMap=true&isForm=true"
API = "/on/demandware.store/Sites-MudBay-Site/default/Stores-FindStores?showMap=true&radius={radius}&postalCode={zip}"
DETAILS = SITE + "/on/demandware.store/Sites-MudBay-Site/default/Stores-StoreDetails?storeID={id}"
OUTPUT_CSV = "mudbay_locations.csv"
PROFILE_DIR = ".browser_profile_mudbay"  # reused between runs (keeps Cloudflare cookie)

RADIUS = 3000
# Search centres: Seattle, Portland, Spokane, Bend - all stores are in WA/OR,
# one search already returns them all; the extras are a safety net.
SEARCH_ZIPS = ["98107", "97201", "99201", "97701"]

FETCH_JS = """
async (u) => {
    const r = await fetch(u, {headers: {"X-Requested-With": "XMLHttpRequest"}});
    return [r.status, await r.text()];
}
"""


def yes_no(value):
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return ""


def split_hours(hours_html):
    """Return (regular hours, holiday hours) as '; '-joined plain text."""
    text = html.unescape(re.sub(r"<br\s*/?>|</p>", "\n", hours_html or "", flags=re.I))
    text = re.sub(r"<[^>]+>", "", text).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]

    regular, holiday, in_holiday = [], [], False
    for ln in lines:
        if ln.lower().startswith("holiday hours"):
            in_holiday = True
            continue
        (holiday if in_holiday else regular).append(ln)
    return "; ".join(regular), "; ".join(holiday)


def format_phone(phone):
    digits = re.sub(r"\D", "", phone or "")[-10:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else (phone or "")


def pass_cloudflare(page, attempts=2):
    """Load the store finder and wait until Cloudflare lets us through."""
    for attempt in range(1, attempts + 1):
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_function("document.title !== 'Just a moment...'", timeout=20000)
            return
        except Exception:  # noqa: BLE001
            print("  Cloudflare check still showing - if a checkbox is visible "
                  "in the browser window, please click it (waiting up to 2 min)...")
        try:
            page.wait_for_function("document.title !== 'Just a moment...'", timeout=120000)
            return
        except Exception:  # noqa: BLE001
            print(f"  attempt {attempt} timed out, reloading...")
    raise RuntimeError("Could not get past the Cloudflare check")


def fetch_stores():
    stores = {}
    with sync_playwright() as p:
        # Persistent profile: the Cloudflare clearance cookie is reused on later
        # runs, so the check is usually skipped after the first successful run.
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False, no_viewport=True
        )
        page = context.pages[0] if context.pages else context.new_page()

        print("Opening store finder (Cloudflare check)...")
        pass_cloudflare(page)
        page.wait_for_timeout(2000)

        for zip_code in SEARCH_ZIPS:
            status, body = page.evaluate(FETCH_JS, API.format(radius=RADIUS, zip=zip_code))
            if status != 200:
                print(f"  {zip_code}: HTTP {status}")
                continue
            try:
                found = json.loads(body).get("stores", [])
            except ValueError:
                print(f"  {zip_code}: response was not JSON")
                continue
            new = sum(1 for s in found if s.get("ID") not in stores)
            for s in found:
                stores.setdefault(s.get("ID"), s)
            print(f"  search {zip_code}: {len(found)} stores ({new} new)")
            time.sleep(0.5)

        context.close()
    return list(stores.values())


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    stores = fetch_stores()

    records = []
    for s in stores:
        hours, holiday = split_hours(s.get("storeHours"))
        street = ", ".join(x for x in (s.get("address1"), s.get("address2")) if x)
        records.append({
            "Store ID": s.get("ID"),
            "Store Name": f"Mud Bay {s.get('name', '')}".strip(),
            "Address": street,
            "City": s.get("city"),
            "State": s.get("stateCode"),
            "Zip": s.get("postalCode"),
            "Country": s.get("countryCode"),
            "Phone": format_phone(s.get("phone")),
            "Latitude": s.get("latitude"),
            "Longitude": s.get("longitude"),
            "Hours": hours,
            "Holiday Hours": holiday,
            "Open": yes_no(s.get("storeOpenStatus")),
            "Curbside Pickup": yes_no(s.get("curbsideAvailable")),
            "Local Delivery": yes_no(s.get("localDeliveryAvailable")),
            "Pet Delivery": yes_no(s.get("petDelivery")),
            "Store Page": DETAILS.format(id=s.get("ID")),
        })

    df = pd.DataFrame(records).drop_duplicates(subset="Store ID")
    df = df.sort_values(["State", "City", "Store Name"]).reset_index(drop=True)
    return df


def main():
    start = time.time()
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\nScraped {len(df)} locations -> {OUTPUT_CSV} in {time.time() - start:.0f}s")
    print(df["State"].value_counts().to_string())
    print(df[["Store Name", "City", "State", "Phone"]].head(10).to_string())


if __name__ == "__main__":
    main()
