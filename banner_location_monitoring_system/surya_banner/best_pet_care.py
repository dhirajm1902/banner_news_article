"""
Best Friends Pet Care - store locations scraper.

The locations page (https://bestfriendspetcare.com/locations/) uses the
WP Store Locator plugin, which loads stores from a JSON AJAX endpoint.
One request centred on the US with a huge radius returns every location.
"""
import html
import re

import pandas as pd
import requests
from bs4 import BeautifulSoup

url = "https://bestfriendspetcare.com/locations?"  # page shown in the browser
AJAX_URL = "https://www.bestfriendspetcare.com/wp-admin/admin-ajax.php"
OUTPUT_CSV = "best_friends_pet_care_locations.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": url,
    "X-Requested-With": "XMLHttpRequest",
}

PARAMS = {
    "action": "store_search",
    "lat": 39.8283,          # geographic centre of the US
    "lng": -98.5795,
    "max_results": 2000,
    "search_radius": 10000,  # miles - covers all of North America
    "autoload": 1,
}


def clean(text):
    """Decode HTML entities (&#038; -> &, &#8217; -> ') and trim."""
    text = html.unescape(text or "")
    for bad, good in {"’": "'", "‘": "'", "–": "-", "—": "-",
                      "“": '"', "”": '"', "\xa0": " "}.items():
        text = text.replace(bad, good)
    return text.strip()


def parse_hours(hours_html):
    """Turn the WPSL hours <table> into 'Monday: 6:30 AM - 7:00 PM; ...'."""
    if not hours_html:
        return ""
    soup = BeautifulSoup(hours_html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) == 2:
            day = tds[0].get_text(strip=True)
            time = re.sub(r"\s+", " ", clean(tds[1].get_text(" ", strip=True)))
            rows.append(f"{day}: {time}")
    return "; ".join(rows)


def fetch_stores():
    resp = requests.get(AJAX_URL, params=PARAMS, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    stores = fetch_stores()
    records = []
    for s in stores:
        records.append({
            "Store ID": s.get("id"),
            "Store Name": clean(s.get("store")),
            "Address": clean(s.get("address")),
            "Address 2": clean(s.get("address2")),
            "City": clean(s.get("city")),
            "State": clean(s.get("state")),
            "Zip": clean(s.get("zip")),
            "Country": clean(s.get("country")),
            "Phone": clean(s.get("phone")),
            "Email": clean(s.get("email")),
            "Website": clean(s.get("url")),
            "Location Page": clean(s.get("permalink")),
            "Latitude": s.get("lat"),
            "Longitude": s.get("lng"),
            "Status": clean(s.get("location_status")),
            "Hours": parse_hours(s.get("hours")),
        })

    df = pd.DataFrame(records).drop_duplicates(subset="Store ID")
    df = df.sort_values(["State", "City"]).reset_index(drop=True)
    return df


def main():
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"Scraped {len(df)} locations -> {OUTPUT_CSV}")
    print(df[["Store Name", "City", "State", "Phone"]].head(10).to_string())


if __name__ == "__main__":
    main()
