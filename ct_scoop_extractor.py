#!/usr/bin/env python3
"""
CT Scoop Article Fetcher
Reads ct_scoop_latest.json, fetches each article body, and saves the
combined text to ct_scoop_articles_ready.txt — ready to paste into Claude
for manual extraction.

Usage:
    python ct_scoop_extractor.py
"""

import json
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup


def fetch_article_text(url: str, max_chars: int = 2500) -> str:
    """Fetch an article page and return its plain-text body."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=15)
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
        return body.get_text(separator=" ", strip=True)[:max_chars]

    except Exception as exc:
        return f"[Error fetching article: {exc}]"


def main():
    scoop_path = Path("ct_scoop_latest.json")
    if not scoop_path.exists():
        print("ct_scoop_latest.json not found.")
        return

    with open(scoop_path, encoding="utf-8") as f:
        scoop_data = json.load(f)

    articles = scoop_data.get("data", [])
    if not articles:
        print("No articles found.")
        return

    # ── Rotate output files in batches/ so previous extractions are preserved ──
    batches_dir = Path("batches")
    batches_dir.mkdir(exist_ok=True)
    MAX_KEEP = 5

    existing = sorted(
        [p for p in batches_dir.glob("ct_scoop_batch*_output.md")
         if p.stem.replace("ct_scoop_batch", "").replace("_output", "").isdigit()],
        key=lambda p: int(p.stem.replace("ct_scoop_batch", "").replace("_output", "")),
        reverse=True,
    )
    for p in existing:
        n = int(p.stem.replace("ct_scoop_batch", "").replace("_output", ""))
        if n + 1 > MAX_KEEP:
            p.unlink()
        else:
            p.rename(batches_dir / f"ct_scoop_batch{n + 1}_output.md")
    if existing:
        print(f"  Shifted {len(existing)} CT Scoop output file(s) up")

    # Create blank placeholder for this run's output
    placeholder = batches_dir / "ct_scoop_batch1_output.md"
    placeholder.write_text("", encoding="utf-8")
    print(f"  + batches/ct_scoop_batch1_output.md  (ready for extraction)\n")

    print(f"Fetching {len(articles)} article(s)...\n")
    blocks = []
    for i, art in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] {art['link']}")
        text = fetch_article_text(art["link"])
        blocks.append(
            f"--- Article {i} ---\n"
            f"URL: {art['link']}\n"
            f"Headline: {art['heading']}\n\n"
            f"{text}"
        )
        time.sleep(1)

    output = "\n\n".join(blocks)
    out_path = Path("ct_scoop_articles_ready.txt")
    out_path.write_text(output, encoding="utf-8")
    print(f"\n✓ Article text saved to {out_path}")
    print("  → Paste into Claude, then save response to batches/ct_scoop_batch1_output.md")


if __name__ == "__main__":
    main()
