#!/usr/bin/env python3
"""
Build Extraction Master CSVs
Reads all Claude batch output markdown files, parses the extraction tables,
and writes consolidated master CSVs to master_file/.

Output files:
  master_file/restaurant_master_extraction.csv     <- batches/batch_*.md
  master_file/businessdebut_master_extraction.csv  <- batches/bizdbatch_*.md
  master_file/ct_scoop_master_extraction.csv       <- batches/ct_scoop_*.md
  master_file/daily_news_master_extraction.csv     <- newsbatch_*.md (root level)

Usage:
    python build_extraction_masters.py          # rebuild all master CSVs from scratch
    python build_extraction_masters.py --append # append new rows (skip duplicates by article_link)
"""

import csv
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent
MASTER_DIR = ROOT / "master_file"
TODAY = date.today().isoformat()

COLUMN_MAP = {
    "store/shop/restaurant name":             "store_name",
    "store/restaurant":                       "store_name",
    "store / restaurant":                     "store_name",
    "store name":                             "store_name",
    "location or full address with zip code": "location",
    "location or full address":               "location",
    "location":                               "location",
    "event type":                             "event_type",
    "event date":                             "event_date",
    "status":                                 "status",
    "short description":                      "short_description",
    "article link":                           "article_link",
    "article":                                "article_link",
    "published date":                         "published_date",
}

CSV_COLUMNS = [
    "store_name", "location", "event_type", "event_date",
    "status", "short_description", "article_link", "published_date",
    "source_batch", "Date_Appended",
]

BATCH_GROUPS = {
    "restaurant_master_extraction.csv": sorted(
        (ROOT / "batches").glob("batch_*_output.md")
    ),
    "businessdebut_master_extraction.csv": sorted(
        (ROOT / "batches").glob("bizdbatch_*_output.md")
    ),
    "ct_scoop_master_extraction.csv": sorted(
        (ROOT / "batches").glob("ct_scoop_*_output.md")
    ),
    "daily_news_master_extraction.csv": sorted(
        ROOT.glob("newsbatch_*_output.md")
    ),
}


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


def parse_batch_file(path: Path) -> list[dict]:
    rows = []
    headers: list[str] = []
    json_keys: list[str] = []
    in_table = False

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            if in_table:
                in_table = False
            continue
        if "|" not in line:
            continue
        if not line.startswith("|"):
            # Guard against a leading sentence glued onto the same line as the
            # table (e.g. "I'll extract the data...| Store Name | ..."), which
            # would otherwise be parsed as a bogus extra header/column.
            line = line[line.index("|"):]
        if is_separator(line):
            continue

        cells = [clean_cell(c) for c in line.strip().strip("|").split("|")]

        if not headers:
            headers = cells
            json_keys = [
                COLUMN_MAP.get(h.lower(), h.lower().replace(" ", "_").replace("/", "_"))
                for h in headers
            ]
            in_table = True
            continue

        if not in_table:
            continue

        if len(cells) < len(json_keys):
            cells += [""] * (len(json_keys) - len(cells))

        row = {json_keys[i]: cells[i] for i in range(len(json_keys))}

        et = row.get("event_type", "").strip().lower()
        row["event_type"] = {"opening": "Opening", "closing": "Closing", "remodel": "Remodel"}.get(
            et, row.get("event_type", "")
        )

        if not row.get("location", "").strip() and row.get("address"):
            row["location"] = synthesize_location(row)

        if all(v in ("", "—", "-", "N/A") for v in row.values()):
            continue

        if not row.get("location"):
            location_parts = [row.get("address", ""), row.get("city", "")]
            state_zip = " ".join(p for p in (row.get("state", ""), row.get("zipcode", "")) if p)
            if state_zip:
                location_parts.append(state_zip)
            row["location"] = ", ".join(p for p in location_parts if p)

        row["source_batch"] = path.stem
        row["Date_Appended"] = TODAY
        rows.append(row)

    return rows


def load_existing_links(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    with csv_path.open(encoding="utf-8", newline="") as f:
        return {row.get("article_link", "") for row in csv.DictReader(f)}


def write_csv(csv_path: Path, rows: list[dict], append: bool) -> None:
    existing_links = load_existing_links(csv_path) if append else set()
    new_rows = [r for r in rows if r.get("article_link", "") not in existing_links]

    mode = "a" if (append and csv_path.exists()) else "w"
    with csv_path.open(mode, encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if mode == "w":
            writer.writeheader()
        writer.writerows(new_rows)

    return len(new_rows)


def main():
    append = "--append" in sys.argv
    MASTER_DIR.mkdir(exist_ok=True)

    for csv_name, batch_files in BATCH_GROUPS.items():
        csv_path = MASTER_DIR / csv_name
        all_rows: list[dict] = []

        if not batch_files:
            print(f"  no batch files found for {csv_name}")
            continue

        for bf in batch_files:
            rows = parse_batch_file(bf)
            all_rows.extend(rows)
            print(f"  {bf.name}: {len(rows)} rows")

        added = write_csv(csv_path, all_rows, append)
        mode_label = f"appended {added} new" if append else f"wrote {added}"
        print(f"  -> {csv_path.relative_to(ROOT)}  ({mode_label} rows, {len(all_rows)} total parsed)\n")


if __name__ == "__main__":
    main()
