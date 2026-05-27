#!/usr/bin/env python3
"""
CT Scoop Extraction Parser
Reads a Claude Markdown table output file, parses each row, and saves
the result to ct_scoop_extraction_latest.json.

Batch workflow:
  1. python ct_scoop_extractor.py             → ct_scoop_articles_ready.txt
  2. For each batch:
       a. Paste into Claude with extraction prompt → Markdown table
       b. Save Claude's response to a file        → e.g. ct_scoop_batch1_output.md
       c. python ct_scoop_extraction_parser.py ct_scoop_batch1_output.md           (first batch)
          python ct_scoop_extraction_parser.py ct_scoop_batch2_output.md --append  (subsequent)
  3. git add ct_scoop_extraction_latest.json && git commit && git push

Usage:
    python ct_scoop_extraction_parser.py [input_file] [--append] [--reset]

    input_file  : Markdown file with Claude's output  (default: ct_scoop_extraction_raw.md)
    --append    : Merge new rows INTO the existing JSON instead of overwriting
    --reset     : Clear the existing JSON and start fresh (ignores input file)
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

OUT_PATH = Path("ct_scoop_extraction_latest.json")

COLUMN_MAP = {
    "store/shop/restaurant name":             "store_name",
    "store/restaurant":                       "store_name",
    "store / restaurant":                     "store_name",
    "store name":                             "store_name",
    "business name":                          "store_name",
    "location or full address with zip code": "location",
    "location or full address":               "location",
    "location":                               "location",
    "event type":                             "event_type",
    "event date":                             "event_date",
    "status":                                 "status",
    "short description":                      "short_description",
    "description":                            "short_description",
    "article link":                           "article_link",
    "article":                                "article_link",
    "source link":                            "article_link",
    "link":                                   "article_link",
}


def clean_cell(text: str) -> str:
    text = re.sub(
        r'\[([^\]]*)\]\(([^)]*)\)',
        lambda m: m.group(2) if m.group(2).startswith("http") else m.group(1),
        text,
    )
    text = re.sub(r'\*+', '', text)
    return text.strip()


def parse_table_row(line: str) -> list[str]:
    return [clean_cell(c) for c in line.strip().strip("|").split("|")]


def is_separator_row(line: str) -> bool:
    return bool(re.match(r'^\s*\|?\s*[-:]+\s*(\|\s*[-:]+\s*)+\|?\s*$', line))


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
        if not line or line.lower() == "none":
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

        et = row.get("event_type", "").strip().lower()
        row["event_type"] = {"opening": "Opening", "closing": "Closing", "remodel": "Remodel"}.get(et, row.get("event_type", ""))

        if all(v in ("", "—", "-", "N/A") for v in row.values()):
            continue

        rows.append(row)

    if not rows:
        warnings.append("No table rows parsed — check that the file contains a Markdown table.")

    return rows, warnings


def clean_html_cell(tag) -> str:
    a = tag.find("a")
    if a and a.get("href", "").startswith("http"):
        return a["href"].strip()
    return tag.get_text(separator=" ", strip=True)


def parse_html_table(text: str) -> tuple[list[dict], list[str]]:
    if not _BS4_AVAILABLE:
        return [], ["beautifulsoup4 is not installed — run: pip install beautifulsoup4"]

    rows: list[dict] = []
    warnings: list[str] = []
    soup = BeautifulSoup(text, "html.parser")
    table = soup.find("table")
    if not table:
        return [], ["No <table> found in HTML output."]

    th_tags = table.find_all("th")
    json_keys = []
    for th in th_tags:
        h = th.get_text(strip=True).lower()
        key = COLUMN_MAP.get(h, h.replace(" ", "_").replace("/", "_"))
        json_keys.append(key)

    start_col = 1 if json_keys and json_keys[0] == "#" else 0
    json_keys = json_keys[start_col:]

    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cells = [clean_html_cell(td) for td in tds][start_col:]
        if len(cells) < len(json_keys):
            cells += [""] * (len(json_keys) - len(cells))
        row = {json_keys[i]: cells[i] for i in range(len(json_keys))}

        et = row.get("event_type", "").strip().lower()
        row["event_type"] = {"opening": "Opening", "closing": "Closing", "remodel": "Remodel"}.get(et, row.get("event_type", ""))

        if all(v in ("", "—", "-", "N/A") for v in row.values()):
            continue
        rows.append(row)

    if not rows:
        warnings.append("No data rows found in HTML table.")

    return rows, warnings


def parse_html_non_working(text: str) -> list[dict]:
    if not _BS4_AVAILABLE:
        return []
    non_working = []
    soup = BeautifulSoup(text, "html.parser")
    for div in soup.find_all("div", class_="note"):
        h3 = div.find("h3")
        if not h3 or "non" not in h3.get_text(strip=True).lower():
            continue
        for p in div.find_all("p"):
            line = p.get_text(strip=True)
            if not line:
                continue
            hit = re.match(r'^(Article\s*\d+[a-z]?|https?://\S+)[^:]*:\s*(.*)', line, re.IGNORECASE)
            if hit:
                non_working.append({"identifier": hit.group(1).strip(), "reason": hit.group(2).strip()})
            else:
                non_working.append({"identifier": line[:80], "reason": ""})
    return non_working


def load_existing() -> tuple[list[dict], list[dict]]:
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
    print("Next: git add ct_scoop_extraction_latest.json && git commit && git push")


def main():
    args = sys.argv[1:]

    if "--reset" in args:
        save([], [])
        print(f"✓  Reset {OUT_PATH} — all data cleared.")
        return

    append_mode = "--append" in args
    file_args   = [a for a in args if not a.startswith("--")]
    input_path  = Path(file_args[0]) if file_args else Path("ct_scoop_extraction_raw.md")

    if not input_path.exists():
        print(f"❌  Input file not found: {input_path}")
        print("    Usage: python ct_scoop_extraction_parser.py ct_scoop_batch1_output.md")
        sys.exit(1)

    text = input_path.read_text(encoding="utf-8")

    is_html = text.lstrip().startswith("<!DOCTYPE") or "<table" in text[:500]
    if is_html:
        print("ℹ️  Detected HTML output — parsing as HTML table.")
        new_rows, warnings  = parse_html_table(text)
        new_non_working     = parse_html_non_working(text)
    else:
        new_rows, warnings  = parse_markdown_table(text)
        new_non_working     = parse_non_working_section(text)

    for w in warnings:
        print(f"⚠️  {w}")

    if append_mode:
        existing_data, existing_nw = load_existing()
        merged_data        = existing_data + new_rows
        merged_non_working = existing_nw   + new_non_working
        save(merged_data, merged_non_working)
        print_summary(len(existing_data), len(new_rows), len(merged_data), len(new_non_working), "append")
    else:
        save(new_rows, new_non_working)
        print_summary(0, len(new_rows), len(new_rows), len(new_non_working), "overwrite")


if __name__ == "__main__":
    main()
