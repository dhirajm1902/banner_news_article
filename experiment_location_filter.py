"""
EXPERIMENT — does the `locations` keyword clause in fetch_banner_store_news.py
hide local US/Canada store news?

This is a standalone, read-only test. It does NOT touch fetch_banner_store_news.py,
analyst.csv writes, master CSVs, or Supabase. It only prints/saves a comparison
so you can decide whether to change the real pipeline.

Hypothesis being tested:
    build_query() in the main script ANDs in this clause:
        (USA OR Canada OR "United States" OR America OR state OR city OR county)
    Google's query syntax ANDs space-separated groups together, so every result
    must ALSO contain one of those literal words. Small local news stories
    ("Springfield, Ohio grocer opens...") often never use the words
    "state"/"city"/"county"/"USA"/"America" — they just name the town — so this
    clause silently drops exactly the local coverage you're trying to find.
    Meanwhile `gl=US&ceid=US:en&hl=en-US` already scopes results to the US
    edition, so the clause isn't buying you geo-targeting either.

What this script does for each test store:
    1. Runs the CURRENT query (with the locations clause) — baseline.
    2. Runs a MODIFIED query (locations clause removed).
    3. Diffs the two result sets and geo-tags the new articles using a
       lightweight US-state / Canadian-province / country-name heuristic,
       so you can eyeball whether the "new" articles are genuinely
       US/Canada-relevant or just noise.

Usage:
    python experiment_location_filter.py                      # runs a few sample stores
    python experiment_location_filter.py "Dollar General" "Ulta Beauty"
"""

import sys
import time
import re
from datetime import date, timedelta
from urllib.parse import quote_plus

import feedparser
import pandas as pd
from dateutil import parser as date_parser
from html import unescape
import base64

# ────────────────────────────────────────────────
# Minimal copies of the helpers/data needed from fetch_banner_store_news.py
# (duplicated on purpose — this file must not import the main script, since
# that script runs its full fetch pipeline at import time)
# ────────────────────────────────────────────────

INDUSTRIES = {
    "Apparel": ["clothing store", "fashion retailer", "apparel store", "apparel chain",
                "clothing chain", "fashion chain", "garment store"],
    "Mass Merchandisers": ["mass merchandiser", "big box store", "discount store", "supercenter"],
    "Grocery": ["grocery store", "supermarket", "food store", "grocery chain", "supermarket chain"],
    "Health / Beauty": ["beauty store", "beauty retailer", "salon", "spa", "health store",
                         "wellness store", "cosmetics store"],
    "Restaurants": ["restaurant", "fast food chain", "quick service restaurant",
                     "fast casual", "diner", "eatery chain", "food chain"],
    # Add more industries here if you want to test stores outside these categories.
}

INDUSTRY_QUERY_STRINGS = {
    industry: " OR ".join(f'"{kw}"' for kw in keywords)
    for industry, keywords in INDUSTRIES.items()
}

BASE_KEYWORDS_OPEN = (
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

BASE_KEYWORDS_CLOSE = (
    '"store closing" OR "closing soon" OR "closing" OR "closures" OR '
    '"shutting down" OR "shutters" OR "permanent closure" OR "permanent closing" OR '
    '"going out of business" OR "going-out-of-business" OR "liquidation" OR '
    '"everything must go" OR "store closing sale" OR "last day" OR "final day" OR '
    '"final closing" OR "ceases operations" OR "store to close" OR "stores to close" OR '
    '"closing all locations" OR "closing locations" OR "shutter stores"'
)

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
        return date_parser.parse(published_str).date() >= cutoff_date
    except Exception:
        return False


def _decode_gnews_blob(encoded: str):
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
    raw_link = entry.get('link', '')
    source = entry.get('source', {})
    source_href = source.get('href', '') if isinstance(source, dict) else ''
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


def build_industry_context(industry) -> str:
    if industry and industry in INDUSTRY_QUERY_STRINGS:
        return f"({INDUSTRY_QUERY_STRINGS[industry]})"
    return '(store OR location OR retail OR shop OR outlet OR station OR pharmacy OR supermarket OR grocery OR "auto parts")'


def build_query(store: str, industry=None, is_closure: bool = False, include_locations: bool = True) -> str:
    """Same as the production build_query(), but `include_locations` lets us
    toggle the disputed clause on/off for comparison."""
    keywords = BASE_KEYWORDS_CLOSE if is_closure else BASE_KEYWORDS_OPEN
    industry_context = build_industry_context(industry)
    recent_date = (date.today() - timedelta(days=4)).strftime('%Y-%m-%d')

    parts = [f'"{store}"', keywords, industry_context]
    if include_locations:
        parts.append('(USA OR Canada OR "United States" OR America OR state OR city OR county)')
    parts.append(f'after:{recent_date}')
    return " ".join(parts)


def fetch(query: str) -> list[dict]:
    encoded_query = quote_plus(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded_query}"
        f"&hl=en-US&gl=US&ceid=US:en&scoring=d"
    )
    feed = feedparser.parse(rss_url)
    cutoff_date = date.today() - timedelta(days=2)

    results = []
    for entry in feed.entries:
        published_str = entry.get('published', 'Date not available')
        if not is_recent(published_str, cutoff_date):
            continue
        results.append({
            'title': entry.title,
            'link': decode_google_news_url(entry),
            'published': published_str,
            'summary': clean_summary(entry.get('summary', 'No summary')),
        })
    return results


# ────────────────────────────────────────────────
# Lightweight US/Canada geo heuristic — used only to eyeball the diff,
# not as a production filter.
# ────────────────────────────────────────────────
US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan",
    "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont",
    "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
CA_PROVINCES = [
    "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba", "Saskatchewan",
    "Nova Scotia", "New Brunswick", "Newfoundland", "Prince Edward Island",
]
FOREIGN_HINTS = [
    "UK", "United Kingdom", "London", "Australia", "Sydney", "Melbourne",
    "India", "Mumbai", "Delhi", "Ireland", "Dublin", "New Zealand",
]
GEO_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in US_STATES + CA_PROVINCES) + r")\b", re.IGNORECASE
)
FOREIGN_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in FOREIGN_HINTS) + r")\b", re.IGNORECASE
)


def geo_tag(article: dict) -> str:
    text = f"{article['title']} {article['summary']}"
    if FOREIGN_RE.search(text):
        return "LIKELY-FOREIGN"
    if GEO_RE.search(text):
        return "US/CA-STATE-MATCH"
    return "NO-STATE-MENTIONED"  # not necessarily bad — many hyper-local stories skip the state name


# ────────────────────────────────────────────────
# Main experiment
# ────────────────────────────────────────────────
DEFAULT_TEST_STORES = [
    ("Dollar General", "Mass Merchandisers"),
    ("Ulta Beauty", "Health / Beauty"),
    ("Aldi", "Grocery"),
]


def run_comparison(store: str, industry: str):
    print("=" * 90)
    print(f"STORE: {store}   INDUSTRY: {industry}")
    print("=" * 90)

    rows = []
    for is_closure in (False, True):
        label = "CLOSING" if is_closure else "OPENING"

        query_with = build_query(store, industry, is_closure, include_locations=True)
        query_without = build_query(store, industry, is_closure, include_locations=False)

        results_with = fetch(query_with)
        time.sleep(1.2)
        results_without = fetch(query_without)
        time.sleep(1.2)

        links_with = {a['link'] for a in results_with}
        new_articles = [a for a in results_without if a['link'] not in links_with]

        print(f"\n[{label}] WITH locations clause:    {len(results_with)} article(s)")
        print(f"[{label}] WITHOUT locations clause: {len(results_without)} article(s)")
        print(f"[{label}] NEW articles unlocked by removing clause: {len(new_articles)}")

        for a in new_articles:
            tag = geo_tag(a)
            print(f"    [{tag}] {a['title']}")
            rows.append({
                'Store': store, 'Type': label, 'GeoTag': tag,
                'Title': a['title'], 'Link': a['link'], 'Published': a['published'],
            })

    return rows


if __name__ == "__main__":
    test_targets = []
    if len(sys.argv) > 1:
        # Store names passed on the command line; industry left blank
        # (falls back to the generic retail keyword context).
        test_targets = [(name, None) for name in sys.argv[1:]]
    else:
        test_targets = DEFAULT_TEST_STORES

    print(f"Run date: {date.today()}")
    print(f"Testing {len(test_targets)} store(s): {[t[0] for t in test_targets]}\n")

    all_new_rows = []
    for store, industry in test_targets:
        all_new_rows.extend(run_comparison(store, industry))

    if all_new_rows:
        out_path = f"experiment_location_filter_results_{date.today().strftime('%Y-%m-%d')}.csv"
        pd.DataFrame(all_new_rows).to_csv(out_path, index=False, encoding='utf-8')
        print(f"\n✓ Saved {len(all_new_rows)} newly-unlocked article(s) to: {out_path}")
        foreign_count = sum(1 for r in all_new_rows if r['GeoTag'] == 'LIKELY-FOREIGN')
        print(f"  Of those, {foreign_count} were flagged as likely non-US/Canada noise.")
    else:
        print("\nNo extra articles were unlocked by removing the locations clause for these stores.")
