"""
normalize.py - turn each scraper's DataFrame into one standard location record.

Every scraper in surya_banner/ outputs different column names ("Address" vs
"Street", "Store ID" vs "Store Code", full state names vs abbreviations...).
This module maps them onto one schema and builds the identity keys that the
comparison engine matches on.
"""
from __future__ import annotations

import json
import math
import re

# Standard fields stored for every location (and compared for UPDATED).
STANDARD_FIELDS = [
    "external_store_id", "store_name", "address", "city", "state", "zip_code",
    "country", "phone", "latitude", "longitude", "status", "store_page",
]

# Candidate source columns for each standard field, first non-empty wins.
COLUMN_MAP = {
    "store_name": ["Store Name", "Business Name"],
    "address": ["Address", "Street"],
    "address2": ["Address 2"],
    "city": ["City"],
    "state": ["State"],
    "zip_code": ["Zip", "Postal Code"],
    "country": ["Country"],
    "phone": ["Phone"],
    "latitude": ["Latitude"],
    "longitude": ["Longitude"],
    "status": ["Status"],
    "store_page": ["Store Page", "Location Page", "Google Maps"],
}

STATE_ABBR = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "d.c.": "DC",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "puerto rico": "PR",
    # Canada
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB", "new brunswick": "NB",
    "newfoundland and labrador": "NL", "nova scotia": "NS", "ontario": "ON",
    "prince edward island": "PE", "quebec": "QC", "québec": "QC", "saskatchewan": "SK",
}

COUNTRY = {"us": "US", "usa": "US", "united states": "US", "united states of america": "US",
           "ca": "CA", "canada": "CA"}

# Words shortened so "123 North Main Street, Suite 4" == "123 N Main St Ste 4".
ADDRESS_ABBR = {
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd", "road": "rd",
    "drive": "dr", "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy",
    "highway": "hwy", "hiway": "hwy", "expressway": "expy", "freeway": "fwy",
    "circle": "cir", "terrace": "ter", "trail": "trl", "square": "sq", "plaza": "plz",
    "center": "ctr", "centre": "ctr", "crossing": "xing", "suite": "ste", "unit": "ste",
    "building": "bldg", "floor": "fl", "apartment": "apt", "north": "n", "south": "s",
    "east": "e", "west": "w", "northeast": "ne", "northwest": "nw", "southeast": "se",
    "southwest": "sw", "first": "1st", "second": "2nd", "third": "3rd", "mount": "mt",
    "saint": "st", "fort": "ft", "number": "", "no": "",
}

# Raw status text -> standard status.
STATUS_RULES = [
    (re.compile(r"coming|soon|opening|upcoming", re.I), "Coming Soon"),
    (re.compile(r"temporar", re.I), "Temporarily Closed"),
    (re.compile(r"closed|^c$|inactive", re.I), "Closed"),
    (re.compile(r"headquarter", re.I), "Headquarters"),
    (re.compile(r"open|active|^a$|^yes$|^true$", re.I), "Open"),
]


def text(value) -> str:
    """Stringify a cell: NaN/None -> '', floats like 62.0 -> '62', trim spaces."""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    return re.sub(r"\s+", " ", str(value)).strip()


def first(row: dict, columns: list[str]) -> str:
    for col in columns:
        val = text(row.get(col))
        if val:
            return val
    return ""


def norm_state(value: str) -> str:
    v = text(value)
    return STATE_ABBR.get(v.lower(), v.upper() if len(v) == 2 else v)


def norm_zip(value: str) -> str:
    v = text(value).upper()
    if re.fullmatch(r"\d{1,5}", v):
        return v.zfill(5)               # 3861 -> 03861 (lost leading zero)
    m = re.match(r"^(\d{5})(?:-?\d{4})?$", v)
    if m:
        return m.group(1)               # ZIP+4 -> ZIP5
    m = re.fullmatch(r"([A-Z]\d[A-Z])\s?(\d[A-Z]\d)", v)
    if m:
        return f"{m.group(1)} {m.group(2)}"  # Canadian postal code
    return v


def norm_phone(value: str) -> str:
    v = text(value).split(",")[0]
    digits = re.sub(r"\D", "", v)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return v


def norm_coord(value) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else round(f, 6)


def norm_status(raw: str, row: dict) -> str:
    raw = text(raw)
    if not raw and text(row.get("Open")):  # Mud Bay: "Open" Yes/No column
        raw = "open" if text(row.get("Open")).lower() == "yes" else "temporarily closed"
    if not raw:
        return "Open"
    for pattern, label in STATUS_RULES:
        if pattern.search(raw):
            return label
    return raw.title()


def address_key(address: str, zip_code: str) -> str:
    """Normalized street address + ZIP5 - used for identity matching."""
    a = text(address).lower().replace("&", " and ")
    a = re.sub(r"[.,#'\"()/\\-]", " ", a)
    words = [ADDRESS_ABBR.get(w, w) for w in a.split()]
    a = " ".join(w for w in words if w)
    return f"{a}|{norm_zip(zip_code)[:5].lower()}" if a else ""


def name_key(store_name: str, city: str, state: str) -> str:
    """Fallback identity: name + city + state (low confidence, needs review)."""
    n = re.sub(r"[^a-z0-9]+", " ", text(store_name).lower()).strip()
    c = re.sub(r"[^a-z0-9]+", " ", text(city).lower()).strip()
    return f"{n}|{c}|{norm_state(state).lower()}" if (n or c) else ""


def json_safe(value):
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):  # numpy scalars
        return value.item()
    return value


def normalize_row(row: dict, company: dict) -> dict:
    """Map one raw scraper row to the standard record + identity keys."""
    id_col = company.get("id_field")
    rec = {f: first(row, COLUMN_MAP.get(f, [])) for f in STANDARD_FIELDS if f in COLUMN_MAP}
    addr2 = first(row, COLUMN_MAP["address2"])
    if addr2 and addr2.lower() not in rec["address"].lower():
        rec["address"] = f"{rec['address']}, {addr2}" if rec["address"] else addr2

    rec["external_store_id"] = text(row.get(id_col)) if id_col else ""
    rec["state"] = norm_state(rec["state"])
    rec["zip_code"] = norm_zip(rec["zip_code"])
    rec["country"] = COUNTRY.get(rec["country"].lower(), rec["country"]) or company.get("country", "US")
    rec["phone"] = norm_phone(rec["phone"])
    rec["latitude"] = norm_coord(rec["latitude"])
    rec["longitude"] = norm_coord(rec["longitude"])
    rec["status"] = norm_status(rec["status"], row)
    if not rec["store_name"]:
        rec["store_name"] = company["company_name"]

    rec["address_key"] = address_key(rec["address"], rec["zip_code"])
    rec["name_key"] = name_key(rec["store_name"], rec["city"], rec["state"])
    if rec["external_store_id"]:
        rec["location_key"] = f"id:{rec['external_store_id']}"
    elif rec["address_key"]:
        rec["location_key"] = f"addr:{rec['address_key']}"
    else:
        rec["location_key"] = f"name:{rec['name_key']}"

    rec["raw_data"] = {k: json_safe(v) for k, v in row.items()}
    return rec


def normalize_frame(df, company: dict) -> tuple[list[dict], list[str]]:
    """Normalize a scraper DataFrame. Returns (records, warnings)."""
    warnings, seen, records = [], set(), []
    for row in df.to_dict(orient="records"):
        rec = normalize_row(row, company)
        if not rec["location_key"].split(":", 1)[1]:
            warnings.append(f"Skipped row with no id/address/name: {json.dumps(rec['raw_data'])[:200]}")
            continue
        if rec["location_key"] in seen:
            warnings.append(f"Duplicate location key skipped: {rec['location_key']}")
            continue
        seen.add(rec["location_key"])
        records.append(rec)
    return records, warnings
