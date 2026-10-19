#!/usr/bin/env python3
"""
Daily News Recovery
Rebuilds batch files for articles that dropped out of docs/news_data.json
before anyone ran daily_news_prepare.py (e.g. Friday articles that only
appeared in the weekend runs). Reads past versions of news_data.json from
git history — nothing is fetched from GitHub or pushed.

Usage:
    python daily_news_recover.py 2026-10-16              # one published date
    python daily_news_recover.py 2026-10-16 2026-10-15   # several dates
    python daily_news_recover.py 2026-10-16 --dry-run    # just count

Output:
    newsbatch_recover_1.txt, newsbatch_recover_2.txt, ...
    newsbatch_recover_N_output.md placeholders (picked up by
    build_extraction_masters.py; not renamed by daily_news_prepare.py)
"""

import csv
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from daily_news_prepare import (
    BATCH_SIZE, EXTRACTION_PROMPT, batched_keys, fetch_article, resolve_google_link,
)

NEWS_JSON   = "docs/news_data.json"
MASTER_CSV  = Path("data/daily_news/daily_news_extraction_master.csv")
PREFIX      = "newsbatch_recover"
HISTORY     = 15   # how many past commits of news_data.json to scan


def git(*args) -> bytes:
    return subprocess.check_output(["git", *args])


def main():
    dates   = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    if not dates or not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in dates):
        print("Usage: python daily_news_recover.py YYYY-MM-DD [YYYY-MM-DD ...] [--dry-run]")
        return

    # ── Collect every article published on those dates from git history ──────
    commits = git("log", f"-{HISTORY}", "--format=%h", "--", NEWS_JSON).decode().split()
    found = {}
    for c in commits:
        try:
            data = json.loads(git("show", f"{c}:{NEWS_JSON}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        for art in data.get("articles", []):
            # the same article can carry a different news.google.com link
            # in each run, so key on the title
            key = art.get("title", "").lower().strip()
            if key and (art.get("published_date") or "")[:10] in dates:
                found.setdefault(key, art)
    print(f"✓  {len(found)} article(s) published on {', '.join(dates)} "
          f"found in last {len(commits)} versions of {NEWS_JSON}")

    # ── Skip anything already extracted or already sitting in a batch file ───
    done = set()
    if MASTER_CSV.exists():
        with open(MASTER_CSV, encoding="utf-8", newline="") as f:
            done = {row["article_link"] for row in csv.DictReader(f)}
    batched_urls, batched_titles = batched_keys()
    done |= batched_urls

    articles = [
        a for title, a in found.items()
        if title not in batched_titles and a.get("direct_link", "") not in done
    ]
    articles.sort(key=lambda a: a.get("published_date", ""), reverse=True)
    total_batches = (len(articles) + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"✓  {len(found) - len(articles)} already extracted / batched — skipped")
    print(f"✓  {len(articles)} to recover -> {total_batches} batch file(s)\n")

    if dry_run or not articles:
        return

    existing = sorted(Path(".").glob(f"{PREFIX}_*"))
    if existing:
        print(f"❌  {PREFIX}_* files already exist — extract or delete them first:")
        for p in existing:
            print(f"    {p}")
        return

    for b in range(total_batches):
        Path(f"{PREFIX}_{b + 1}_output.md").write_text("", encoding="utf-8")

    for b in range(total_batches):
        batch    = articles[b * BATCH_SIZE : (b + 1) * BATCH_SIZE]
        b_start  = b * BATCH_SIZE + 1
        filename = f"{PREFIX}_{b + 1}.txt"
        print(f"── Batch {b + 1}/{total_batches}  (articles {b_start}–{b_start + len(batch) - 1}) ──")

        blocks = []
        for i, art in enumerate(batch, b_start):
            url = resolve_google_link(art.get("direct_link", ""))
            print(f"  [{i:>3}] {url[:75]}")
            blocks.append(
                f"--- Article {i} ---\n"
                f"URL: {url}\n"
                f"Title: {art.get('title', '')}\n"
                f"Status hint: {art.get('status', '')} | Industry: {art.get('industry', '')}\n"
                f"Published: {art.get('published_date', '')}\n"
                f"\n{fetch_article(url)}"
            )
            time.sleep(0.4)

        Path(filename).write_text(EXTRACTION_PROMPT + "\n\n" + "\n\n".join(blocks), encoding="utf-8")
        print(f"  ✓ Saved → {filename}\n")

    print("=" * 60)
    print("NEXT STEPS")
    print("=" * 60)
    for b in range(total_batches):
        print(f"\n  {PREFIX}_{b + 1}.txt -> paste into Claude -> save as {PREFIX}_{b + 1}_output.md")
        print(f"    Run: python daily_news_save_output_cluade.py {PREFIX}_{b + 1}_output.md --append")
    print("\n  When all are extracted, delete the newsbatch_recover_* files.")
    print("=" * 60)


if __name__ == "__main__":
    main()
