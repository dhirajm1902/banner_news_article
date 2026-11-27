# Banner Location Monitoring System

Watches company store locators every week and records which locations are
**NEW**, **REMOVED**, **UPDATED** or **UNCHANGED** compared with the previous
successful scrape. Results show up in the dashboard tab **Banner Location Monitoring**
(`index.html`).

```
surya_banner/*.py ──scrape()──▶ normalize.py ──▶ validate ──▶ compare.py ──▶ Supabase (blms_*)
   (11 company scrapers)         one schema      blocked/partial   NEW/REMOVED/      └▶ docs/location_monitor.json
                                                 scrape = stop     UPDATED/UNCHANGED      (dashboard fallback)
```

## Files

| File | Role |
|------|------|
| `surya_banner/*.py` | One scraper per company. Each has `scrape() -> DataFrame` (used by the monitor) and `main()` (writes its own CSV, as before). |
| `companies.py` | Registry: company name, scraper module, store-ID column, minimum expected locations. |
| `normalize.py` | Maps every scraper's columns to one schema; normalizes state, ZIP, phone, status, and address (`North Main Street` = `N Main St`). |
| `compare.py` | Comparison engine. Pure Python, no DB. |
| `storage.py` | `SupabaseStore` (production) and `LocalStore` (JSON file, offline testing). |
| `monitor.py` | Orchestrator / CLI. |
| `schema.sql` | Supabase tables + RLS. Run once. |
| `test_compare.py` | Tests, including the 3-scrape Walmart example from the spec. |

## How a run works

1. **Scrape** each active company (`blms_companies.is_active`).
2. **Validate.** If any check fails, the run is marked `incomplete`, **nothing is saved and nothing is removed**, and it shows in *Manual Review*:
   - 0 locations, or fewer than `min_locations`
   - more than 30% fewer locations than the last successful scrape
   - under 80% of rows have city + state
3. **Compare** with the master list. Matching priority:
   1. company + official store ID (`Store ID` / `Store Code`; the store-page URL where a site has no ID)
   2. company + normalized address + ZIP
   3. company + name + city + state. This is a fallback, so the match is always flagged for **manual review** (usually an address change, not a new store).
4. **Save**: snapshot of every location seen, one change row per location, master list updated.
   Removed stores are set `is_active = false` and are never deleted. If a store comes back, it is logged as NEW with `reappeared = true`.
5. **Flag for review**:
   - name-only matches
   - mass removals (5 or more stores, and at least 10% of the company's stores)
   - removals in a run where store pages failed to load (Hollywood Feed)

The first successful run of a company is the **baseline**. Its locations are stored but no changes are logged.
UPDATED compares name, address, city, state, ZIP, phone, status, coordinates (>~100 m) and store ID.
Hours and amenities are kept in `raw_data` but not compared, because they change too often.

## Setup (separate Supabase project)

1. Create a Supabase project, then run `schema.sql` in its SQL Editor.
2. Project Settings → API: copy the **Project URL**, **anon** key and **service_role** key.
3. GitHub → repo Settings → Secrets and variables → Actions: add
   `BLMS_SUPABASE_URL` (Project URL) and `BLMS_SUPABASE_KEY` (service_role key).
4. In `index.html`, set `LM_SUPA_URL` and `LM_SUPA_ANON` (anon key only - never service_role).
5. Optional, to get changes on the very first live run: seed the baseline from the CSVs in `surya_banner/`:
   ```powershell
   $env:BLMS_SUPABASE_URL="..."; $env:BLMS_SUPABASE_KEY="..."
   python banner_location_monitoring_system/monitor.py --from-csv
   ```
6. `.github/workflows/weekly-location-monitor.yml` runs every Monday 03:00 UTC (or manually from the Actions tab).

Manual Review is view-only by default (resolve items in Table Editor → `blms_location_changes`).
To enable Confirm/Reject buttons, run the OPTIONAL block at the end of `schema.sql` and set
`LM_REVIEW_WRITABLE = true` in `index.html`.

## Commands

```powershell
python monitor.py                      # all active companies -> Supabase
python monitor.py --company dogtopia   # one company (repeatable)
python monitor.py --dry-run            # scrape + compare, print changes, write nothing
python monitor.py --local              # use data/local_state.json instead of Supabase
python monitor.py --from-csv           # use the existing *_locations.csv instead of scraping
python monitor.py --skip-browser       # skip Hollywood Feed + Mud Bay (need a real browser)
python monitor.py --force              # accept a scrape that failed validation
python monitor.py --export-only        # just rewrite docs/location_monitor.json
python test_compare.py                 # run the tests
```

## Adding a company

1. Put a scraper in `surya_banner/` with a `scrape()` function that returns a DataFrame.
   Use the usual column names: `Store ID`, `Store Name`, `Address`, `City`, `State`, `Zip`, `Phone`, `Latitude`, `Longitude`, `Status`, `Store Page`.
   Other names are mapped in `normalize.COLUMN_MAP`.
2. Add one entry to `companies.py`, with `id_field` and `min_locations` set.

To pause a company without changing code, set `is_active = false` in `blms_companies`.

## Dashboard tab

The tab has these views: Dashboard (stat cards, change feed, per-company summary), Companies, All Locations,
New, Removed, Updated (previous → current values per field), Scrape History and Manual Review.
In Manual Review, analysts can **Confirm** or **Reject** a flagged item. Only the review columns are writable by logged-in users.
The page reads the `blms_*` tables through the logged-in Supabase session. If they aren't there yet, it reads
`docs/location_monitor.json` instead.
