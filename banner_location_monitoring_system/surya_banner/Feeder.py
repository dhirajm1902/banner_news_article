"""
Feeders Pet Supply - store locations scraper.

Step 1: https://www.feederspetsupply.com/pages/store-locator lists every store
        as <div class="sl-item" data-map="...center=lat,lng...">
            <span class="sl-city">, <span class="sl-address">, <a class="sl-phone">,
            <a class="sl-more" href="/pages/store-locator/<slug>">
Step 2: each "More Details" page has <script id="location-data"> JSON with
        zip, store hours and pet-wash hours, which the list page lacks.
"""
import json
import re
import time
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup
import pandas as pd

BASE = "https://www.feederspetsupply.com"
url = BASE + "/pages/store-locator"
OUTPUT_CSV = "feeders_pet_supply_locations.csv"
DELAY = 0.5  # seconds between detail-page requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

STATE_ABBR = {
    "Alabama": "AL", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Florida": "FL", "Georgia": "GA", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kentucky": "KY", "Michigan": "MI", "Missouri": "MO", "North Carolina": "NC",
    "Ohio": "OH", "Pennsylvania": "PA", "South Carolina": "SC", "Tennessee": "TN",
    "Texas": "TX", "Virginia": "VA", "West Virginia": "WV", "Wisconsin": "WI",
}

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def fmt_time(hhmm):
    """'09:00' -> '9:00 AM'."""
    try:
        h, m = map(int, hhmm.split(":"))
    except (AttributeError, ValueError):
        return hhmm or ""
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def as_dict(value):
    """Hours come as a dict, or sometimes a JSON string like '{}' (= none)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def fmt_hours(hours):
    """{'monday': {'open': '09:00', 'close': '20:00'}, ...} -> 'Mon: 9:00 AM-8:00 PM; ...'."""
    hours = as_dict(hours)
    if not hours:
        return ""
    out = []
    for day in DAYS:
        h = hours.get(day)
        label = day[:3].title()
        if h and h.get("open") and h.get("close"):
            out.append(f"{label}: {fmt_time(h['open'])}-{fmt_time(h['close'])}")
        elif day in hours:
            out.append(f"{label}: Closed")
    return "; ".join(out)


def fmt_phone(raw):
    digits = re.sub(r"\D", "", raw or "")[-10:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}" if len(digits) == 10 else clean(raw)


def get_store_list(session):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    stores = []
    for item in soup.select("div.sl-item"):
        city = item.select_one(".sl-city")
        addr = item.select_one(".sl-address")
        phone = item.select_one(".sl-phone")
        more = item.select_one("a.sl-more")

        data_map = unquote(item.get("data-map", ""))
        center = re.search(r"center=(-?\d+\.?\d*),(-?\d+\.?\d*)", data_map)

        full_addr = clean(addr.get_text()) if addr else ""
        state = full_addr.rsplit(",", 1)[-1].strip() if "," in full_addr else ""

        stores.append({
            "Store Name": clean(city.get_text()) if city else "",
            "Full Address": full_addr,
            "State": state,
            "Phone": clean(phone.get_text()) if phone else "",
            "Latitude": center.group(1) if center else "",
            "Longitude": center.group(2) if center else "",
            "Store Page": urljoin(BASE, more["href"]) if more else "",
        })
    return stores


def get_store_details(session, page_url):
    resp = session.get(page_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find("script", id="location-data")
    return json.loads(tag.string) if tag and tag.string else {}


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    session = requests.Session()
    session.headers.update(HEADERS)

    stores = get_store_list(session)
    print(f"Found {len(stores)} stores on locator page; fetching details...")

    records = []
    for i, s in enumerate(stores, 1):
        d = {}
        if s["Store Page"]:
            try:
                d = get_store_details(session, s["Store Page"])
            except (requests.RequestException, ValueError) as e:
                print(f"  [{i}] {s['Store Page']} failed: {e}")
            time.sleep(DELAY)

        street = ", ".join(p for p in (d.get("address1"), d.get("address2")) if p)
        records.append({
            "Store Name": clean(d.get("name")) or s["Store Name"],
            "Street": street or s["Full Address"].rsplit(",", 2)[0],
            "City": d.get("city") or s["Store Name"],
            "State": STATE_ABBR.get((d.get("province") or s["State"]).strip().title(),
                                    (d.get("province") or s["State"]).strip()),
            "Zip": d.get("zip", ""),
            "Phone": fmt_phone(d.get("phone")) if d.get("phone") else s["Phone"],
            "Latitude": d.get("latitude") or s["Latitude"],
            "Longitude": d.get("longitude") or s["Longitude"],
            "Store Hours": fmt_hours(d.get("hours")),
            "Pet Wash": "Yes" if as_dict(d.get("petHours")) else ("No" if d else ""),
            "Pet Wash Hours": fmt_hours(d.get("petHours")),
            "Store Page": s["Store Page"],
        })
        print(f"  [{i}/{len(stores)}] {records[-1]['Store Name']}")

    df = pd.DataFrame(records).drop_duplicates(subset="Store Page")
    df = df.sort_values(["State", "City"]).reset_index(drop=True)
    return df


def main():
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\nScraped {len(df)} locations -> {OUTPUT_CSV}")
    print(df["State"].value_counts().to_string())


if __name__ == "__main__":
    main()
