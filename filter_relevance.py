"""
POST-FETCH RELEVANCE FILTER — operates on data ALREADY collected by
fetch_banner_store_news_chunked.py. No new network calls.

Two rounds of investigation on the real output (banner_news_master_chunked.csv,
1324 rows) found two DIFFERENT problems, discovered in this order:

ROUND 1 — generic-English-word store names ("At Home" 198 rows, "Target" 193,
"FIELDS" 81, "Staples" 28, "BP" 22) drowning in matches where the store name
is a common word or a stock ticker (stock/finance sites, sports content,
obituaries incidentally containing "at home"/"field"/"target").

ROUND 2 — the bigger finding: even completely unambiguous, famous names are
mostly missing their own name from the result. Checked against the ROUND-1-
filtered data: "Chick-fil-a" (74 rows) had the literal name absent from
title+summary in 73% of its own rows; "CVS" 92%; "Whole Foods Market" 91%
(after correcting for legitimate brand shortening, e.g. "Whole Foods Opens
in Eatontown" for "Whole Foods Market", or "DICK'S House of Sport" for
"Dick's Sporting Goods" — those are real matches, not noise). This means
Google News RSS is NOT enforcing the quoted store name as a hard requirement
for these multi-clause queries — it's a soft ranking signal, and Google
substitutes generic "trending retail news of the day" when a specific chunk
query has few true hits (confirmed by the SAME article — "American Eagle
Outfitters to open DC in North Carolina" — appearing under seven totally
unrelated store queries: Office Depot, Giant Eagle, Dollar General, Fred
Meyer, Jay C, City Market, Harris Teeter — none of which appear anywhere in
its title or summary).

Three filters are applied, in order:
  0. STORE NAME MUST BE PRESENT — mandatory for every row, not just
     ambiguous ones. Uses each store's distinctive short form (e.g. "Whole
     Foods" not "Whole Foods Market", "Dicks" not "Dick's Sporting Goods")
     so legitimate brand-shortening in headlines isn't penalized. This is
     the single most impactful filter found across both rounds.
  1. SOURCE_BLOCKLIST — drop rows whose trailing " - Source Name" matches a
     known stock/finance/generic-content publisher.
  2. For AMBIGUOUS_STORE_NAMES only: additionally require an industry-context
     keyword (the post-fetch equivalent of the industry clause removed from
     the search query itself) — catches cases where the short name alone
     still isn't enough (e.g. "target" as a verb).
"""

import re
import pandas as pd
from pathlib import Path

INPUT_FILE = Path("master_file_chunked/banner_news_master_chunked.csv")
OUTPUT_FILE = Path("master_file_chunked/banner_news_master_chunked_filtered.csv")

# ────────────────────────────────────────────────
# Filter 0: the store's own name must actually appear in title+summary.
# Mandatory for every row. Uses a short/distinctive form so that legitimate
# brand-shortening in headlines (very common) isn't penalized.
# ────────────────────────────────────────────────
NAME_ALIASES = {
    "dick's sporting goods": ["dicks"],
    "whole foods market": ["whole foods"],
    "bj's wholesale club": ["bjs"],
    "o'reilly auto parts": ["oreilly"],
    "sam's club": ["sams club"],
    "trader joe's": ["trader joes"],
    "costco wholesale corp.": ["costco"],
    "variety wholesalers": ["variety wholesalers", "big lots"],
    "macy's": ["macys"],
}
_STRIP_RE = re.compile(r"['’â�.]")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    s = str(s).lower()
    s = _STRIP_RE.sub("", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    return _WHITESPACE_RE.sub(" ", s).strip()


def _core_terms(store: str) -> list:
    norm = _normalize(store)
    if norm in NAME_ALIASES:
        return NAME_ALIASES[norm]
    words = norm.split()
    while words and words[0] in ("the", "at"):
        words = words[1:]
    if not words:
        return [norm]
    # short leading word ("BJ's", "TJ's"-style) -> keep two words together
    if len(words[0]) <= 3 and len(words) > 1:
        return [" ".join(words[:2])]
    return [words[0]]


def name_is_present(store: str, title: str, summary: str) -> bool:
    text = _normalize(f"{title} {summary}")
    return any(term in text for term in _core_terms(store))


# ────────────────────────────────────────────────
# Filter 1: known noise-source publishers (stock/finance/crypto/generic wire)
# ────────────────────────────────────────────────
SOURCE_BLOCKLIST = [
    "tradingview", "tradingkey", "investing.com", "marketbeat", "gurufocus",
    "tipranks", "stocktwits", "stock titan", "seeking alpha", "simplywall.st",
    "simply wall st", "zacks", "24/7 wall st", "motley fool", "barchart",
    "benzinga", "coindesk", "bitget", "cryptonews", "yellow.com",
    "techstock", "finance.biggo.com", "idnfinancials", "streamlinefeed",
    "tradingnews", "business wire", "globe and mail", "el-balad", "readers.id",
    "ssbcrack", "mshale.com", "el balad",
]

# Fortune/Yahoo Finance "Q_ 20__ Earnings Call Transcript" pattern is common
# enough (and specific enough) to catch as a title pattern regardless of source
TITLE_BLOCKLIST_PATTERNS = [
    r"\bq[1-4]\s?20\d{2}\s+earnings\s+call\s+transcript\b",
    r"\b12.?month\s+price\s+target\b",
    r"\btechnical\s+analysis\b.*\bsupport,?\s+resistance\b",
    r"\bimplies\s+\d+%\s+(upside|downside)\b",
    r"\bstock\s+quote\s+price\s+and\s+forecast\b",
    r"\bfound\s+dead\b",
    r"\barrested\b|\bcharged\b (after|with)|\bsentenced\b",
    r"\bpremier\s+league\b|\bfixtures?\b.*\b20\d{2}\b|\btraining\s+camp\b",
    r"\bgameweek\b|\bplayoff\b.*\bpreview\b",
]
TITLE_BLOCKLIST_RE = re.compile("|".join(TITLE_BLOCKLIST_PATTERNS), re.IGNORECASE)

# ────────────────────────────────────────────────
# Filter 2: ambiguous dictionary-word store names — require a retail-context
# signal in title+summary, or drop.
#
# IMPORTANT: this must NOT include bare "opening"/"closing"/"store" as generic
# words — every row already matched one of those because that's literally how
# the search query found it (it's circular and filters nothing). A first
# version of this filter made that mistake and let through "LeBron James...
# closing in on agreement", "Closing the Gap" (an Australian government
# program), "opening weekend clashes" (sports) for the "At Home" store. The
# real fix is to require the STORE'S OWN INDUSTRY keywords (the same list
# INDUSTRIES/INDUSTRY_QUERY_STRINGS uses in the production script) rather than
# a generic word list, plus a construction/permit signal for pre-opening news
# that may not use the industry noun yet.
# ────────────────────────────────────────────────
AMBIGUOUS_STORE_INDUSTRY_KEYWORDS = {
    "At Home": ["housewares store", "home goods store", "home furnishings store",
                "kitchenware store", "furniture store", "furnishings"],
    "Target": ["mass merchandiser", "big box store", "discount store", "supercenter",
               "retailer", "retail chain"],
    "Staples": ["mass merchandiser", "big box store", "discount store", "supercenter",
                "office supply", "retailer"],
    "FIELDS": ["department store", "department chain", "retailer"],
    "BP": ["gas station", "convenience store", "c-store", "fuel station", "fuel pump"],
    "Chevron": ["gas station", "convenience store", "c-store", "fuel station", "fuel pump"],
    "Shell Oil": ["gas station", "convenience store", "c-store", "fuel station", "fuel pump"],
    "ConocoPhillips": ["gas station", "convenience store", "c-store", "fuel station"],
    "ARCO": ["gas station", "convenience store", "c-store", "fuel station"],
    "Circle K": ["gas station", "convenience store", "c-store", "fuel station"],
}

CONSTRUCTION_SIGNAL_RE = re.compile(
    r"\b(construction|underway|breaks?\s+ground|broke\s+ground|groundbreaking|"
    r"permit|zoning|lease\s+signed|signed\s+lease|development|build.?out|"
    r"site\s+plan|new\s+location|new\s+store|storefront)\b",
    re.IGNORECASE,
)

AMBIGUOUS_STORE_NAMES = set(AMBIGUOUS_STORE_INDUSTRY_KEYWORDS)


def has_industry_context(store: str, combined_text: str) -> bool:
    keywords = AMBIGUOUS_STORE_INDUSTRY_KEYWORDS.get(store, [])
    text_lower = combined_text.lower()
    if any(kw in text_lower for kw in keywords):
        return True
    return bool(CONSTRUCTION_SIGNAL_RE.search(combined_text))


def source_from_title(title: str) -> str:
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip().lower()
    return ""


def is_blocked_source(title: str) -> bool:
    src = source_from_title(title)
    return any(bad in src for bad in SOURCE_BLOCKLIST)


def passes_filters(row) -> bool:
    title = str(row["Title"])
    summary = str(row["Summary"])
    store = row["Store"]

    if not name_is_present(store, title, summary):
        return False

    if is_blocked_source(title):
        return False
    if TITLE_BLOCKLIST_RE.search(title):
        return False

    if store in AMBIGUOUS_STORE_NAMES:
        combined = f"{title} {summary}"
        if not has_industry_context(store, combined):
            return False

    return True


if __name__ == "__main__":
    df = pd.read_csv(INPUT_FILE, encoding="utf-8")
    print(f"Loaded {len(df)} rows from {INPUT_FILE}")

    before_counts = df["Store"].value_counts()

    mask = df.apply(passes_filters, axis=1)
    kept = df[mask]
    dropped = df[~mask]

    print(f"\nKept:    {len(kept)} rows")
    print(f"Dropped: {len(dropped)} rows ({len(dropped)/len(df)*100:.1f}%)")

    print("\n--- Before vs after, by store (top 15 by original count) ---")
    after_counts = kept["Store"].value_counts()
    for store, before_n in before_counts.head(15).items():
        after_n = after_counts.get(store, 0)
        print(f"  {store:30s} {before_n:4d} -> {after_n:4d}")

    print("\n--- Sample of DROPPED rows (first 15) ---")
    for _, r in dropped.head(15).iterrows():
        print(f"  [{r['Store']}] {r['Title']}")

    kept.to_csv(OUTPUT_FILE, index=False, encoding="utf-8")
    print(f"\nFiltered CSV saved to: {OUTPUT_FILE}")
