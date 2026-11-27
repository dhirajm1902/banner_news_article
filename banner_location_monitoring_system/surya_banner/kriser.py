"""
Kriser's - store locations scraper.

The locator (https://stores.krisers.com/) is a Next.js site (same platform as
Chuck & Don's). Every store is embedded in the page's
<script id="__NEXT_DATA__"> JSON under props.pageProps.locations,
so one request gets all locations.
"""
import json
import re

import requests
from bs4 import BeautifulSoup
import pandas as pd

url = "https://stores.krisers.com/"
OUTPUT_CSV = "krisers_locations.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

STATUS = {"A": "Active", "C": "Closed", "T": "Temporarily Closed"}


def yes_no(value):
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return ""


def format_phone(phone):
    """Normalise messy formats like '(720-823-0172' to '(720) 823-0172'."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return (phone or "").strip()


def fetch_locations():
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise RuntimeError("__NEXT_DATA__ not found - page layout may have changed")
    data = json.loads(script.string)
    return data["props"]["pageProps"]["locations"]


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    locations = fetch_locations()
    records = []
    for loc in locations:
        custom = loc.get("custom") or {}
        records.append({
            "Store Code": loc.get("storeCode"),
            "Store Name": custom.get("storetitle") or loc.get("businessName"),
            "Business Name": loc.get("businessName"),
            "Address": ", ".join(loc.get("addressLines") or []),
            "City": loc.get("city"),
            "State": loc.get("state"),
            "Zip": loc.get("postalCode"),
            "Country": loc.get("country"),
            "Phone": ", ".join(format_phone(p) for p in (loc.get("phoneNumbers") or [])),
            "Latitude": loc.get("latitude"),
            "Longitude": loc.get("longitude"),
            "Hours": "; ".join(
                " ".join(h.replace("|", ": ").split())
                for h in (loc.get("formattedBusinessHours") or [])
            ),
            "Grooming": yes_no(custom.get("grooming")),
            "Self-Wash": yes_no(custom.get("selfwash")),
            "Store Manager": custom.get("store_manager", ""),
            "Opening Date": loc.get("openingDate"),
            "Closing Date": loc.get("closingDate"),
            "Status": STATUS.get(loc.get("_status"), loc.get("_status")),
            "Store Page": url.rstrip("/") + (loc.get("localPageUrl") or ""),
        })

    df = pd.DataFrame(records).drop_duplicates(subset="Store Code")
    df = df.sort_values(["State", "City"]).reset_index(drop=True)
    return df


def main():
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"Scraped {len(df)} locations -> {OUTPUT_CSV}")
    print(df[["Store Name", "City", "State", "Phone", "Grooming", "Self-Wash", "Opening Date"]].to_string())


if __name__ == "__main__":
    main()
