"""
EXPERIMENT #2 — fix for the real root cause found via experiment_location_filter.py.

CONFIRMED (see console session): Google News RSS search silently truncates
boolean queries after ~30 words. Proof: appending a nonsense AND-term
("zzznonexistentcanaryterm999") to the END of the production query returns the
SAME result count as without it — i.e. Google isn't even reading that far into
the query. Binary-searching the cutoff showed the flip from "canary respected"
(0 results) to "canary ignored" (nonzero results) happens right around 9-10
quoted phrases / ~30 words into the query.

Consequence for fetch_banner_store_news.py: build_query() concatenates ~40
action-keyword phrases + an industry-keyword group + a locations group into
ONE query. Only the first ~9 action-keyword phrases are ever actually searched
for — the entire industry clause and locations clause come after the cutoff
and are dead weight for almost every store. This — not geography/proxies — is
why local/niche coverage is thin: the industry-specific terms that would
narrow+diversify the match (e.g. "grocery store", "supermarket") never fire.

This script does NOT modify fetch_banner_store_news.py. It only proves out a
fix idea: split the keyword list into multiple smaller queries (each safely
under the word limit, each still including the industry clause), fetch all of
them, and de-duplicate by article link. Then it prints old-vs-new counts so
you can see the real gain before deciding whether to port this into the
production script.
"""

import time
import re
from datetime import date, timedelta
from urllib.parse import quote_plus

import feedparser
from dateutil import parser as date_parser
from html import unescape

# ── same minimal helpers as experiment_location_filter.py ──────────────────
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
            'link': entry.get('link', ''),  # raw link is fine for this count-only test
            'published': published_str,
            'summary': clean_summary(entry.get('summary', 'No summary')),
        })
    return results


# ── keyword data (same phrases as production, but kept as a LIST so we can chunk) ──
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

INDUSTRY_KEYWORDS = {
    "Restaurants": ["restaurant", "fast food chain", "quick service restaurant",
                     "fast casual", "diner", "eatery chain", "food chain"],
    "Grocery": ["grocery store", "supermarket", "food store", "grocery chain", "supermarket chain"],
    "Mass Merchandisers": ["mass merchandiser", "big box store", "discount store", "supercenter"],
    "Health / Beauty": ["beauty store", "beauty retailer", "salon", "spa", "health store",
                          "wellness store", "cosmetics store"],
}


def chunk(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def build_chunked_queries(store, industry, is_closure, chunk_size=6):
    """Split the phrase list into chunks small enough that, combined with the
    store name + industry clause, each query stays comfortably under ~28
    words — safely inside Google's ~30-word cutoff."""
    phrases = CLOSE_PHRASES if is_closure else OPEN_PHRASES
    industry_kw = INDUSTRY_KEYWORDS.get(industry)
    industry_clause = f'({" OR ".join(chr(34)+k+chr(34) for k in industry_kw)})' if industry_kw else ''
    recent_date = (date.today() - timedelta(days=4)).strftime('%Y-%m-%d')

    queries = []
    for group in chunk(phrases, chunk_size):
        kw_clause = " OR ".join(f'"{p}"' for p in group)
        q = f'"{store}" ({kw_clause}) {industry_clause} after:{recent_date}'.strip()
        queries.append(q)
    return queries


def build_original_query(store, industry, is_closure):
    """Reproduces the CURRENT production query shape (one mega OR-list) for comparison."""
    phrases = CLOSE_PHRASES if is_closure else OPEN_PHRASES
    kw_clause = " OR ".join(f'"{p}"' for p in phrases)
    industry_kw = INDUSTRY_KEYWORDS.get(industry)
    industry_clause = f'({" OR ".join(chr(34)+k+chr(34) for k in industry_kw)})' if industry_kw else ''
    locations = '(USA OR Canada OR "United States" OR America OR state OR city OR county)'
    recent_date = (date.today() - timedelta(days=4)).strftime('%Y-%m-%d')
    return f'"{store}" {kw_clause} {industry_clause} {locations} after:{recent_date}'


def run_test(store, industry):
    print("=" * 90)
    print(f"STORE: {store}   INDUSTRY: {industry}")
    print("=" * 90)

    for is_closure in (False, True):
        label = "CLOSING" if is_closure else "OPENING"

        # --- OLD behavior: one mega query ---
        old_query = build_original_query(store, industry, is_closure)
        old_results = fetch(old_query)
        print(f"\n[{label}] OLD single mega-query ({len(old_query.split())} words): {len(old_results)} article(s)")
        time.sleep(1.2)

        # --- NEW behavior: chunked queries, deduped ---
        chunked_queries = build_chunked_queries(store, industry, is_closure)
        seen_links = set()
        new_results = []
        for q in chunked_queries:
            r = fetch(q)
            for a in r:
                if a['link'] not in seen_links:
                    seen_links.add(a['link'])
                    new_results.append(a)
            time.sleep(1.2)

        print(f"[{label}] NEW chunked queries ({len(chunked_queries)} sub-queries, "
              f"~{len(chunked_queries[0].split())} words each): {len(new_results)} unique article(s)")

        old_links = {a['link'] for a in old_results}
        gained = [a for a in new_results if a['link'] not in old_links]
        if gained:
            print(f"[{label}] Articles gained by chunking that OLD query completely missed: {len(gained)}")
            for a in gained[:10]:
                print(f"    - {a['title']}")


if __name__ == "__main__":
    targets = [
        ("Chick-fil-a", "Restaurants"),
        ("Trader Joe's", "Grocery"),
        ("Whole Foods Market", "Grocery"),
        ("Burlington Stores", "Mass Merchandisers"),
    ]
    print(f"Run date: {date.today()}\n")
    for store, industry in targets:
        run_test(store, industry)
