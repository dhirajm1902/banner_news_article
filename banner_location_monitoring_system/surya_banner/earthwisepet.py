"""
EarthWise Pet (and family brands) - store locations scraper.

https://earthwisepet.com/pet-stores-near-me only renders ~24 cards in HTML
("Show more" reveals the rest client-side), but the page embeds the full
store list for all 166 locations in its hydration script as JS objects like:

  {id:"1433",brand:"Dee-O-Gee",name:"Billings",region:"North Mountain",
   address:"27 Shiloh Rd Suite #1",city:"Billings",state:"MT",zip:"59106",
   phone:"(406) 534-4245",email:"",groomingTables:3,groomingTubs:2,
   selfWashTubs:0,lat:45.7753,lng:-108.6519,mobile:void 0}

We parse those objects directly. The JSON-LD ItemList on the same page is
used as a fallback if the script format ever changes.
"""
import json
import re

import requests
from bs4 import BeautifulSoup
import pandas as pd

url = "https://earthwisepet.com/pet-stores-near-me"
OUTPUT_CSV = "earthwise_pet_locations.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

# One store object: {id:"...", ... mobile:...}
STORE_OBJ = re.compile(r'\{id:"\d+",brand:.*?mobile:[^}]*\}', re.S)
# key:value pairs inside it ("string", number, !0 / !1, void 0)
KEY_VAL = re.compile(r'(\w+):("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|!0|!1|void 0)')


def js_value(raw):
    if raw.startswith('"'):
        return json.loads(raw)  # handles \" and \u escapes
    if raw == "!0":
        return True
    if raw == "!1":
        return False
    if raw == "void 0":
        return None
    return float(raw) if "." in raw else int(raw)


def parse_hydration(page_html):
    stores = []
    for obj in STORE_OBJ.findall(page_html):
        stores.append({k: js_value(v) for k, v in KEY_VAL.findall(obj)})
    return stores


def parse_json_ld(page_html):
    """Fallback: schema.org ItemList (no grooming/region data)."""
    soup = BeautifulSoup(page_html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        data = json.loads(tag.string or "{}")
        if data.get("@type") != "ItemList":
            continue
        stores = []
        for el in data.get("itemListElement", []):
            it = el.get("item", {})
            addr, geo = it.get("address", {}), it.get("geo", {})
            stores.append({
                "brand": it.get("brand"), "name": it.get("name"),
                "address": addr.get("streetAddress"), "city": addr.get("addressLocality"),
                "state": addr.get("addressRegion"), "zip": addr.get("postalCode"),
                "phone": it.get("telephone"), "email": it.get("email"),
                "lat": geo.get("latitude"), "lng": geo.get("longitude"),
            })
        return stores
    return []


def yes_no(count):
    if count is None:
        return ""
    return "Yes" if count > 0 else "No"


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    page_html = resp.text

    stores = parse_hydration(page_html)
    source = "hydration script"
    if not stores:
        stores = parse_json_ld(page_html)
        source = "JSON-LD fallback"

    records = []
    for s in stores:
        records.append({
            "Store ID": s.get("id"),
            "Brand": s.get("brand"),
            "Store Name": s.get("name"),
            "Region": s.get("region"),
            "Address": s.get("address"),
            "City": s.get("city"),
            "State": s.get("state"),
            "Zip": s.get("zip"),
            "Phone": s.get("phone"),
            "Email": s.get("email"),
            "Latitude": s.get("lat"),
            "Longitude": s.get("lng"),
            "Grooming Salon": yes_no(s.get("groomingTables")),
            "Grooming Tables": s.get("groomingTables"),
            "Grooming Tubs": s.get("groomingTubs"),
            "Self-Wash": yes_no(s.get("selfWashTubs")),
            "Self-Wash Tubs": s.get("selfWashTubs"),
            "Mobile Grooming": "Yes" if s.get("mobile") else "",
        })

    df = pd.DataFrame(records).drop_duplicates(subset="Store ID")
    # Some GROOMBAR (mobile grooming) entries have no street address, so only
    # de-duplicate rows that actually have one. When the same store is listed
    # twice (e.g. Milton, GA), keep the copy with the most grooming detail.
    has_addr = df["Address"].fillna("").str.strip() != ""
    df = pd.concat([
        df[has_addr]
        .sort_values("Grooming Tables", ascending=False)
        .drop_duplicates(subset=["Brand", "Address", "Zip"]),
        df[~has_addr],
    ])
    df = df.sort_values(["State", "City", "Brand"]).reset_index(drop=True)
    print(f"Parsed {len(df)} stores via {source}")
    return df


def main():
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"Scraped {len(df)} locations -> {OUTPUT_CSV}")
    print(df["Brand"].value_counts().to_string())
    print(df[["Brand", "Store Name", "City", "State", "Phone", "Grooming Salon"]].head(10).to_string())


if __name__ == "__main__":
    main()
