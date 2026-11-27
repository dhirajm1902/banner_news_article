"""
Dogtopia - store locations scraper.

https://www.dogtopia.com/location-finder/store-listing/ is server-rendered:
    section.store-list__country   (h2 = country)
      details.store-list__state   (summary h3 = state / province)
        li.store-list__card       (one store)
            h3 a span             -> store name, a[href] -> store page
            span.store-list__badge-> "Coming soon!" (only on upcoming stores)
            a.store-list__contact -> address (maps link) and phone (tel:)
"""
import re

import requests
from bs4 import BeautifulSoup
import pandas as pd

url = "https://www.dogtopia.com/location-finder/store-listing/"
OUTPUT_CSV = "dogtopia_locations.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

# Last part of the address: "Alabama 35233" / "Ontario L4B 1B4" / "TX 75001"
REGION_POSTAL = re.compile(
    r"^(?P<region>.*?)\s*(?P<postal>\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d)?$", re.I
)


# The site has a few duplicate accordions labelled with abbreviations ("CA", "FL", "NY").
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "D.C.", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming",
}


def clean(text):
    text = (text or "").replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def split_address(full):
    """'1200 2nd Ave S, Birmingham, Alabama 35233' -> street, city, region, postal."""
    parts = [p.strip() for p in full.split(",") if p.strip()]
    if parts and parts[-1].lower() in ("united states", "usa", "canada"):
        parts = parts[:-1]
    if len(parts) < 3:
        return full, "", "", ""
    m = REGION_POSTAL.match(parts[-1])
    region = m.group("region") if m else parts[-1]
    postal = (m.group("postal") or "") if m else ""
    return ", ".join(parts[:-2]), parts[-2], region, postal.upper()


def parse_card(card, country, state):
    link = card.select_one(".store-list__card-header h3 a")
    name_span = link.find("span") if link else card.find("h3")
    name = clean(name_span.get_text()) if name_span else ""

    badge = card.select_one(".store-list__badge")
    status = clean(badge.get_text()) if badge else "Open"

    address, phone = "", ""
    for a in card.select("a.store-list__contact"):
        for sr in a.select(".screen-reader-text"):
            sr.decompose()
        if a.get("href", "").startswith("tel:"):
            phone = clean(a.get_text())
        else:
            address = clean(a.get_text())
    if phone == "000-000-0000":  # placeholder used for coming-soon stores
        phone = ""

    street, city, region, postal = split_address(address)

    return {
        "Store Name": name,
        "Status": status,
        "Full Address": address,
        "Street": street,
        "City": city,
        "State": STATE_NAMES.get((state or region).upper(), state or region),
        "Zip": postal,
        "Country": country,
        "Phone": phone,
        "Store Page": link["href"] if link and link.has_attr("href") else "",
    }


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    records = []
    for country_sec in soup.select("section.store-list__country"):
        title = country_sec.select_one(".store-list__country-title")
        country = clean(title.get_text()) if title else ""
        for state_block in country_sec.select("details.store-list__state"):
            state_h = state_block.select_one("summary h3")
            state = clean(state_h.get_text()) if state_h else ""
            for card in state_block.select("li.store-list__card"):
                records.append(parse_card(card, country, state))

    df = pd.DataFrame(records).drop_duplicates(subset=["Store Page", "Full Address"])
    return df


def main():
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"Scraped {len(df)} locations -> {OUTPUT_CSV}")
    print(df["Country"].value_counts().to_string())
    print(df["Status"].value_counts().to_string())
    print(df[["Store Name", "City", "State", "Zip", "Phone", "Status"]].head(10).to_string())


if __name__ == "__main__":
    main()
