"""
daily_news_save_output_cluade.py
─────────────────────────────────
Run this AFTER you have:
  1. Opened a newsbatch_N.txt file and pasted it into Claude.ai
  2. Copied Claude's full Markdown table response and saved it as newsbatch_N_output.md

What it does:
  - Parses Claude's Markdown table from the output file
  - Saves daily_news_extraction_latest.json  ← dashboard reads this
  - Appends new rows to data/daily_news/daily_news_extraction_master.csv

Usage:
    python daily_news_save_output_cluade.py newsbatch_1_output.md
    python daily_news_save_output_cluade.py newsbatch_2_output.md --append
    python daily_news_save_output_cluade.py --reset    # clear extraction JSON
"""

import csv
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

OUTPUT_JSON = "daily_news_extraction_latest.json"

# Determine input file from first non-flag argument
_file_args = [a for a in sys.argv[1:] if not a.startswith("--")]
RESPONSE_FILE = _file_args[0] if _file_args else "claude_response.txt"

# ─────────────────────────────────────────────────────────
# 0. Handle --reset flag
# ─────────────────────────────────────────────────────────
if "--reset" in sys.argv:
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"last_updated": date.today().isoformat(), "data": []}, f, indent=2)
    print(f"🔄  Reset {OUTPUT_JSON} to empty.")
    sys.exit(0)

APPEND_MODE = "--append" in sys.argv

# ─────────────────────────────────────────────────────────
# 1. Read Claude's response
# ─────────────────────────────────────────────────────────
if not os.path.exists(RESPONSE_FILE):
    print(f"❌  {RESPONSE_FILE} not found.")
    print("    Paste Claude's Markdown table response into that file and re-run.")
    sys.exit(1)

raw = open(RESPONSE_FILE, encoding="utf-8").read()

if not raw.strip() or raw.strip().startswith("#"):
    print(f"❌  {RESPONSE_FILE} is empty or still has placeholder text.")
    print("    Paste Claude's response and re-run.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────
# 2. Parse Markdown table(s)
# ─────────────────────────────────────────────────────────
def _parse_md_table(text: str) -> list[dict]:
    rows, headers = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if re.match(r'^\|[\s\-|:]+\|$', line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not headers:
            headers = cells
        else:
            while len(cells) < len(headers):
                cells.append("")
            rows.append(dict(zip(headers, cells[:len(headers)])))
    return rows

all_rows = _parse_md_table(raw)

if not all_rows:
    print("❌  No Markdown table found in claude_response.txt.")
    print("    Make sure you pasted Claude's full response including the table.")
    sys.exit(1)

print(f"✅  Parsed {len(all_rows)} rows from Claude's response")

# ─────────────────────────────────────────────────────────
# 3. Normalise column names
# ─────────────────────────────────────────────────────────
_COL_MAP_PATTERNS = {
    "store_name":        r"store|shop|restaurant|business|name",
    "location":          r"address|location",
    "event_type":        r"event.?type|type",
    "event_date":        r"event.?date|^date$",
    "status":            r"status",
    "short_description": r"description|summary|short",
    "article_link":      r"link|url|article",
    "published_date":    r"published",
}

if all_rows:
    raw_headers = list(all_rows[0].keys())
    col_mapping = {}
    for std_col, pattern in _COL_MAP_PATTERNS.items():
        for h in raw_headers:
            if re.search(pattern, h, re.IGNORECASE):
                col_mapping[h] = std_col
                break

    normalized = []
    for row in all_rows:
        norm = {col_mapping.get(k, k): v for k, v in row.items()}
        normalized.append(norm)
    all_rows = normalized

# ─────────────────────────────────────────────────────────
# 4. Build final records
# ─────────────────────────────────────────────────────────
today_str = date.today().isoformat()

new_records = []
for row in all_rows:
    new_records.append({
        "store_name":        row.get("store_name",        ""),
        "location":          row.get("location",          "Address not specified"),
        "event_type":        row.get("event_type",        ""),
        "event_date":        row.get("event_date",        "Not specified"),
        "status":            row.get("status",            ""),
        "short_description": row.get("short_description", ""),
        "article_link":      row.get("article_link",      ""),
        "published_date":    row.get("published_date",    ""),
    })

print(f"🔗  {len(new_records)} records ready")

# ─────────────────────────────────────────────────────────
# 5. Merge with existing data if --append
# ─────────────────────────────────────────────────────────
existing_records = []
if APPEND_MODE and os.path.exists(OUTPUT_JSON):
    try:
        existing = json.load(open(OUTPUT_JSON, encoding="utf-8"))
        existing_records = existing.get("data", [])
        print(f"📂  Loaded {len(existing_records)} existing records (append mode)")
    except Exception as e:
        print(f"⚠️   Could not read {OUTPUT_JSON}: {e}")

final_records = existing_records + new_records

# ─────────────────────────────────────────────────────────
# 6. Save daily_news_extraction_latest.json
# ─────────────────────────────────────────────────────────
output = {"last_updated": today_str, "data": final_records}
with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)
print(f"💾  Saved {OUTPUT_JSON}  ({len(final_records)} records)")

print(f"\n✅  Done!")
if not APPEND_MODE:
    print(f"    Run next batch with:  python daily_news_save_output_cluade.py newsbatch_2_output.md --append")
print(f"    When all batches done:  git add daily_news_extraction_latest.json && git commit && git push")

# ─────────────────────────────────────────────────────────
# 7. Update extraction master CSV
# ─────────────────────────────────────────────────────────
MASTER_COLS = ["store_name", "location", "event_type", "event_date",
               "status", "short_description", "article_link", "published_date", "Date_Appended"]
MASTER_FILE = Path("data/daily_news/daily_news_extraction_master.csv")
MASTER_FILE.parent.mkdir(parents=True, exist_ok=True)

existing_rows = []
if MASTER_FILE.exists():
    with open(MASTER_FILE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        existing_rows = list(reader)

seen = {(r.get("article_link", ""), r.get("store_name", "")) for r in existing_rows}
added = 0
for rec in final_records:
    key = (rec.get("article_link", ""), rec.get("store_name", ""))
    if key not in seen:
        existing_rows.append({**rec, "Date_Appended": today_str})
        seen.add(key)
        added += 1

existing_rows.sort(key=lambda r: r.get("published_date") or "", reverse=True)

with open(MASTER_FILE, "w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=MASTER_COLS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(existing_rows)

print(f"📂  Extraction master updated: {MASTER_FILE}  (+{added} new, {len(existing_rows)} total)")
