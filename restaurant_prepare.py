#!/usr/bin/env python3
"""
Restaurant Batch Preparer
Reads restaurant_latest.json, fetches article text, and creates
ready-to-paste batch files (prompt already included at the top).

Usage:
    python restaurant_prepare.py

Output:
    batch_1.txt, batch_2.txt, ... (each ready to paste directly into Claude)
"""

import json
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BATCH_SIZE       = 20
MAX_CHARS        = 2000
MAX_OUTPUT_KEEP  = 5    # keep only the 5 most recent output files
BATCH_PREFIX     = "batch"          # batch_1.txt, batch_2.txt …
OUTPUT_SUFFIX    = "_output.md"

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
    rest_path = Path("restaurant_latest.json")
    if not rest_path.exists():
        print("❌  restaurant_latest.json not found.")
        return

    raw = json.loads(rest_path.read_text(encoding="utf-8"))
    articles = raw.get("data", []) if isinstance(raw, dict) else raw

    # Filter out already-processed URLs
    done_path = Path("restaurant_extraction_latest.json")
    done_urls = set()
    if done_path.exists():
        done_data = json.loads(done_path.read_text(encoding="utf-8"))
        done_urls = {r.get("article_link", "") for r in done_data.get("data", [])}
        done_urls |= {r.get("identifier", "") for r in done_data.get("non_working", [])}
        print(f"✓  {len(done_urls)} already-processed URLs loaded from extraction JSON")
    articles = [a for a in articles if a.get("url", "") not in done_urls]

    # Sort newest articles first so batch_1 always has the latest
    articles.sort(
        key=lambda a: datetime.strptime(a["date"], "%B %d, %Y") if a.get("date") else datetime.min,
        reverse=True,
    )

    total         = len(articles)
    total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"✓  {total} new articles  →  {total_batches} new batch file(s)\n")

    # ── Shift existing output files up to make room for new batches ────────────
    batches_dir = Path("batches")
    batches_dir.mkdir(exist_ok=True)

    if total_batches > 0:
        existing_outputs = sorted(
            [p for p in batches_dir.glob(f"{BATCH_PREFIX}_*{OUTPUT_SUFFIX}")
             if p.stem.replace(f"{BATCH_PREFIX}_", "").replace("_output", "").isdigit()],
            key=lambda p: int(p.stem.replace(f"{BATCH_PREFIX}_", "").replace("_output", "")),
            reverse=True,
        )
        for p in existing_outputs:
            n = int(p.stem.replace(f"{BATCH_PREFIX}_", "").replace("_output", ""))
            p.rename(batches_dir / f"{BATCH_PREFIX}_{n + total_batches}{OUTPUT_SUFFIX}")
        if existing_outputs:
            print(f"  Shifted {len(existing_outputs)} output file(s) up by {total_batches}")

        # Discard files beyond rolling cap
        discarded = 0
        for p in batches_dir.glob(f"{BATCH_PREFIX}_*{OUTPUT_SUFFIX}"):
            n_str = p.stem.replace(f"{BATCH_PREFIX}_", "").replace("_output", "")
            if n_str.isdigit() and int(n_str) > MAX_OUTPUT_KEEP:
                p.unlink()
                discarded += 1
        if discarded:
            print(f"  Discarded {discarded} old output file(s) beyond limit of {MAX_OUTPUT_KEEP}")

        # Create ALL placeholder output files upfront in batches/
        print("\nCreating output placeholders upfront...")
        for b in range(total_batches):
            placeholder = batches_dir / f"{BATCH_PREFIX}_{b + 1}{OUTPUT_SUFFIX}"
            if not placeholder.exists():
                placeholder.write_text("", encoding="utf-8")
                print(f"  + batches/{placeholder.name}  (ready for extraction)")
        print()

    batch_files = []

    for b in range(total_batches):
        batch    = articles[b * BATCH_SIZE : (b + 1) * BATCH_SIZE]
        b_start  = b * BATCH_SIZE + 1
        b_end    = b_start + len(batch) - 1
        filename = f"{BATCH_PREFIX}_{b + 1}.txt"

        print(f"── Batch {b + 1}/{total_batches}  (articles {b_start}–{b_end}) ──")

        blocks = []
        for i, art in enumerate(batch, b_start):
            url   = art.get("url",     "")
            title = art.get("title",   "")
            addr  = art.get("address", "")
            pub   = art.get("date",    "")
            print(f"  [{i:>3}] {url[:75]}")
            body  = fetch_article(url)
            block = f"--- Article {i} ---\nURL: {url}\nTitle: {title}\n"
            if pub:
                block += f"Published: {pub}\n"
            if addr:
                block += f"Address (from scraper): {addr}\n"
            block += f"\n{body}"
            blocks.append(block)
            time.sleep(0.4)

        content = EXTRACTION_PROMPT + "\n\n" + "\n\n".join(blocks)
        Path(filename).write_text(content, encoding="utf-8")
        batch_files.append(filename)
        print(f"  ✓ Saved → {filename}\n")

    # Print instructions
    print("=" * 60)
    print("NEXT STEPS")
    print("=" * 60)
    for i, fname in enumerate(batch_files):
        out_name = fname.replace(".txt", "_output.md")
        flag = "" if i == 0 else " --append"
        print(f"\n  Batch {i + 1}:")
        print(f"    1. Open {fname} → Copy all text → Paste into Claude")
        print(f"    2. Copy Claude's response → Save as {out_name}")
        print(f"    3. Run: python restaurant_extraction_parser.py {out_name}{flag}")

    print("\n  After all batches:")
    print("    git add restaurant_extraction_latest.json")
    print("    git commit -m \"chore: restaurant extraction update\"")
    print("    git push")
    print("=" * 60)


if __name__ == "__main__":
    main()
