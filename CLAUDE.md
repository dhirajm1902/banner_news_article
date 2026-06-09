# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A retail news aggregation platform that monitors store opening/closing announcements across multiple sources (Google News RSS, BusinessDebut, restaurant listings, WARN Act). Articles are batch-processed through Claude for structured data extraction, then synced to Supabase for a glassmorphic dashboard frontend deployed on GitHub Pages / Vercel.

## Environment Setup

Requires a `.env` file (see `.env.example`):
```
ZYTE_API_KEY=...       # For proxied web scraping
SUPABASE_URL=...
SUPABASE_KEY=...
ANTHROPIC_API_KEY=...  # Used by some extraction scripts
GEMINI_API_KEY=...     # Used by the chat widget in index.html
```

Install dependencies:
```powershell
pip install pandas feedparser python-dateutil supabase selenium requests beautifulsoup4
```

## Key Scripts and Their Role

### Data Fetching
| Script | Source | Notes |
|--------|--------|-------|
| `fetch_banner_store_news.py` | Google News RSS | Core scraper; reads `analyst.csv` for store/keyword mapping |
| `businessdebut_scraper.py` | BusinessDebut.com | Retail store announcements |
| `restaurant_scraper.py` | Restaurant listings | Uses Selenium (headless Chrome) |
| `warn.py` | State WARN Act portals | 5,000+ lines; state-specific handlers |
| `ct_scoop_scraper.py` | CT Scoop | Regional announcements |

### Extraction Pipeline (Claude-Assisted)
1. **`daily_news_prepare.py`** — Batches ~50 articles, writes `newsbatch_N.txt` files with a structured Claude prompt asking for a markdown extraction table.
2. **Manual step** — User pastes each batch into Claude; Claude returns `newsbatch_N_output.md` with a table of store name, location, event type, date, etc.
3. **`build_extraction_masters.py`** — Parses all `newsbatch_*_output.md` files into master CSVs under `data/daily_news/`.

### Data Consolidation & Sync
- **`merge_results.py`** — Merges opening/closing JSON archives into unified datasets.
- **`sync_to_supabase.py`** — Upserts master CSVs to Supabase; deduplicates by `article_link` or `unique_key`.

Run the full daily pipeline:
```powershell
python fetch_banner_store_news.py
python daily_news_prepare.py
# (paste batch files into Claude, save outputs as newsbatch_N_output.md)
python build_extraction_masters.py
python sync_to_supabase.py
```

## Data Architecture

**`analyst.csv`** — Maps 200+ retail store brands to analyst email addresses; used by scrapers to scope which stores to monitor.

**Master CSV schema** (in `data/daily_news/` and `master_file/`):
```
store_name, location, event_type (Opening/Closing/Remodel),
event_date, status, short_description, article_link,
published_date, source_batch, Date_Appended
```

**Local JSON archives** live in `data/store_news/json_archive/` — scrapers write here before sync.

**Batch files**: `newsbatch_N.txt` (Claude input prompts) and `newsbatch_N_output.md` (Claude responses) accumulate in the repo root.

## Frontend (index.html)

Single-file glassmorphic dashboard. Key behaviors:
- Requires Supabase session — page is hidden until auth is verified.
- Fetches data directly from Supabase tables via the JS client.
- AI chat widget calls **Gemini API** (not Claude) from the browser using `GEMINI_API_KEY` embedded at build/deploy time.
- Tabs: Store News, CT Scoop, Restaurant News — each with Articles and Extraction sub-tabs.
- Deployed to both GitHub Pages and Vercel; Vercel config adds 1-hour cache headers on JSON files.

## GitHub Actions Automation

Eight workflows in `.github/workflows/` run on schedule:

| Workflow | Schedule (UTC) | Action |
|----------|---------------|--------|
| Daily banner news | 23:30 daily | `fetch_banner_store_news.py` → sync |
| Daily restaurant | daily | `restaurant_scraper.py` → sync |
| Daily BusinessDebut | daily | `businessdebut_scraper.py` → sync |
| Daily WARN Act | daily | `warn.py` → sync |
| Extraction master build | manual trigger | `build_extraction_masters.py` → sync |

All workflows commit updated CSVs back to `main` and call `sync_to_supabase.py`.

## Common Tasks

**Add a new store to monitor**: Add a row to `analyst.csv` with the store name, keywords, and analyst email. No script changes needed.

**Re-run extraction for a batch**: Delete the relevant `newsbatch_N_output.md`, re-paste the `.txt` into Claude, save the new output, then re-run `build_extraction_masters.py`.

**Trigger a manual Supabase sync**:
```powershell
python sync_to_supabase.py
```

**Test scraper output locally** (without committing):
```powershell
python fetch_banner_store_news.py --dry-run  # check script for flag support
```
