"""
Friendly Pets - store locations scraper.

https://friendlypets.com/locations is a Divi (WordPress) page. Each store is a
text module <div class="et_pb_text ... amazing_stores"> inside an
et_pb_column, laid out as:
    <h3>Lee Store</h3>
    <p><img>  street <br> City, ST ZIP <br> <strong>Ph:</strong> phone</p>
    <p><strong>Amenities:</strong></p>
    <ul><li>Self Serve Dog Wash</li>...</ul>
"""
import re

import requests
from bs4 import BeautifulSoup
import pandas as pd

url = 'https://friendlypets.com/locations'
OUTPUT_CSV = "friendly_pets_locations.csv"

headers = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36 "
}

CITY_STATE_ZIP = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
PHONE = re.compile(r"\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}")


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_store(block):
    title = block.find(["h1", "h2", "h3", "h4"])
    name = clean(title.get_text(" ")) if title else ""
    name = re.sub(r"\s*\bStore\b\s*$", "", name, flags=re.I)  # "Lee Store" -> "Lee"

    street, city, state, zip_code, phone = "", "", "", "", ""
    for p in block.find_all("p"):
        lines = [clean(t) for t in p.get_text("\n").split("\n") if clean(t)]
        for line in lines:
            csz = CITY_STATE_ZIP.match(line)
            if csz:
                city, state, zip_code = csz.group("city"), csz.group("state"), csz.group("zip")
            elif PHONE.search(line):
                phone = PHONE.search(line).group(0)
            elif line.rstrip(":").lower() in ("ph", "phone", "amenities"):
                continue
            elif not street and not city:
                street = line

    amenities = [clean(li.get_text()) for li in block.select("ul li")]

    return {
        "Store Name": f"Friendly Pets {name}".strip(),
        "Address": street,
        "City": city,
        "State": state,
        "Zip": zip_code,
        "Phone": phone,
        "Amenities": "; ".join(amenities),
        "Self Serve Dog Wash": "Yes" if any("dog wash" in a.lower() for a in amenities) else "No",
        "Small Animal Boarding": "Yes" if any("boarding" in a.lower() for a in amenities) else "No",
    }


def scrape():
    """Return every location as a DataFrame (used by the location monitor)."""
    req = requests.get(url, headers=headers, timeout=30)
    req.raise_for_status()
    soup = BeautifulSoup(req.text, "html.parser")

    records = []
    for block in soup.select("div.et_pb_text.amazing_stores"):
        store = parse_store(block)
        if store["Address"] or store["Phone"]:  # skip non-store text modules
            records.append(store)

    return pd.DataFrame(records).drop_duplicates(subset=["Address", "Zip"])


def main():
    df = scrape()
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    print(f"Scraped {len(df)} locations -> {OUTPUT_CSV}")
    print(df.to_string())


if __name__ == "__main__":
    main()
