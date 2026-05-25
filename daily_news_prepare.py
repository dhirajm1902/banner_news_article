#!/usr/bin/env python3
"""
Daily News Batch Preparer
Reads docs/news_data.json, fetches article text, and creates
ready-to-paste batch files (prompt already included at the top).

Usage:
    python daily_news_prepare.py

Output:
    newsbatch_1.txt, newsbatch_2.txt, ... (each ready to paste directly into Claude)
"""

import csv
import json
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BATCH_SIZE = 20
MAX_CHARS  = 2000

EXTRACTION_PROMPT = """\
You are an expert, precise data extractor specialized in retail and restaurant openings and closures. I will provide multiple news articles (each usually starting with its source URL). For EVERY article, extract the following information strictly and only from the text provided — no assumptions, no external knowledge, no guessing zip codes, no inferring dates or statuses:

🔍 Extract these fields
• Store/Shop/Restaurant Name
• Location or Full Address with zip code (if no zip code is mentioned, write exactly the address given; if no address at all, write "Address not specified")
• Event Type (write exactly "Opening" or "Closing" or "remodel" based only on the article content)
• Event Date
  - For openings → Opening Date
  - For closures → Closing Date (write exact date or month/year if mentioned; otherwise write exactly "Not specified")
• Status
  - For openings → use phrasing like: "under construction", "opening soon", "set to open", "recently opened", "grand opening on…", "planned for", etc.
  - For closures → use phrasing like: "closed", "permanently closed", "closing soon", "set to close", "shut down", "liquidation", etc.
  👉 Use the exact phrasing or closest direct wording from the article — do NOT invent or normalize
• Short Description (exactly 2–3 concise sentences summarizing ONLY what the article says — no opinions, no extra context)

📊 Output format
Create ONE clean Markdown table with these exact column headers (in this order):
| Store/Shop/Restaurant Name | Location or Full Address with zip code | Event Type | Event Date | Status | Short Description | Article Link | Published Date |

📌 Rules
• Add one row per article in the order the articles are given
• If an article contains multiple businesses, create a separate row for each
• If an article includes both openings and closures, extract each separately
• For Published Date → copy exactly the value from the "Published:" line in the article metadata
• If an article has zero relevant business opening or closure information, still include a row with:
  - Store Name: "No qualifying business found"
  - Other columns: "N/A"

🚫 Strict constraints
• ❌ No assumptions  • ❌ No external data  • ❌ No inferred addresses or dates  • ❌ No rewriting or normalizing status text

📎 Final section (mandatory)
At the very end of your response, add:
Non-working or unusable articles List:
• Article number — Reason (paywall / no business details / duplicate / text missing / etc.)
If none, write: None

✅ Articles below — extract now:
"""


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
        return f"[Could not fetch: {exc}]"


def main():
    src_path = Path("docs/news_data.json")
    if not src_path.exists():
        print("❌  docs/news_data.json not found.")
        print("    This file is generated automatically by GitHub Actions each day.")
        print("    Pull the latest changes from GitHub first:  git pull origin main")
        return

    raw = json.loads(src_path.read_text(encoding="utf-8"))
    articles = raw.get("articles", [])
    if not articles:
        print("❌  No articles found in docs/news_data.json.")
        return

    # Filter out already-processed URLs
    master_path = Path("data/daily_news/daily_news_extraction_master.csv")
    done_urls = set()
    if master_path.exists():
        with open(master_path, encoding="utf-8", newline="") as f:
            done_urls = {row["article_link"] for row in csv.DictReader(f)}
        print(f"✓  {len(done_urls)} already-processed URLs loaded from master CSV")
    articles = [a for a in articles if a.get("direct_link", "") not in done_urls]

    # Sort newest articles first so newsbatch_1 always has the latest
    articles.sort(key=lambda a: a.get("published_date", ""), reverse=True)

    total         = len(articles)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

    generated = raw.get("generated", "unknown")
    print(f"✓  {total} articles  (generated: {generated})")
    print(f"✓  {total_batches} batch file(s) to create\n")

    batch_files = []

    for b in range(total_batches):
        batch    = articles[b * BATCH_SIZE : (b + 1) * BATCH_SIZE]
        b_start  = b * BATCH_SIZE + 1
        b_end    = b_start + len(batch) - 1
        filename = f"newsbatch_{b + 1}.txt"

        print(f"── Batch {b + 1}/{total_batches}  (articles {b_start}–{b_end}) ──")

        blocks = []
        for i, art in enumerate(batch, b_start):
            url      = art.get("direct_link", "")
            title    = art.get("title", "")
            status   = art.get("status", "")
            industry = art.get("industry", "")
            print(f"  [{i:>3}] {url[:75]}")
            body  = fetch_article(url)
            published = art.get("published_date", "")
            block = (
                f"--- Article {i} ---\n"
                f"URL: {url}\n"
                f"Title: {title}\n"
                f"Status hint: {status} | Industry: {industry}\n"
                f"Published: {published}\n"
                f"\n{body}"
            )
            blocks.append(block)
            time.sleep(0.4)

        content = EXTRACTION_PROMPT + "\n\n" + "\n\n".join(blocks)
        Path(filename).write_text(content, encoding="utf-8")
        batch_files.append(filename)
        print(f"  ✓ Saved → {filename}\n")

    print("=" * 60)
    print("NEXT STEPS")
    print("=" * 60)
    for i, fname in enumerate(batch_files):
        out_name = fname.replace(".txt", "_output.md")
        flag = "" if i == 0 else " --append"
        print(f"\n  Batch {i + 1}:")
        print(f"    1. Open {fname} → Copy all text → Paste into Claude")
        print(f"    2. Copy Claude's response → Save as {out_name}")
        print(f"    3. Run: python daily_news_save_output_cluade.py {out_name}{flag}")

    print("\n  After all batches:")
    print("    git add daily_news_extraction_latest.json")
    print("    git commit -m \"chore: daily news extraction update\"")
    print("    git push")
    print("=" * 60)


if __name__ == "__main__":
    main()
