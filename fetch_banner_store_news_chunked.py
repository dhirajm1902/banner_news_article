"""
VALIDATED-FIX VARIANT of fetch_banner_store_news.py — does NOT touch the
production script, its data/store_news/, master_file/, or latest_news.json.
All outputs from this file go to separate *_chunked paths so it is 100% safe
to run alongside the real pipeline.

Root cause (proven in experiment_location_filter.py / experiment_chunked_query_fix.py):
Google News RSS search silently truncates boolean queries after ~28-30 words.
The production build_query() concatenates ~40 action-keyword phrases + an
industry-keyword clause + a locations clause into ONE query (95-176 words).
Only the first ~9 action-keyword phrases are ever actually evaluated by
Google — the industry clause and locations clause never execute for almost
any store, and most of the crafted keyword variety is dead weight.

Fix applied here:
  1. Split the action-keyword phrase list into small chunks, each combined
     with the store name and issued as its OWN query, comfortably under the
     ~28-word cutoff so every phrase actually gets searched.
  2. Drop the industry-keyword clause and the locations clause from the
     search query entirely. Testing showed that once the industry clause
     actually executes (which it couldn't before, due to truncation) it
     acts as a hard AND-filter that excludes real coverage — most local
     articles about "Trader Joe's" or "Whole Foods" never restate the
     generic word "grocery"/"supermarket". Industry is still recorded in
     the output CSV/JSON (from analyst.csv) for analyst grouping — it's
     just no longer used to narrow the search itself.
  3. Results from all chunks (per store, per event type) are merged and
     de-duplicated by the decoded article link.

Trade-off: this issues ~8 RSS requests per store instead of 2, so a full run
across analyst.csv takes noticeably longer. That's an acceptable cost for a
daily batch job but is why this stays a separate experiment file until the
real gain is confirmed at full scale.
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
import sys
import socket
import urllib.error

# Line-buffer stdout so progress prints show up immediately in a redirected
# log file instead of only appearing when the process exits.
sys.stdout.reconfigure(line_buffering=True)

# ────────────────────────────────────────────────
# Output directory setup — kept fully separate from the production paths
# ────────────────────────────────────────────────
STORE_NEWS_DIR = Path("data/store_news_chunked")
JSON_ARCHIVE_DIR = Path("data/store_news_chunked/json_archive")
MASTER_DIR = Path("master_file_chunked")
STORE_NEWS_DIR.mkdir(parents=True, exist_ok=True)
JSON_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
MASTER_DIR.mkdir(parents=True, exist_ok=True)

MASTER_FILE = MASTER_DIR / "banner_news_master_chunked.csv"
LATEST_JSON = Path("latest_news_chunked.json")

# ────────────────────────────────────────────────
# Industries — kept ONLY for labeling output rows now (not used in queries)
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

# ────────────────────────────────────────────────
# Google News URL Decoder (unchanged from production)
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
# Default fallback (unchanged from production)
# ────────────────────────────────────────────────
default_stores = [
    "Doggie Style", "Dogtopia", "Earthwise Pet", "Feeders Supply",
    "Friendly Pets", "Hollywood Feed", "Kahoots Pet Products", "Kriser's",
    "Mud Bay", "Pet Club Food and Supplies", "Pet Depot", "Pet Evolution"
]

# ────────────────────────────────────────────────
# Load stores + analyst + industry mapping from analyst.csv (unchanged)
# ────────────────────────────────────────────────
stores = []
analyst_map = {}
industry_map = {}

csv_file = Path('analyst.csv')

if csv_file.exists():
    try:
        df = pd.read_csv(csv_file)
        df['Store']   = df['Store'].astype(str).str.strip()
        df['Analyst'] = df['Analyst'].astype(str).str.strip()
        df = df.dropna(subset=['Store'])
        df = df[df['Store'].str.len() > 0]

        stores = df['Store'].tolist()
        analyst_map = dict(zip(df['Store'], df['Analyst']))

        if 'Industry' in df.columns:
            df['Industry'] = df['Industry'].astype(str).str.strip()
            industry_map = dict(zip(df['Store'], df['Industry']))
            valid = sum(1 for v in industry_map.values() if v in INDUSTRIES)
            print(f"-> Loaded {len(stores)} stores + {len(analyst_map)} analyst mappings + "
                  f"{valid} valid industry mappings from analyst.csv")
        else:
            industry_map = {s: None for s in stores}
            print(f"-> Loaded {len(stores)} stores from analyst.csv (no Industry column)")

        unique_analysts = len(set(analyst_map.values()))
        print(f"   -> {unique_analysts} unique analysts detected")

    except Exception as e:
        print(f"Error reading analyst.csv: {e}")
        print("-> Falling back to default pet stores list")
        stores = default_stores
        analyst_map  = {s: "Unassigned" for s in default_stores}
        industry_map = {s: "Pet Care"   for s in default_stores}
else:
    print("-> analyst.csv not found — using default pet stores fallback")
    stores = default_stores
    analyst_map  = {s: "Unassigned" for s in default_stores}
    industry_map = {s: "Pet Care"   for s in default_stores}


# ────────────────────────────────────────────────
# Helper functions (unchanged from production)
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


# ────────────────────────────────────────────────
# Action-keyword phrases — kept as LISTS (not one giant OR string) so they
# can be split into Google-safe chunks. Same phrases as production.
# ────────────────────────────────────────────────
OPEN_PHRASES = [
    "new store", "new location", "opening soon", "coming soon", "grand opening",
    "now open", "opens new", "opening in", "to open", "set to open",
    "plans to open", "breaks ground", "now hiring", "store opening", "location opening",
    "will open", "opening date", "opening weekend", "soft opening", "ribbon cutting",
    "open for business", "doors open", "expanding to", "announced plans", "plans announced",
    "permit filed", "building permit", "permit application", "zoning approval", "site plan approved",
    "lease signed", "signed lease", "retail space leased", "land acquired", "site acquired",
    "broke ground", "groundbreaking ceremony", "under construction", "construction underway",
    "construction started", "construction begins", "tenant improvement", "interior build-out",
    "build out", "certificate of occupancy",
]

CLOSE_PHRASES = [
    "store closing", "closing soon", "closing", "closures", "shutting down", "shutters",
    "permanent closure", "permanent closing", "going out of business", "going-out-of-business",
    "liquidation", "everything must go", "store closing sale", "last day", "final day",
    "final closing", "ceases operations", "store to close", "stores to close",
    "closing all locations", "closing locations", "shutter stores",
]

# Chunk size validated via canary-term testing: a forced AND-term was still
# respected at ~9 phrases / 29 tokens for a short store name, but longer
# multi-word store names (e.g. "Kahoots Pet Products") push token count past
# the ~29-30 cutoff at that chunk size. CHUNK_SIZE=7 keeps every chunk's
# query at or below 27 tokens even for the longest store names in
# analyst.csv, leaving safe margin below the cutoff for every store.
CHUNK_SIZE = 7


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_chunked_queries(store: str, is_closure: bool = False) -> list[str]:
    """Validated fix: small per-chunk queries (store + one keyword group +
    date only) instead of one giant query. No industry/location clause —
    testing showed those either never executed (truncated away) or, once
    they did execute, incorrectly excluded real coverage."""
    phrases = CLOSE_PHRASES if is_closure else OPEN_PHRASES
    recent_date = (date.today() - timedelta(days=4)).strftime('%Y-%m-%d')

    queries = []
    for group in _chunk(phrases, CHUNK_SIZE):
        kw_clause = " OR ".join(f'"{p}"' for p in group)
        queries.append(f'"{store}" ({kw_clause}) after:{recent_date}')
    return queries


def fetch_one_query(query: str, is_closure: bool, max_retries: int = 2) -> list[dict]:
    encoded_query = quote_plus(query)
    rss_url = (
        f"https://news.google.com/rss/search?q={encoded_query}"
        f"&hl=en-US&gl=US&ceid=US:en&scoring=d"
    )

    feed = None
    for attempt in range(max_retries + 1):
        try:
            feed = feedparser.parse(rss_url)
            # feedparser swallows network errors into feed.bozo_exception
            # rather than raising, so check it explicitly.
            if getattr(feed, 'bozo', False) and isinstance(
                getattr(feed, 'bozo_exception', None),
                (OSError, socket.error, urllib.error.URLError),
            ):
                raise feed.bozo_exception
            break
        except (OSError, socket.error, urllib.error.URLError) as e:
            if attempt < max_retries:
                wait = 3 * (attempt + 1)
                print(f"    [warn] network error on chunk query, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"    [warn] giving up on this chunk after {max_retries} retries: {e}")
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
    return results


def fetch_news_for_store(store: str, is_closure: bool = False, sleep_s: float = 1.2) -> list[dict]:
    """Runs ALL chunked queries for this store/event-type and de-duplicates
    by decoded article link. This is the direct replacement for production's
    single-query fetch_news_for_store()."""
    seen_links = set()
    merged = []

    for query in build_chunked_queries(store, is_closure=is_closure):
        for res in fetch_one_query(query, is_closure):
            if res['link'] not in seen_links:
                seen_links.add(res['link'])
                merged.append(res)
        time.sleep(sleep_s)

    merged.sort(key=lambda x: x['published'], reverse=True)
    return merged


# ────────────────────────────────────────────────
# Output writer — pulled into a function so it can be called as a
# checkpoint every N stores, not just once at the very end. A run this
# long (~2,000+ RSS calls) WILL occasionally hit a transient network error;
# without checkpointing, a crash near the end throws away every article
# collected up to that point.
# ────────────────────────────────────────────────
def _write_with_retry(write_fn, description: str, max_retries: int = 4):
    """Windows file writes can hit a transient PermissionError if antivirus,
    OneDrive sync, or the search indexer briefly has the file open right
    after a previous write. Retry with backoff instead of crashing the run."""
    for attempt in range(max_retries + 1):
        try:
            write_fn()
            return
        except PermissionError as e:
            if attempt < max_retries:
                wait = 2 * (attempt + 1)
                print(f"    [warn] {description} locked, retrying in {wait}s: {e}")
                time.sleep(wait)
            else:
                raise


def save_outputs(analyst_results: dict, checkpoint: bool = False) -> int:
    today_str = date.today().strftime("%Y-%m-%d")
    all_rows = []
    for items in analyst_results.values():
        all_rows.extend(items)

    if not all_rows:
        return 0

    df_out   = pd.DataFrame(all_rows)
    filename = STORE_NEWS_DIR / f"banner_news_chunked_{today_str}.csv"
    _write_with_retry(
        lambda: df_out.to_csv(filename, index=False, encoding='utf-8'),
        f"daily CSV ({filename.name})",
    )

    df_new = pd.DataFrame(all_rows)
    df_new['Date_Appended'] = today_str
    df_new['Published'] = pd.to_datetime(df_new['Published'], errors='coerce', utc=True)

    # Always merge against the pre-run snapshot (MASTER_BASELINE_DF), never
    # against whatever this run itself last wrote to MASTER_FILE — df_new is
    # always this run's FULL accumulated results so far (not a delta), so
    # baseline + df_new + dedup is correct and idempotent on every call,
    # whether it's a mid-run checkpoint or the final save.
    if MASTER_FILE_BASELINE_LOADED:
        df_master = pd.concat([MASTER_BASELINE_DF, df_new], ignore_index=True)
    else:
        df_master = df_new

    df_master = df_master.drop_duplicates(subset=['Store', 'Title', 'Link'])
    df_master = df_master.sort_values('Published', ascending=False)
    _write_with_retry(
        lambda: df_master.to_csv(MASTER_FILE, index=False, encoding='utf-8'),
        "master CSV",
    )

    json_data = {
        "last_updated": today_str,
        "data": {analyst: items for analyst, items in sorted(analyst_results.items())},
    }

    def _write_latest_json():
        with open(LATEST_JSON, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

    _write_with_retry(_write_latest_json, "latest_news_chunked.json")

    archive_filename = JSON_ARCHIVE_DIR / f"latest_news_chunked_{today_str}.json"

    def _write_archive_json():
        with open(archive_filename, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, ensure_ascii=False, indent=2)

    _write_with_retry(_write_archive_json, f"archive JSON ({archive_filename.name})")

    tag = "[checkpoint]" if checkpoint else "[FINAL]"
    print(f"  {tag} saved {len(all_rows)} article(s) -> {filename.name}; "
          f"master now has {len(df_master)} row(s)")
    return len(all_rows)


# Snapshot whatever was already in the master file BEFORE this run started,
# so repeated checkpoint saves (which rebuild all_rows from scratch each
# time) merge against that pre-existing baseline exactly once instead of
# re-reading a file this same run keeps overwriting.
MASTER_FILE_BASELINE_LOADED = MASTER_FILE.exists()
if MASTER_FILE_BASELINE_LOADED:
    MASTER_BASELINE_DF = pd.read_csv(MASTER_FILE, encoding='utf-8')
    MASTER_BASELINE_DF['Published'] = pd.to_datetime(MASTER_BASELINE_DF['Published'], errors='coerce', utc=True)
else:
    MASTER_BASELINE_DF = None


# ────────────────────────────────────────────────
# Main execution
# ────────────────────────────────────────────────
print(f"\nRun date: {date.today()}")
print("[CHUNKED VARIANT] Fetching recent (last 2 days) store OPENING + CLOSING "
      f"news for ALL {len(stores)} companies in analyst.csv...")
print(f"({len(_chunk(OPEN_PHRASES, CHUNK_SIZE))} opening sub-queries + "
      f"{len(_chunk(CLOSE_PHRASES, CHUNK_SIZE))} closing sub-queries per store, "
      f"~{len(_chunk(OPEN_PHRASES, CHUNK_SIZE)) + len(_chunk(CLOSE_PHRASES, CHUNK_SIZE))} RSS calls/store)\n")

analyst_results = defaultdict(list)
found_any = False
failed_stores = []
CHECKPOINT_EVERY = 20

for idx, store in enumerate(stores, 1):
    try:
        analyst  = analyst_map.get(store, "Unassigned")
        industry = industry_map.get(store)  # kept for labeling only — not used in the query anymore

        results_open  = fetch_news_for_store(store, is_closure=False)
        results_close = fetch_news_for_store(store, is_closure=True)

        all_results = results_open + results_close

        if all_results:
            found_any = True
            for res in all_results:
                analyst_results[analyst].append({
                    'Store':     store,
                    'Analyst':   analyst,
                    'Industry':  industry or "Unknown",
                    'Type':      res['type'],
                    'Title':     res['title'],
                    'Link':      res['link'],
                    'Published': res['published'],
                    'Summary':   res['summary'],
                })
    except Exception as e:
        # A single store's failure (e.g. an unrecoverable network error
        # after retries) should never take down a multi-hour run.
        failed_stores.append(store)
        print(f"  [error] skipping '{store}' after unrecoverable error: {e}")

    if idx % 10 == 0 or idx == len(stores):
        running_total = sum(len(v) for v in analyst_results.values())
        print(f"  ... {idx}/{len(stores)} stores processed, {running_total} article(s) collected so far")

    if idx % CHECKPOINT_EVERY == 0 or idx == len(stores):
        try:
            save_outputs(analyst_results, checkpoint=True)
        except Exception as e:
            # analyst_results stays intact in memory either way — a failed
            # checkpoint just means we retry at the NEXT checkpoint instead
            # of losing the run.
            print(f"  [error] checkpoint save failed, will retry at next checkpoint: {e}")

# ────────────────────────────────────────────────
# Final summary
# ────────────────────────────────────────────────
if not found_any:
    print("No recent store opening or closing news found in the last 2 days.")
else:
    print("\n" + "=" * 85)
    print("     [CHUNKED] STORE OPENING & CLOSING NEWS – GROUPED BY ANALYST (last 2 days only)")
    print("=" * 85 + "\n")

    for analyst, items in sorted(analyst_results.items()):
        print(f"Analyst: {analyst}   ({len(items)} article(s))")

    try:
        save_outputs(analyst_results, checkpoint=False)
    except Exception as e:
        # The idx == len(stores) checkpoint just above already saved this
        # exact data, so a failure here is non-fatal — don't let it flip
        # the run's exit code after a fully successful fetch.
        print(f"  [error] final save failed (last checkpoint already has this data): {e}")

if failed_stores:
    print(f"\n[WARNING] {len(failed_stores)} store(s) were skipped due to unrecoverable errors: {failed_stores}")

print("\n[CHUNKED] Run complete.")
