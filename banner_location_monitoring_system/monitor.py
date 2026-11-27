#!/usr/bin/env python3
"""
monitor.py - Banner Location Monitoring System orchestrator.

For every active company:
  1. run its scraper (surya_banner/<module>.py -> scrape())
  2. normalize + validate the result
       - empty / too small / big unexplained drop  -> run marked INCOMPLETE,
         nothing saved, nothing removed (no false removals from a blocked site)
  3. compare with the master list (= previous successful scrape)
  4. save the snapshot, the changes, and update the master list
       (removed stores are marked inactive - never deleted)
  5. export docs/location_monitor.json for the dashboard fallback

Usage
  python monitor.py                       # all active companies -> Supabase
  python monitor.py --company dogtopia    # one company (repeatable)
  python monitor.py --local               # offline: JSON file instead of Supabase
  python monitor.py --from-csv            # reuse surya_banner/*_locations.csv (no scraping)
  python monitor.py --dry-run             # scrape + compare, print, write nothing
  python monitor.py --skip-browser        # skip Cloudflare sites (Hollywood Feed, Mud Bay)
  python monitor.py --force               # accept a scrape that failed validation
  python monitor.py --export-only         # just refresh docs/location_monitor.json

Env: SUPABASE_URL, SUPABASE_KEY (service_role) unless --local.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from companies import COMPANIES, DEFAULT_FREQUENCY  # noqa: E402
from compare import Change, compare, summarize  # noqa: E402
from normalize import normalize_frame  # noqa: E402
from storage import (T_CHANGES, T_COMPANIES, T_LOCATIONS, T_RUNS, T_SNAPSHOTS,  # noqa: E402
                     LocalStore, SupabaseStore, now_iso)

SCRAPER_DIR = HERE / "surya_banner"
LOCAL_DB = HERE / "data" / "local_state.json"
EXPORT_PATH = HERE.parent / "docs" / "location_monitor.json"

MAX_DROP = 0.30          # >30% fewer locations than last time -> incomplete
MIN_COMPLETENESS = 0.80  # share of rows that must have city + state

LOCATION_FIELDS = [
    "location_key", "external_store_id", "store_name", "address", "city", "state",
    "zip_code", "country", "phone", "latitude", "longitude", "status", "store_page",
    "address_key", "name_key", "raw_data",
]
SNAPSHOT_FIELDS = ["store_name", "address", "city", "state", "zip_code", "phone",
                   "latitude", "longitude", "status", "external_store_id"]


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
@contextmanager
def in_dir(path: Path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def load_scraper(module: str):
    path = SCRAPER_DIR / f"{module}.py"
    spec = importlib.util.spec_from_file_location(f"surya_banner_{module}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_scraper(cfg: dict, from_csv: bool) -> pd.DataFrame:
    with in_dir(SCRAPER_DIR):  # scrapers use relative paths (CSV, browser profile)
        mod = load_scraper(cfg["module"])
        if from_csv:
            return pd.read_csv(mod.OUTPUT_CSV, dtype=str, encoding="utf-8-sig").fillna("")
        df = mod.scrape()
    if df is None:
        raise RuntimeError("scrape() returned nothing")
    return df


def validate(records: list[dict], previous: list[dict], cfg: dict) -> list[str]:
    """Return blocking problems. Any problem => scrape is not trusted."""
    n = len(records)
    if n == 0:
        return ["Scrape returned 0 locations (site blocked or layout changed)"]
    problems = []
    if n < cfg.get("min_locations", 1):
        problems.append(f"Only {n} locations found, expected at least {cfg['min_locations']}")
    prev_active = sum(1 for p in previous if p.get("is_active"))
    if prev_active and n < prev_active * (1 - MAX_DROP):
        problems.append(f"Location count dropped from {prev_active} to {n} "
                        f"(more than {int(MAX_DROP * 100)}%) - possible partial scrape")
    complete = sum(1 for r in records if r["city"] and r["state"]) / n
    if complete < MIN_COMPLETENESS:
        problems.append(f"Only {complete:.0%} of locations have city and state")
    return problems


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def sync_companies(store) -> list[dict]:
    """Insert registry entries missing from blms_companies, refresh metadata.
    is_active is owned by the database so it can be toggled without code."""
    existing = {c["company_key"]: c for c in store.select(T_COMPANIES)}
    out = []
    for cfg in COMPANIES:
        meta = {
            "company_name": cfg["company_name"], "website_url": cfg["website_url"],
            "locator_url": cfg["locator_url"], "scraper_module": cfg["module"],
            "scraper_type": cfg["scraper_type"],
        }
        row = existing.get(cfg["key"])
        if row is None:
            row = store.insert(T_COMPANIES, [{
                "company_key": cfg["key"], **meta, "is_active": True,
                "scrape_frequency": DEFAULT_FREQUENCY, "created_at": now_iso(),
            }])[0]
        elif any(row.get(k) != v for k, v in meta.items()):
            store.update(T_COMPANIES, row["id"], meta)
            row.update(meta)
        out.append({**row, "cfg": cfg})
    return out


def location_row(rec: dict) -> dict:
    return {f: rec.get(f) for f in LOCATION_FIELDS}


def change_row(ch: Change, run_id: int, company_id: int, location_id: int, ts: str) -> dict:
    src = ch.current or ch.previous
    return {
        "run_id": run_id, "company_id": company_id, "location_id": location_id,
        "change_type": ch.change_type, "match_method": ch.match_method or None,
        "changed_fields": ch.changed_fields or None,
        "previous_values": ch.previous_values or None,
        "new_values": ch.new_values or None,
        "needs_review": ch.needs_review, "review_reason": ch.review_reason or None,
        "review_status": "pending" if ch.needs_review else None,
        "reappeared": ch.reappeared, "detected_at": ts,
        "store_name": src.get("store_name"), "address": src.get("address"),
        "city": src.get("city"), "state": src.get("state"), "zip_code": src.get("zip_code"),
    }


def apply_changes(store, company_id: int, run_id: int, changes: list[Change],
                  previous: list[dict], baseline: bool) -> None:
    ts = now_iso()
    key_owner = {p["location_key"]: p["id"] for p in previous}

    # 1. brand-new locations -> insert
    new_rows = []
    for ch in changes:
        if ch.change_type == "NEW" and ch.previous is None:
            row = location_row(ch.current)
            if row["location_key"] in key_owner:  # never collide with an old row's key
                row["location_key"] = f"{row['location_key']}#r{run_id}"
            new_rows.append({**row, "company_id": company_id, "is_active": True,
                             "first_seen_at": ts, "last_seen_at": ts,
                             "first_seen_run_id": run_id, "last_seen_run_id": run_id,
                             "updated_at": ts})
    inserted = store.insert(T_LOCATIONS, new_rows) if new_rows else []
    new_ids = iter(r["id"] for r in inserted)

    # 2. matched / removed locations -> full-row upsert by id
    upserts, loc_id_of = [], {}
    for ch in changes:
        if ch.previous is None:
            loc_id_of[id(ch)] = next(new_ids)
            continue
        prev = ch.previous
        loc_id_of[id(ch)] = prev["id"]
        if ch.change_type == "REMOVED":
            upserts.append({**prev, "is_active": False, "removed_at": ts, "updated_at": ts})
            continue
        row = location_row(ch.current)
        if key_owner.get(row["location_key"], prev["id"]) != prev["id"]:
            row["location_key"] = prev["location_key"]
        upserts.append({**prev, **row, "is_active": True, "removed_at": None,
                        "last_seen_at": ts, "last_seen_run_id": run_id, "updated_at": ts})
    if upserts:
        store.upsert(T_LOCATIONS, upserts)

    # 3. snapshot of everything seen in this run
    snaps = [{"run_id": run_id, "company_id": company_id, "location_id": loc_id_of[id(ch)],
              "captured_at": ts, **{f: ch.current.get(f) for f in SNAPSHOT_FIELDS}}
             for ch in changes if ch.current is not None]
    if snaps:
        store.insert(T_SNAPSHOTS, snaps)

    # 4. change log (the baseline run is the starting point - nothing "changed")
    if not baseline:
        rows = [change_row(ch, run_id, company_id, loc_id_of[id(ch)], ts) for ch in changes]
        if rows:
            store.insert(T_CHANGES, rows)


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def process_company(store, company: dict, args) -> dict:
    cfg, cid = company["cfg"], company["id"]
    label = cfg["company_name"]
    print(f"\n=== {label} ({cfg['module']}.py) ===")
    t0 = time.time()

    run_id = None
    if not args.dry_run:
        run_id = store.insert(T_RUNS, [{
            "company_id": cid, "started_at": now_iso(), "status": "running",
            "trigger": args.trigger,
        }])[0]["id"]
        store.save()

    def finish(status: str, **fields):
        print(f"  -> {status.upper()} {fields.get('error_message') or ''}".rstrip())
        if run_id is not None:
            store.update(T_RUNS, run_id, {"status": status, "completed_at": now_iso(),
                                          "duration_seconds": round(time.time() - t0, 1), **fields})
            store.update(T_COMPANIES, cid, {"last_scraped_at": now_iso(), "last_run_status": status})
            store.save()
        return {"company": label, "status": status, **fields}

    try:
        df = run_scraper(cfg, args.from_csv)
    except Exception as exc:  # noqa: BLE001 - any scraper failure = failed run
        traceback.print_exc()
        return finish("failed", error_message=f"{type(exc).__name__}: {exc}"[:1000])

    records, warnings = normalize_frame(df, {**cfg, "company_name": label})
    failed_pages = int(df.attrs.get("failed_pages", 0) or 0)
    if failed_pages:
        warnings.append(f"{failed_pages} store pages failed to load")
    previous = store.select(T_LOCATIONS, company_id=cid)
    print(f"  scraped {len(df)} rows -> {len(records)} locations; master has "
          f"{sum(1 for p in previous if p.get('is_active'))} active / {len(previous)} total")
    for w in warnings[:10]:
        print("  warning:", w)

    problems = validate(records, previous, cfg)
    if problems and not args.force:
        return finish("incomplete", locations_found=len(records),
                      error_message="; ".join(problems), warnings=warnings[:50] or None)

    baseline = not previous
    changes = compare(records, previous, removal_warning=(
        f"{failed_pages} store pages failed to load during this scrape - verify before treating as closed"
        if failed_pages else ""))
    stats = summarize(changes)
    print(f"  {'BASELINE ' if baseline else ''}NEW {stats['NEW']}  REMOVED {stats['REMOVED']}  "
          f"UPDATED {stats['UPDATED']}  UNCHANGED {stats['UNCHANGED']}  review {stats['needs_review']}")
    if args.dry_run:
        for ch in changes:
            if ch.change_type != "UNCHANGED":
                src = ch.current or ch.previous
                print(f"    {ch.change_type:8} {src.get('store_name')} - {src.get('address')}, "
                      f"{src.get('city')} {src.get('state')} {ch.changed_fields or ''}")
        return {"company": label, "status": "dry-run", **stats}

    apply_changes(store, cid, run_id, changes, previous, baseline)
    store.update(T_COMPANIES, cid, {"last_successful_run_id": run_id,
                                    "last_success_at": now_iso()})
    return finish("completed", locations_found=len(records), is_baseline=baseline,
                  new_count=0 if baseline else stats["NEW"],
                  removed_count=stats["REMOVED"], updated_count=stats["UPDATED"],
                  unchanged_count=stats["UNCHANGED"], review_count=stats["needs_review"],
                  warnings=warnings[:50] or None,
                  error_message=("Accepted with --force: " + "; ".join(problems)) if problems else None)


def export_dashboard(store, path: Path) -> None:
    """Static JSON copy of the tables for the dashboard fallback."""
    companies = store.select(T_COMPANIES)
    runs = sorted(store.select(T_RUNS), key=lambda r: r["id"], reverse=True)[:500]
    locations = [{k: v for k, v in r.items() if k not in ("raw_data", "address_key", "name_key")}
                 for r in store.select(T_LOCATIONS)]
    latest = {c.get("last_successful_run_id") for c in companies}
    keep = ("id", "run_id", "company_id", "location_id", "change_type", "detected_at")
    changes = [c for c in store.select(T_CHANGES) if c["change_type"] != "UNCHANGED"]
    # UNCHANGED rows only for each company's latest run, slimmed down - the
    # dashboard fills name/address from `locations` via location_id.
    changes += [{k: c[k] for k in keep} for c in store.select(T_CHANGES)
                if c["change_type"] == "UNCHANGED" and c["run_id"] in latest]
    def compact(rows):  # drop empty values - keeps the file small
        return [{k: v for k, v in r.items() if v not in (None, "", [], {})} for r in rows]

    payload = {"last_updated": now_iso(), "companies": companies, "runs": compact(runs),
               "locations": compact(locations), "changes": compact(changes)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, default=str, separators=(",", ":")), encoding="utf-8")
    print(f"\nExported dashboard JSON -> {path} "
          f"({len(locations)} locations, {len(changes)} changes)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company", action="append", help="company key (repeatable); default all active")
    ap.add_argument("--local", action="store_true", help=f"use {LOCAL_DB.name} instead of Supabase")
    ap.add_argument("--from-csv", action="store_true", help="read existing *_locations.csv, don't scrape")
    ap.add_argument("--dry-run", action="store_true", help="compare and print only, write nothing")
    ap.add_argument("--force", action="store_true", help="accept scrapes that fail validation")
    ap.add_argument("--skip-browser", action="store_true", help="skip companies needing a real browser")
    ap.add_argument("--no-export", action="store_true", help="don't write docs/location_monitor.json")
    ap.add_argument("--export-only", action="store_true", help="only rewrite docs/location_monitor.json")
    ap.add_argument("--trigger", default=os.environ.get("BLMS_TRIGGER", "manual"))
    args = ap.parse_args()

    store = LocalStore(LOCAL_DB) if args.local else SupabaseStore()
    print(f"Store: {store.name}")
    if args.export_only:
        export_dashboard(store, EXPORT_PATH)
        return 0
    companies = sync_companies(store)
    store.save()

    known = {c["company_key"] for c in companies}
    if args.company:
        unknown = set(args.company) - known
        if unknown:
            print(f"Unknown company key(s): {', '.join(sorted(unknown))}\nKnown: {', '.join(sorted(known))}")
            return 2
        companies = [c for c in companies if c["company_key"] in args.company]
    else:
        companies = [c for c in companies if c.get("is_active")]
    if args.skip_browser:
        companies = [c for c in companies if not c["cfg"].get("needs_browser")]

    results = []
    for company in companies:
        try:
            results.append(process_company(store, company, args))
        except Exception as exc:  # noqa: BLE001 - keep going with other companies
            traceback.print_exc()
            results.append({"company": company["company_name"], "status": "error", "error_message": str(exc)})

    if not args.dry_run and not args.no_export:
        export_dashboard(store, EXPORT_PATH)
    store.save()

    print("\n=== Summary ===")
    for r in results:
        extra = (f"new {r.get('new_count', r.get('NEW', 0))}, removed {r.get('removed_count', r.get('REMOVED', 0))}, "
                 f"updated {r.get('updated_count', r.get('UPDATED', 0))}")
        print(f"  {r['company']:28} {r['status']:11} {extra if r['status'] in ('completed', 'dry-run') else r.get('error_message', '')}")
    bad = [r for r in results if r["status"] in ("failed", "error")]
    return 1 if bad and len(bad) == len(results) else 0


if __name__ == "__main__":
    sys.exit(main())
