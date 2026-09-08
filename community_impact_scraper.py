"""
Community Impact Scraper
=========================
Source: https://communityimpact.com/business/ (Patchright — Cloudflare
"Just a moment..." bot-check, same as company_website_comingsoon.py's
Costco scraper, so it needs a real (non-headless) browser window; run
this under xvfb-run in CI).

The page groups articles into per-metro-area sections, each with a city
heading (e.g. "Austin") followed by a grid of article cards.

Output:
  community_impact_latest.json        — { last_updated, total, data: [...] }
  data/community_impact/community_impact_YYYY-MM-DD.csv
  master_file/community_impact_master.csv   — accumulates all daily results,
                                               deduplicated by link
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup
from patchright.sync_api import sync_playwright

STORE_NEWS_DIR = Path("data/community_impact")
STORE_NEWS_DIR.mkdir(parents=True, exist_ok=True)
MASTER_DIR = Path("master_file")
MASTER_DIR.mkdir(parents=True, exist_ok=True)
MASTER_FILE = MASTER_DIR / "community_impact_master.csv"

URL = "https://communityimpact.com/business/"

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=False,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = context.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function("document.title !== 'Just a moment...'", timeout=20000)
    # Wait until at least one article card is rendered by JS
    page.wait_for_selector("div.grid", timeout=15000)
    html = page.content()
    browser.close()

soup = BeautifulSoup(html, "html.parser")

records = []
for section in soup.find_all("section"):
    header_div = section.find("div", class_=lambda c: c and "items-center" in c.split() and "gap-3" in c.split())
    city_tag = header_div.find("h2") if header_div else None
    grid = section.find("div", class_=lambda c: c and "grid" in c.split())
    if not (city_tag and grid):
        continue

    city = city_tag.get_text(strip=True)
    for article in grid.find_all("article", class_="group"):
        link_tag = article.find("a")
        header_tag = article.find("h3")
        date_tag = article.find("p")
        img_tag = article.find("img")

        if not (link_tag and header_tag and date_tag):
            continue

        image_src = img_tag["src"] if img_tag and img_tag.get("src") else None
        if image_src and image_src.startswith("/"):
            image_src = "https://communityimpact.com" + image_src

        records.append({
            "city":  city,
            "title": header_tag.get_text(strip=True),
            "date":  date_tag.get_text(strip=True),
            "link":  "https://communityimpact.com" + link_tag["href"],
            "image": image_src,
        })

print(f"📋 Total articles scraped: {len(records)}")

today = datetime.now().strftime("%Y-%m-%d")
df = pd.DataFrame(records, columns=["city", "title", "date", "link", "image"])
df.to_csv(STORE_NEWS_DIR / f"community_impact_{today}.csv", index=False, encoding="utf-8")
print(f"✅ CSV saved → {STORE_NEWS_DIR / f'community_impact_{today}.csv'} ({len(df)} rows)")

# ── JSON — matches the {last_updated, total, data} shape read by the
#    frontend tab and by community_impact_auto_extract.py ────────────────────
json_payload = {
    "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    "total": len(records),
    "data": records,
}
with open("community_impact_latest.json", "w", encoding="utf-8") as f:
    json.dump(json_payload, f, ensure_ascii=False, indent=2)
print(f"✅ JSON saved → community_impact_latest.json ({len(records)} records)")

# ── Master file — accumulates all daily results ───────────────────────────────
df_new = df.copy()
df_new["Date_Appended"] = today

if MASTER_FILE.exists():
    df_master = pd.read_csv(MASTER_FILE, encoding="utf-8", dtype=str)
    df_master = pd.concat([df_master, df_new], ignore_index=True)
else:
    df_master = df_new

df_master = df_master.drop_duplicates(subset=["link"])
# Newest scrape day first — matches restaurant_scraper.py / ct_scoop_scraper.py.
# There's no per-article timestamp to sort by (only relative text like "16h
# ago"), so Date_Appended (day granularity) is the best available proxy.
df_master = df_master.sort_values("Date_Appended", ascending=False, kind="stable")
df_master.to_csv(MASTER_FILE, index=False, encoding="utf-8")
print(f"✅ Master file updated: {MASTER_FILE} ({len(df_master)} total rows)")
