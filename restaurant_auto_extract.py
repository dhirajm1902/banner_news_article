#!/usr/bin/env python3
"""
Restaurant Auto Extractor
Reads restaurant_latest.json → fetches each article → calls Claude API
with your extraction prompt → saves to restaurant_extraction_latest.json.

One command does everything:
    python restaurant_auto_extract.py

Options:
    --batch-size 20        Articles per Claude API call  (default: 20)
    --max-articles 40      Only process first N articles (useful for testing)
    --reset                Clear existing JSON before starting

Requirements:
    pip install anthropic requests beautifulsoup4
    Set ANTHROPIC_API_KEY environment variable.
"""

import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
OUT_PATH    = Path("restaurant_extraction_latest.json")
BATCH_SIZE  = 20
MAX_CHARS   = 2500   # characters to extract per article body

EXTRACTION_PROMPT = """\
You are an expert, precise data extractor specialized in retail and restaurant openings and closures.
I will provide multiple news articles (each starting with --- Article N ---).
For EVERY article, extract the following information STRICTLY from the text provided only.
No assumptions, no external knowledge, no guessing zip codes, no inferring dates or statuses.

🔍 Extract these fields
• Store/Shop/Restaurant Name
• Location or Full Address with zip code
  (if no zip code mentioned → write the address exactly as given;
   if no address at all → write "Address not specified")
• Event Type: write exactly "Opening" or "Closing" or "remodel" based only on article content
• Event Date:
  - Openings → Opening Date
  - Closures → Closing Date
  (write exact date or month/year if mentioned; otherwise write exactly "Not specified")
• Status:
  - Openings → e.g. "under construction", "opening soon", "set to open", "recently opened", "grand opening on…"
  - Closures → e.g. "closed", "permanently closed", "closing soon", "set to close", "shut down", "liquidation"
  👉 Use exact phrasing or closest direct wording from the article — do NOT invent or normalize
• Short Description: exactly 2–3 concise sentences summarizing ONLY what the article says

📊 Output format — ONE clean Markdown table with these exact headers in this order:
| Store/Shop/Restaurant Name | Location or Full Address with zip code | Event Type | Event Date | Status | Short Description | Article Link |

📌 Rules
• One row per article, in the order given
• Multiple businesses in one article → separate row for each
• Article with both openings and closures → extract each separately
• Article with zero relevant info → row with Store Name "No qualifying business found", all other columns "N/A"

🚫 Constraints: No assumptions • No external data • No inferred addresses or dates • No normalizing status text

📎 Final section (mandatory) — after the table add:
Non-working or unusable articles List:
• Article number or URL — Reason (paywall / no business details / duplicate / text missing / etc.)
If none, write: None

✅ Articles to extract:\
"""

# ── Column → JSON key map ─────────────────────────────────────────────────────
COLUMN_MAP = {
    "store/shop/restaurant name":             "store_name",
    "store/restaurant":                       "store_name",
    "store / restaurant":                     "store_name",
    "location or full address with zip code": "location",
    "location or full address":               "location",
    "location":                               "location",
    "event type":                             "event_type",
    "event date":                             "event_date",
    "status":                                 "status",
    "short description":                      "short_description",
    "article link":                           "article_link",
    "article":                                "article_link",
}


# ── Parsing helpers ───────────────────────────────────────────────────────────
def clean_cell(text: str) -> str:
    text = re.sub(
        r'\[([^\]]*)\]\(([^)]*)\)',
        lambda m: m.group(2) if m.group(2).startswith("http") else m.group(1),
        text,
    )
    return re.sub(r'\*+', '', text).strip()


def is_separator(line: str) -> bool:
    return bool(re.match(r'^\s*\|?\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?\s*$', line))


def _is_real(value: str) -> bool:
    return bool(value) and value.strip().lower() not in ("not specified", "n/a", "-", "—")


def synthesize_location(row: dict) -> str:
    """Build a single location string from address/city/state/zipcode columns
    (some batches split the address into separate fields instead of a single
    'Location' column, which would otherwise leave row['location'] blank)."""
    address = row.get("address", "")
    city    = row.get("city", "")
    state   = row.get("state", "")
    zipcode = row.get("zipcode", "")
    parts = [v.strip() for v in (address, city) if _is_real(v)]
    state_zip = " ".join(v.strip() for v in (state, zipcode) if _is_real(v))
    if state_zip:
        parts.append(state_zip)
    return ", ".join(parts)


def parse_table(text: str) -> list[dict]:
    rows, headers, keys = [], [], []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if in_table:
                in_table = False
            continue
        if '|' not in line or is_separator(line):
            continue
        cells = [clean_cell(c) for c in line.strip().strip('|').split('|')]
        if not headers:
            headers = cells
            for h in headers:
                keys.append(COLUMN_MAP.get(h.lower(), h.lower().replace(' ', '_').replace('/', '_')))
            in_table = True
            continue
        if not in_table:
            continue
        if len(cells) < len(keys):
            cells += [''] * (len(keys) - len(cells))
        row = {keys[i]: cells[i] for i in range(len(keys))}
        et = row.get('event_type', '').strip().lower()
        row['event_type'] = {'opening': 'Opening', 'closing': 'Closing', 'remodel': 'Remodel'}.get(et, row.get('event_type', ''))
        if not row.get('location', '').strip() and row.get('address'):
            row['location'] = synthesize_location(row)
        if all(v in ('', '—', '-', 'N/A') for v in row.values()):
            continue
        rows.append(row)
    return rows


def parse_non_working(text: str) -> list[dict]:
    out = []
    m = re.search(r'Non[- ]?working[^\n]*\n(.*?)(?:\Z)', text, re.IGNORECASE | re.DOTALL)
    if not m:
        return out
    for line in m.group(1).splitlines():
        line = line.strip().lstrip('•-*').strip()
        if not line or line.lower() == 'none':
            continue
        hit = re.match(r'^(https?://\S+|Article\s*\d+|\d+)[^\w]*[:\-–—]?\s*(.*)', line, re.IGNORECASE)
        if hit:
            out.append({"identifier": hit.group(1).strip(), "reason": hit.group(2).strip()})
        else:
            out.append({"identifier": line, "reason": ""})
    return out


# ── Article fetcher ───────────────────────────────────────────────────────────
def fetch_article(url: str) -> str:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        body = (
            soup.find("article")
            or soup.find("main")
            or soup.find("div", class_=lambda c: c and "content" in c.lower())
            or soup
        )
        return body.get_text(separator=" ", strip=True)[:MAX_CHARS]
    except Exception as exc:
        return f"[Could not fetch article: {exc}]"


# ── JSON persistence ──────────────────────────────────────────────────────────
def load_existing() -> tuple[list, list]:
    if OUT_PATH.exists():
        try:
            p = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            return p.get("data", []), p.get("non_working", [])
        except Exception:
            pass
    return [], []


def save(data: list, non_working: list) -> None:
    OUT_PATH.write_text(
        json.dumps({"last_updated": date.today().isoformat(), "data": data, "non_working": non_working}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    batch_size   = int(args[args.index('--batch-size')   + 1]) if '--batch-size'   in args else BATCH_SIZE
    max_articles = int(args[args.index('--max-articles') + 1]) if '--max-articles' in args else None
    reset        = '--reset' in args

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌  ANTHROPIC_API_KEY environment variable is not set.")
        print("    Set it with:  set ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    if reset:
        save([], [])
        print(f"✓  Reset {OUT_PATH}")

    rest_path = Path("restaurant_latest.json")
    if not rest_path.exists():
        print("❌  restaurant_latest.json not found.")
        sys.exit(1)

    raw = json.loads(rest_path.read_text(encoding="utf-8"))
    articles = raw.get("data", []) if isinstance(raw, dict) else raw
    if max_articles:
        articles = articles[:max_articles]

    total_batches = (len(articles) + batch_size - 1) // batch_size
    print(f"✓  {len(articles)} articles  |  batch size: {batch_size}  |  {total_batches} batch(es)\n")

    client = anthropic.Anthropic()
    all_rows, all_nw = load_existing()

    for b_start in range(0, len(articles), batch_size):
        batch     = articles[b_start : b_start + batch_size]
        b_num     = b_start // batch_size + 1
        b_end     = b_start + len(batch)
        print(f"━━ Batch {b_num}/{total_batches}  (articles {b_start + 1}–{b_end}) ━━")

        # Fetch article bodies
        blocks = []
        for i, art in enumerate(batch, b_start + 1):
            url   = art.get("url",     "")
            title = art.get("title",   "")
            addr  = art.get("address", "")
            print(f"  [{i:>3}] {url[:80]}")
            body  = fetch_article(url)
            block = f"--- Article {i} ---\nURL: {url}\nTitle: {title}\n"
            if addr:
                block += f"Address (from scraper): {addr}\n"
            block += f"\n{body}"
            blocks.append(block)
            time.sleep(0.4)

        # Call Claude
        user_message = EXTRACTION_PROMPT + "\n\n" + "\n\n".join(blocks)
        print(f"\n  → Calling Claude API...")

        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=16000,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            response_text = stream.get_final_message().content
            response_text = next((b.text for b in response_text if b.type == "text"), "")

        # Parse and save
        batch_rows = parse_table(response_text)
        batch_nw   = parse_non_working(response_text)
        all_rows.extend(batch_rows)
        all_nw.extend(batch_nw)
        save(all_rows, all_nw)

        print(f"  ✓ {len(batch_rows)} rows extracted  (running total: {len(all_rows)})")
        if batch_nw:
            print(f"    Non-working: {len(batch_nw)}")
        print()

        if b_end < len(articles):
            time.sleep(2)  # brief pause between batches

    print(f"✅  Complete!  {len(all_rows)} rows → {OUT_PATH}")
    print("Next: git add restaurant_extraction_latest.json && git commit && git push")


if __name__ == "__main__":
    main()
