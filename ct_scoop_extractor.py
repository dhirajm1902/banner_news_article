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

🌎 Geographic filter (STRICT)
• Only extract businesses located in the USA or Canada
• If the article is about a business in any other country (UK, Australia, India, UAE, etc.) → DO NOT add any row to the table; add it ONLY to the Non-working list as "Outside USA/Canada"
• If an article covers both USA/Canada locations AND international locations → extract only the USA/Canada rows, skip the rest; add the article to the Non-working list as "Outside USA/Canada (partial)"

📌 Rules
• Add one row per article in the order the articles are given
• If an article contains multiple businesses, create a separate row for each
• If an article includes both openings and closures, extract each separately
• For Published Date → copy exactly the value from the "Published:" line in the article metadata
• If a USA/Canada article has zero relevant business opening or closure information, still include a row with:
  - Store Name: "No qualifying business found"
  - Other columns: "N/A"
• ❌ Never add a table row for articles outside USA/Canada — those go in the Non-working list only

🚫 Strict constraints
• ❌ No assumptions  • ❌ No external data  • ❌ No inferred addresses or dates  • ❌ No rewriting or normalizing status text
• ❌ No extraction of businesses outside USA or Canada

📎 Final section (mandatory)
At the very end of your response, add:
Non-working or unusable articles List:
• Article number — Reason (paywall / no business details / duplicate / text missing / Outside USA/Canada / etc.)
If none, write: None

✅ Articles below — extract now:
"""


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

    output = EXTRACTION_PROMPT + "\n\n".join(blocks)
    out_path = Path("ct_scoop_articles_ready.txt")
    out_path.write_text(output, encoding="utf-8")
    print(f"\n✓ Article text saved to {out_path}")
    print("  → Paste into Claude, then save response to batches/ct_scoop_batch1_output.md")


if __name__ == "__main__":
    main()
