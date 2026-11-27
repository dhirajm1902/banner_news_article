"""
Kahoots Feed & Pet - store locations scraper.

Step 1: https://kahootsfeedandpet.com/pages/locations lists every store as
        <div class="location-card" data-storetype="feed_and_pet|pet" data-storeState="california">
            ... <a class="info" href="/pages/kahoots-<slug>"> <h2>Ramona</h2>
            ... .service-icon img[alt]  (Veterinarian Icon, Teeth Cleaning Icon, ...)
Step 2: each store page has
        <div data-kah-loc2-header data-lat data-lng data-weekday data-weekend data-special>
        p.kah-loc2-header__address, a.kah-loc2-header__phone
        Store pages are fetched in parallel (no Cloudflare on this site).
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://kahootsfeedandpet.com"
url = BASE + "/pages/locations"
OUTPUT_CSV = "kahoots_locations.csv"
WORKERS = 5  # parallel store-page requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

STORE_TYPES = {"feed_and_pet": "Feed & Pet", "pet": "Pet"}
ADDRESS = re.compile(r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def get_soup(session, page_url):
    resp = session.get(page_url, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def get_store_cards(session):
    soup = get_soup(session, url)
    cards = []
    for card in soup.select("div.location-card"):
        link = card.select_one("a.info[href]")
        name = card.select_one("h2")
        icons = [img.get("alt", "") for img in card.select(".service-icon img")]
        icons = [re.sub(r"\s*Icon$", "", a).strip() for a in icons if a]
        cards.append({
            "Store Name": clean(name.get_text()) if name else "",
            "Store Type": STORE_TYPES.get(card.get("data-storetype", ""), card.get("data-storetype", "")),
            "State Name": (card.get("data-storestate") or "").title(),
            "Services": "; ".join(dict.fromkeys(icons)),
            "Store Page": urljoin(BASE, link["href"]) if link else "",
        })
    return cards


def get_store_details(session, page_url):
    soup = get_soup(session, page_url)
    header = soup.select_one("[data-kah-loc2-header]") or {}
    addr_tag = soup.select_one(".kah-loc2-header__address")
    phone_tag = soup.select_one("a.kah-loc2-header__phone")

    full_addr = clean(addr_tag.get_text(" ")) if addr_tag else ""
    m = ADDRESS.match(full_addr)

    hours = [header.get("data-weekday", ""), header.get("data-weekend", "")] if header else []
    return {
        "Full Address": full_addr,
        "Address": m.group("street") if m else full_addr,
        "City": m.group("city") if m else "",
        "State": m.group("state") if m else "",
        "Zip": m.group("zip") if m else "",
        "Phone": clean(phone_tag.get_text()) if phone_tag else "",
        "Latitude": header.get("data-lat", "") if header else "",
        "Longitude": header.get("data-lng", "") if header else "",
        "Hours": "; ".join(clean(h) for h in hours if h),
        "Special Hours": clean(header.get("data-special", "")) if header else "",
        "Walk-in Vet Clinic": "Yes" if soup.select_one(".kah-loc2-vet__strip") else "No",
    }


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    session = requests.Session()
    session.headers.update(HEADERS)

    cards = get_store_cards(session)
    print(f"Found {len(cards)} stores on locations page; fetching details...")

    def work(card):
        try:
            return {**card, **get_store_details(session, card["Store Page"])}
        except requests.RequestException as e:
            print(f"  failed {card['Store Page']}: {e}")
            return card

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        records = list(pool.map(work, cards))

    cols = ["Store Name", "Store Type", "Address", "City", "State", "Zip", "State Name",
            "Phone", "Latitude", "Longitude", "Hours", "Special Hours", "Services",
            "Walk-in Vet Clinic", "Full Address", "Store Page"]
    df = pd.DataFrame(records).reindex(columns=cols)
    df = df.drop_duplicates(subset="Store Page").sort_values(["State", "City"]).reset_index(drop=True)
    return df


def main():
    start = time.time()
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print(f"\nScraped {len(df)} locations -> {OUTPUT_CSV} in {time.time() - start:.1f}s")
    print(df["Store Type"].value_counts().to_string())
    print(df[["Store Name", "Store Type", "City", "State", "Phone"]].to_string())


if __name__ == "__main__":
    main()
