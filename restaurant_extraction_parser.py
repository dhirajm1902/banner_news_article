#!/usr/bin/env python3
"""
Restaurant Extraction Parser
Reads a Claude Markdown table output file, parses each row, and saves
the result to restaurant_extraction_latest.json.

Batch workflow (77 articles → split into 3–4 batches of ~20):
  1. python restaurant_extractor.py            → restaurant_articles_ready.txt
  2. Split the txt into batches manually
  3. For each batch:
       a. Paste into Claude with extraction prompt → Markdown table
       b. Save Claude's response to a file        → e.g. batch1.md
       c. python restaurant_extraction_parser.py batch1.md          (first batch)
          python restaurant_extraction_parser.py batch2.md --append  (subsequent)
  4. git add restaurant_extraction_latest.json && git commit && git push

Usage:
    python restaurant_extraction_parser.py [input_file] [--append] [--reset]

    input_file  : Markdown file with Claude's output  (default: restaurant_extraction_raw.md)
    --append    : Merge new rows INTO the existing JSON instead of overwriting
    --reset     : Clear the existing JSON and start fresh (ignores input file)
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

OUT_PATH = Path("restaurant_extraction_latest.json")

# ── Column mapping ─────────────────────────────────────────────────────────────
COLUMN_MAP = {
    "store/shop/restaurant name":            "store_name",
    "store/restaurant":                      "store_name",
    "store / restaurant":                    "store_name",
    "location or full address with zip code":"location",
    "location or full address":              "location",
    "location":                              "location",
    "event type":                            "event_type",
    "event date":                            "event_date",
    "status":                                "status",
    "short description":                     "short_description",
    "article link":                          "article_link",
    "article":                               "article_link",
    "published date":                        "published_date",
}


def clean_cell(text: str) -> str:
    # Markdown link [label](url) → keep url if it's a real URL, else keep label
    text = re.sub(
        r'\[([^\]]*)\]\(([^)]*)\)',
        lambda m: m.group(2) if m.group(2).startswith("http") else m.group(1),
        text,
    )
    text = re.sub(r'\*+', '', text)   # remove bold/italic markers
    return text.strip()


def parse_table_row(line: str) -> list[str]:
    return [clean_cell(c) for c in line.strip().strip("|").split("|")]


def is_separator_row(line: str) -> bool:
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


def parse_non_working_section(text: str) -> list[dict]:
    non_working = []
    m = re.search(
        r'(?:Non[- ]?working|Unusable|Non[- ]?usable)[^\n]*\n(.*?)(?:\Z)',
        text, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return non_working
    for line in m.group(1).splitlines():
        line = line.strip().lstrip("•-*").strip()
        if not line:
            continue
        hit = re.match(r'^(https?://\S+|Article\s*\d+|\d+)[^\w]*[:\-–—]?\s*(.*)', line, re.IGNORECASE)
        if hit:
            non_working.append({"identifier": hit.group(1).strip(), "reason": hit.group(2).strip()})
        else:
            non_working.append({"identifier": line, "reason": ""})
    return non_working


def parse_markdown_table(text: str) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    warnings: list[str] = []
    headers: list[str] = []
    json_keys: list[str] = []
    in_table = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if in_table:
                in_table = False
            continue
        if "|" not in line:
            continue
        if is_separator_row(line):
            continue

        cells = parse_table_row(line)

        if not headers:
            headers = cells
            for h in headers:
                key = COLUMN_MAP.get(h.lower(), h.lower().replace(" ", "_").replace("/", "_"))
                json_keys.append(key)
            in_table = True
            continue

        if not in_table:
            continue

        if len(cells) < len(json_keys):
            cells += [""] * (len(json_keys) - len(cells))

        row = {json_keys[i]: cells[i] for i in range(len(json_keys))}

        # Normalise event_type
        et = row.get("event_type", "").strip().lower()
        row["event_type"] = {"opening": "Opening", "closing": "Closing", "remodel": "Remodel"}.get(et, row.get("event_type", ""))

        # Some batches split the address into address/city/state/zipcode
        # columns instead of a single "Location" column — synthesize one.
        if not row.get("location", "").strip() and row.get("address"):
            row["location"] = synthesize_location(row)

        # Skip blank / separator artefacts
        if all(v in ("", "—", "-", "N/A") for v in row.values()):
            continue

        rows.append(row)

    if not rows:
        warnings.append("No table rows parsed — check that the file contains a Markdown table.")

    return rows, warnings


def load_existing() -> tuple[list[dict], list[dict]]:
    """Load existing data and non_working from the output JSON (if it exists)."""
    if OUT_PATH.exists():
        try:
            payload = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            return payload.get("data", []), payload.get("non_working", [])
        except Exception:
            pass
    return [], []


def save(data: list[dict], non_working: list[dict]) -> None:
    payload = {
        "last_updated": date.today().isoformat(),
        "data": data,
        "non_working": non_working,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_summary(existing_count: int, new_count: int, total: int, non_working: int, mode: str) -> None:
    if mode == "append":
        print(f"✓  Appended {new_count} new row(s)  (was {existing_count}, now {total})")
    else:
        print(f"✓  Parsed {new_count} row(s)")
    if non_working:
        print(f"   Non-working articles logged: {non_working}")
    print(f"✓  Saved → {OUT_PATH}")
    print(f"\nTotal rows in JSON: {total}")
    print("Next: git add restaurant_extraction_latest.json && git commit && git push")


def main():
    args = sys.argv[1:]

    # --reset: wipe the JSON clean
    if "--reset" in args:
        save([], [])
        print(f"✓  Reset {OUT_PATH} — all data cleared.")
        return

    append_mode = "--append" in args
    file_args   = [a for a in args if not a.startswith("--")]
    input_path  = Path(file_args[0]) if file_args else Path("restaurant_extraction_raw.md")

    if not input_path.exists():
        print(f"❌  Input file not found: {input_path}")
        print("    Save Claude's Markdown output to that file and re-run.")
        sys.exit(1)

    text = input_path.read_text(encoding="utf-8")
    new_rows, warnings = parse_markdown_table(text)
    new_non_working    = parse_non_working_section(text)

    for w in warnings:
        print(f"⚠️  {w}")

    if append_mode:
        existing_data, existing_nw = load_existing()
        merged_data       = existing_data + new_rows
        merged_non_working = existing_nw  + new_non_working
        save(merged_data, merged_non_working)
        print_summary(len(existing_data), len(new_rows), len(merged_data), len(new_non_working), "append")
    else:
        save(new_rows, new_non_working)
        print_summary(0, len(new_rows), len(new_rows), len(new_non_working), "overwrite")


if __name__ == "__main__":
    main()
