"""
Doggie Style Pets - store locations scraper.

The find-a-store page is a static Shopify page. Each store is an
<li class="multicolumn-list__item"> containing:
    <h3>STORE NAME</h3>
    <div class="rte"><p>street,<br>City, ST ZIP<br>phone</p></div>
    <a href="google maps link">VISIT</a>
"""
import re

import requests
from bs4 import BeautifulSoup
import pandas as pd

url = "https://www.doggiestylepets.com/pages/find-a-store"
OUTPUT_CSV = "doggie_style_pets_locations.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
PHONE = re.compile(r"\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}")


def lat_lng_from_maps(link):
    """Google Maps place links carry the pin as !3d<lat>!4d<lng>; fall back to @lat,lng."""
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", link or "")
    if not m:
        m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", link or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def parse_store(li):
    name_tag = li.find("h3")
    info = li.find("div", class_="rte")
    if not name_tag or not info:
        return None

    # Lines are separated by <br>; strip trailing commas.
    lines = [t.strip().rstrip(",").strip() for t in info.get_text("\n").split("\n") if t.strip()]

    street, city, state, zip_code, phone = "", "", "", "", ""
    for line in lines:
        csz = CITY_STATE_ZIP.match(line)
        if csz:
            city, state, zip_code = csz.group("city"), csz.group("state"), csz.group("zip")
        elif PHONE.fullmatch(line):
            phone = line
        elif not street:
            street = line

    link = li.find("a", href=True)
    maps_url = link["href"] if link else ""
    lat, lng = lat_lng_from_maps(maps_url)

    return {
        "Store Name": name_tag.get_text(strip=True).title(),
        "Address": street,
        "City": city,
        "State": state,
        "Zip": zip_code,
        "Phone": phone,
        "Latitude": lat,
        "Longitude": lng,
        "Google Maps": maps_url,
    }


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    records = []
    for li in soup.select("ul.multicolumn-list li.multicolumn-list__item"):
        store = parse_store(li)
        if store:
            records.append(store)

    df = pd.DataFrame(records).drop_duplicates(subset=["Address", "Zip"])
    return df


def main():
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"Scraped {len(df)} locations -> {OUTPUT_CSV}")
    print(df[["Store Name", "Address", "City", "State", "Zip", "Phone"]].to_string())


if __name__ == "__main__":
    main()
