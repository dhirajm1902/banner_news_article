"""
New Banner News fetcher.
Same Google News RSS logic as fetch_banner_store_news.py, but driven by
"New Banner list.xlsx" (Banner / High priority / Industry cohort / Analyst).
Writes new_banner_news.json (read by the "New Banner News" tab in index.html),
data/new_banner_news/ daily CSV + JSON archive, and
master_file/new_banner_news_master.csv.
"""
import feedparser
from urllib.parse import quote_plus
import time
from datetime import date, timedelta
from pathlib import Path
from dateutil import parser as date_parser
import pandas as pd
from collections import defaultdict
import json
import re
from html import unescape
import base64

# ────────────────────────────────────────────────
# Output directory setup
# ────────────────────────────────────────────────
STORE_NEWS_DIR = Path("data/new_banner_news")
JSON_ARCHIVE_DIR = Path("data/new_banner_news/json_archive")
STORE_NEWS_DIR.mkdir(parents=True, exist_ok=True)
JSON_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


# ────────────────────────────────────────────────
# Industries — keyword map used to narrow queries
# ────────────────────────────────────────────────
INDUSTRIES = {
    "Apparel": [
        "clothing store", "fashion retailer", "apparel store", "apparel chain",
        "clothing chain", "fashion chain", "garment store",
    ],
    "Arts & Crafts": [
        "arts and crafts store", "hobby store", "craft store", "craft retailer",
        "hobby shop",
    ],
    "Auto Parts": [
        "auto parts store", "auto parts retailer", "automotive parts",
        "car parts store",
    ],
    "Books / Toys / Gaming": [
        "bookstore", "book store", "toy store", "toy retailer",
        "gaming store", "game store",
    ],
    "Convenience Stores / Distributors": [
        "convenience store", "c-store", "gas station convenience",
        "fuel station store", "convenience chain",
    ],
    "Cruise Lines": [
        "cruise line", "cruise terminal", "cruise port", "cruise ship homeport",
    ],
    "Department Stores": [
        "department store", "department chain",
    ],
    "Drug Retailers": [
        "pharmacy chain", "drugstore chain", "drug store", "drug retailer",
        "retail pharmacy",
    ],
    "Electronics / Distributors": [
        "electronics store", "electronics retailer", "consumer electronics store",
        "tech retailer",
    ],
    "Experiential": [
        "entertainment venue", "experiential retail", "family entertainment",
        "escape room", "trampoline park", "adventure park",
    ],
    "Facility & Support Services": [
        "facility services", "facilities management", "support services provider",
    ],
    "Financial Services": [
        "bank branch", "credit union branch", "financial services branch",
        "bank location",
    ],
    "Foodservice Distributors": [
        "foodservice distributor", "food distributor", "restaurant supply",
    ],
    "Footwear": [
        "shoe store", "footwear store", "footwear retailer", "shoe retailer",
        "sneaker store",
    ],
    "Furniture / Mattress": [
        "furniture store", "mattress store", "furniture retailer",
        "mattress retailer", "home furniture store",
    ],
    "Grocery": [
        "grocery store", "supermarket", "food store", "grocery chain",
        "supermarket chain",
    ],
    "Grocery Wholesale": [
        "wholesale grocery", "warehouse club", "wholesale club",
        "cash and carry",
    ],
    "Gyms / Fitness": [
        "gym", "fitness center", "fitness studio", "health club",
        "fitness chain",
    ],
    "Health / Beauty": [
        "beauty store", "beauty retailer", "salon", "spa", "health store",
        "wellness store", "cosmetics store",
    ],
    "Home Improvement / Building Materials": [
        "home improvement store", "hardware store", "building materials store",
        "home improvement chain",
    ],
    "Hospitality": [
        "hotel", "motel", "resort", "inn", "hotel chain", "hotel brand",
    ],
    "Housewares & Home Furnishings": [
        "housewares store", "home goods store", "home furnishings store",
        "kitchenware store",
    ],
    "International": [
        "international retailer", "global retail chain",
    ],
    "Jewelry & Accessories": [
        "jewelry store", "jewellery store", "accessories store",
        "jewelry retailer",
    ],
    "Mass Merchandisers": [
        "mass merchandiser", "big box store", "discount store",
        "supercenter",
    ],
    "Movie Theatres": [
        "movie theater", "movie theatre", "cinema", "multiplex",
        "theater chain",
    ],
    "Office & Computer": [
        "office supply store", "computer store", "office retailer",
        "stationery store",
    ],
    "Pet Care": [
        "pet store", "pet supply store", "pet retailer", "veterinary clinic",
        "animal hospital",
    ],
    "Pharmaceutical / Medical": [
        "medical clinic", "health center", "urgent care center",
        "medical center", "outpatient clinic",
    ],
    "Restaurants": [
        "restaurant", "fast food chain", "quick service restaurant",
        "fast casual", "diner", "eatery chain", "food chain",
    ],
    "Retail REITs": [
        "shopping center", "shopping mall", "retail plaza", "strip mall",
        "retail REIT", "outlet mall",
    ],
    "Specialty / Other": [
        "specialty retailer", "specialty store", "niche retailer",
    ],
    "Sporting Goods": [
        "sporting goods store", "sports retailer", "outdoor retailer",
        "sports chain",
    ],
}

# Pre-build a flat lookup: industry name → OR string of quoted keywords
# e.g. '"pet store" OR "pet supply store" OR ...'
INDUSTRY_QUERY_STRINGS: dict[str, str] = {
    industry: " OR ".join(f'"{kw}"' for kw in keywords)
    for industry, keywords in INDUSTRIES.items()
}


# ────────────────────────────────────────────────
# Google News URL Decoder
# ────────────────────────────────────────────────

def _decode_gnews_blob(encoded: str) -> str | None:
    rem = len(encoded) % 4
    if rem:
        encoded += '=' * (4 - rem)
    try:
        data = base64.urlsafe_b64decode(encoded)
    except Exception:
        return None

    i = 0
    while i < len(data) - 4:
        if data[i] == 0x0A:
            length_byte = data[i + 1]
            if length_byte & 0x80:
                if i + 2 < len(data):
                    length = (length_byte & 0x7F) | ((data[i + 2] & 0x7F) << 7)
                    start = i + 3
                else:
                    i += 1
                    continue
            else:
                length = length_byte
                start = i + 2
            end = start + length
            if end <= len(data):
                candidate = data[start:end]
                try:
                    s = candidate.decode('utf-8')
                    if s.startswith('http'):
                        return s
                except UnicodeDecodeError:
                    pass
        i += 1

    idx = data.find(b'http')
    if idx != -1:
        chunk = data[idx:]
        end = next((j for j, b in enumerate(chunk) if b < 32), len(chunk))
        try:
            return chunk[:end].decode('utf-8', errors='ignore') or None
        except Exception:
            pass
    return None


def decode_google_news_url(entry) -> str:
    raw_link: str = entry.get('link', '')
    source = entry.get('source', {})
    source_href: str = source.get('href', '') if isinstance(source, dict) else ''
    if source_href.startswith('http') and '/' in source_href.lstrip('https://'):
        path = source_href.split('/', 3)
        if len(path) > 3 and len(path[3]) > 5:
            return source_href
    match = re.search(r'/articles/([A-Za-z0-9_=-]+)', raw_link)
    if match:
        decoded = _decode_gnews_blob(match.group(1))
        if decoded:
            return decoded
    return raw_link


# ────────────────────────────────────────────────
# Industry cohort (as written in New Banner list.xlsx) → INDUSTRIES key
# used to narrow the Google News query. "Other" falls back to the generic
# retail context in build_industry_context().
# ────────────────────────────────────────────────
COHORT_TO_INDUSTRY = {
    "Grocery":                        "Grocery",
    "Pet Stores":                     "Pet Care",
    "Convenience":                    "Convenience Stores / Distributors",
    "Big Box":                        "Mass Merchandisers",
    "Discount":                       "Mass Merchandisers",
    "Mass Merchandise":               "Mass Merchandisers",
    "Department Stores":              "Department Stores",
    "Branded Fast Food / Restaurant": "Restaurants",
    "Misc Restaurant":                "Restaurants",
}

# ────────────────────────────────────────────────
# Load banners + analyst + industry cohort + priority from New Banner list.xlsx
# Expected columns: Banner, High priority, Industry cohort, Analyst
# ────────────────────────────────────────────────
BANNER_LIST_FILE = Path("New Banner list.xlsx")

df = pd.read_excel(BANNER_LIST_FILE)
df.columns = [c.strip() for c in df.columns]
df = df.dropna(subset=['Banner'])
df['Banner']          = df['Banner'].astype(str).str.strip()
df = df[df['Banner'].str.len() > 0]
df['Analyst']         = df['Analyst'].fillna('Unassigned').astype(str).str.strip()
df['Industry cohort'] = df['Industry cohort'].fillna('Other').astype(str).str.strip()
df['High priority']   = df['High priority'].fillna('').astype(str).str.strip().str.lower() == 'yes'

stores       = df['Banner'].tolist()
analyst_map  = dict(zip(df['Banner'], df['Analyst']))
cohort_map   = dict(zip(df['Banner'], df['Industry cohort']))
industry_map = {b: COHORT_TO_INDUSTRY.get(c) for b, c in cohort_map.items()}
priority_map = dict(zip(df['Banner'], df['High priority']))

print(f"→ Loaded {len(stores)} banners from {BANNER_LIST_FILE}")
print(f"   → {len(set(analyst_map.values()))} unique analysts, "
      f"{len(set(cohort_map.values()))} industry cohorts, "
      f"{sum(priority_map.values())} high-priority banners")


# ────────────────────────────────────────────────
# Helper functions
# ────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")


def clean_summary(summary: str) -> str:
    if not summary:
        return "No summary"
    text = unescape(str(summary))
    text = _TAG_RE.sub("", text)
    text = " ".join(text.split())
    return text or "No summary"


def is_recent(published_str, cutoff_date):
    if not published_str or published_str == 'Date not available':
        return False
    try:
        pub_date = date_parser.parse(published_str).date()
        return pub_date >= cutoff_date
    except Exception:
        return False


def build_industry_context(industry: str | None) -> str:
    """
    Return an OR-grouped industry keyword clause for the query.
    Falls back to a generic retail context if industry is unknown/missing.
    """
    if industry and industry in INDUSTRY_QUERY_STRINGS:
        return f"({INDUSTRY_QUERY_STRINGS[industry]})"
    # Generic fallback — same as original retail_context
    return '(store OR location OR retail OR shop OR outlet OR station OR pharmacy OR supermarket OR grocery OR "auto parts")'


def build_query(store: str, industry: str | None = None, is_closure: bool = False) -> str:
    base_keywords_open = (
        '"new store" OR "new location" OR "opening soon" OR "coming soon" OR '
        '"grand opening" OR "now open" OR "opens new" OR "opening in" OR '
        '"to open" OR "set to open" OR "plans to open" OR "breaks ground" OR '
        '"now hiring" OR "store opening" OR "location opening" OR '
        '"will open" OR "opening date" OR "opening weekend" OR '
        '"soft opening" OR "ribbon cutting" OR "open for business" OR '
        '"doors open" OR "expanding to" OR "announced plans" OR '
        '"plans announced" OR "permit filed" OR "building permit" OR '
        '"permit application" OR "zoning approval" OR "site plan approved" OR '
        '"lease signed" OR "signed lease" OR "retail space leased" OR '
        '"land acquired" OR "site acquired" OR "broke ground" OR '
        '"groundbreaking ceremony" OR "under construction" OR '
        '"construction underway" OR "construction started" OR '
        '"construction begins" OR "tenant improvement" OR '
        '"interior build-out" OR "build out" OR "certificate of occupancy"'
    )

    base_keywords_close = (
        '"store closing" OR "closing soon" OR "closing" OR "closures" OR '
        '"shutting down" OR "shutters" OR "permanent closure" OR "permanent closing" OR '
        '"going out of business" OR "going-out-of-business" OR "liquidation" OR '
        '"everything must go" OR "store closing sale" OR "last day" OR "final day" OR '
        '"final closing" OR "ceases operations" OR "store to close" OR "stores to close" OR '
        '"closing all locations" OR "closing locations" OR "shutter stores"'
    )

    keywords         = base_keywords_close if is_closure else base_keywords_open
    industry_context = build_industry_context(industry)
    locations        = '(USA OR Canada OR "United States" OR America OR state OR city OR county)'
    recent_date      = (date.today() - timedelta(days=4)).strftime('%Y-%m-%d')

    return f'"{store}" {keywords} {industry_context} {locations} after:{recent_date}'


def fetch_news_for_store(store: str, industry: str | None = None, is_closure: bool = False) -> list[dict]:
    query         = build_query(store, industry=industry, is_closure=is_closure)
    encoded_query = quote_plus(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded_query}"
        f"&hl=en-US&gl=US&ceid=US:en&scoring=d"
    )

    # Google News occasionally drops the connection; retry, then skip this
    # banner rather than losing the whole run.
    feed = None
    for attempt in range(3):
        try:
            feed = feedparser.parse(rss_url)
            break
        except Exception as exc:
            print(f"   ⚠  {store}: fetch failed ({exc}); retry {attempt + 1}/3")
            time.sleep(5 * (attempt + 1))
    if feed is None:
        return []
    cutoff_date = date.today() - timedelta(days=2)

    results = []
    for entry in feed.entries:
        published_str = entry.get('published', 'Date not available')
        if not is_recent(published_str, cutoff_date):
            continue

        real_link  = decode_google_news_url(entry)
        raw_summary = entry.get('summary', 'No summary')
        results.append({
            'title':     entry.title,
            'link':      real_link,
            'published': published_str,
            'summary':   clean_summary(raw_summary),
            'type':      'Closing' if is_closure else 'Opening',
        })

    results.sort(key=lambda x: x['published'], reverse=True)
    return results


# ────────────────────────────────────────────────
# Main execution
# ────────────────────────────────────────────────
print(f"\nRun date: {date.today()}")
print("Fetching recent (last 2 days) store OPENING + CLOSING news in USA/Canada...\n")

analyst_results = defaultdict(list)
found_any = False

for store in stores:
    analyst  = analyst_map.get(store, "Unassigned")
    industry = industry_map.get(store)          # None → generic retail context

    results_open  = fetch_news_for_store(store, industry=industry, is_closure=False)
    results_close = fetch_news_for_store(store, industry=industry, is_closure=True)

    all_results = results_open + results_close

    if all_results:
        found_any = True
        for res in all_results:
            analyst_results[analyst].append({
                'Store':     store,
                'Analyst':   analyst,
                'Industry':  cohort_map.get(store, "Other"),
                'High_Priority': bool(priority_map.get(store, False)),
                'Type':      res['type'],
                'Title':     res['title'],
                'Link':      res['link'],
                'Published': res['published'],
                'Summary':   res['summary'],
            })

    time.sleep(1.3)

# ────────────────────────────────────────────────
# Output – grouped by analyst
# ────────────────────────────────────────────────
if not found_any:
    print("No recent store opening or closing news found in the last 2 days.")
else:
    print("=" * 85)
    print("     NEW BANNER OPENING & CLOSING NEWS – GROUPED BY ANALYST (last 2 days only)")
    print("=" * 85 + "\n")

    for analyst, items in sorted(analyst_results.items()):
        print(f"Analyst: {analyst}   ({len(items)} article(s))")
        print("-" * 70)

        for i, item in enumerate(items, 1):
            flag = "  ★ HIGH PRIORITY" if item['High_Priority'] else ""
            print(f"{i}. [{item['Type']}] {item['Store']}  |  Industry: {item['Industry']}{flag}")
            print(f"   {item['Title']}")
            print(f"   Published: {item['Published']}")
            print(f"   Link:      {item['Link']}")
            summary_short = (item['Summary'][:220] + "...") if len(item['Summary']) > 220 else item['Summary']
            print(f"   {summary_short}\n")

        print()

    all_rows = []
    for items in analyst_results.values():
        all_rows.extend(items)

    if all_rows:
        df_out    = pd.DataFrame(all_rows)
        today_str = date.today().strftime("%Y-%m-%d")
        filename  = STORE_NEWS_DIR / f"new_banner_news_{today_str}.csv"
        df_out.to_csv(filename, index=False, encoding='utf-8')
        print(f"\nResults saved to: {filename}")
        print(f"Total articles: {len(all_rows)}")
    
    # ────────────────────────────────────────────────
    # Master file — accumulates all daily results
    # ────────────────────────────────────────────────
    MASTER_BANNER_NEWS_FILE = Path("master_file")
    MASTER_BANNER_NEWS_FILE.mkdir(parents=True, exist_ok=True)


    MASTER_FILE = MASTER_BANNER_NEWS_FILE / "new_banner_news_master.csv"

    if all_rows:
        df_new = pd.DataFrame(all_rows)

        # Add ingestion date so each batch of rows can be traced back to its run
        df_new['Date_Appended'] = today_str

        # Normalize Published to UTC datetime NOW (before concat) so dedup and
        # sorting work on comparable values regardless of raw RSS string format.
        df_new['Published'] = pd.to_datetime(df_new['Published'], errors='coerce', utc=True)

        if MASTER_FILE.exists():
            df_master = pd.read_csv(MASTER_FILE, encoding='utf-8')
            # Re-parse Published in the master (stored as ISO string after previous runs)
            df_master['Published'] = pd.to_datetime(df_master['Published'], errors='coerce', utc=True)
            df_master = pd.concat([df_master, df_new], ignore_index=True)
        else:
            df_master = df_new

        # Deduplicate on Store + Title + Link — more robust than using Published
        # because the same article can have slightly different timestamp strings
        # across runs while Link is a stable identifier.
        df_master = df_master.drop_duplicates(subset=['Store', 'Title', 'Link'])

        # Sort newest first
        df_master = df_master.sort_values('Published', ascending=False)

        df_master.to_csv(MASTER_FILE, index=False, encoding='utf-8')
        print(f"✓ Master file updated: {MASTER_FILE}  ({len(df_master)} total rows)")

# ────────────────────────────────────────────────
# Always create new_banner_news.json (even if no news found)
# ────────────────────────────────────────────────
today_str = date.today().strftime("%Y-%m-%d")

json_data = {
    "last_updated": today_str,
    "data": {analyst: items for analyst, items in sorted(analyst_results.items())},
}

with open('new_banner_news.json', 'w', encoding='utf-8') as f:
    json.dump(json_data, f, ensure_ascii=False, indent=2)

archive_filename = JSON_ARCHIVE_DIR / f"new_banner_news_{today_str}.json"
with open(archive_filename, 'w', encoding='utf-8') as f:
    json.dump(json_data, f, ensure_ascii=False, indent=2)

print(f"\n✓ new_banner_news.json created/updated at root (last_updated: {today_str})")
print(f"✓ {archive_filename} created as historical snapshot")