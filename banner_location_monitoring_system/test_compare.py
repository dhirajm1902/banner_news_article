"""
Tests for the comparison engine + persistence, using the Walmart example
from the project spec.  Run:  python -m pytest test_compare.py  (or plain python)
"""
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare import compare, summarize  # noqa: E402
from monitor import apply_changes  # noqa: E402
from normalize import address_key, normalize_frame  # noqa: E402
from storage import T_CHANGES, T_LOCATIONS, T_SNAPSHOTS, LocalStore  # noqa: E402

CFG = {"company_name": "Walmart", "id_field": "Store ID"}
STORES = {
    "101": ("100 Elm St", "Dallas", "TX", "75001"),
    "102": ("200 Oak St", "Houston", "TX", "77001"),
    "103": ("300 Pine St", "Austin", "TX", "73301"),
    "104": ("400 Main St", "San Antonio", "TX", "78201"),
}


def scrape(*ids, overrides=None):
    rows = []
    for i in ids:
        street, city, state, zip_ = STORES[i]
        row = {"Store ID": i, "Store Name": f"Walmart {city}", "Address": street,
               "City": city, "State": state, "Zip": zip_}
        row.update((overrides or {}).get(i, {}))
        rows.append(row)
    return normalize_frame(pd.DataFrame(rows), CFG)[0]


def run(store, run_id, records):
    previous = store.select(T_LOCATIONS, company_id=1)
    changes = compare(records, previous)
    apply_changes(store, 1, run_id, changes, previous, baseline=not previous)
    by_type = {}
    for c in changes:
        by_type.setdefault(c.change_type, set()).add((c.current or c.previous)["city"])
    return changes, by_type


def test_walmart_three_scrapes():
    store = LocalStore(Path(tempfile.mkdtemp()) / "db.json")

    # 1st scrape - baseline
    changes, t = run(store, 1, scrape("101", "102", "103"))
    assert t == {"NEW": {"Dallas", "Houston", "Austin"}}
    assert store.select(T_CHANGES) == []          # baseline logs no changes

    # 2nd scrape - Austin gone, San Antonio new
    changes, t = run(store, 2, scrape("101", "102", "104"))
    assert t == {"NEW": {"San Antonio"}, "REMOVED": {"Austin"}, "UNCHANGED": {"Dallas", "Houston"}}
    austin = [r for r in store.select(T_LOCATIONS) if r["city"] == "Austin"][0]
    assert austin["is_active"] is False and austin["removed_at"]   # kept, not deleted

    # 3rd scrape - compared with the 2nd, not the 1st
    changes, t = run(store, 3, scrape("101", "104", "103"))
    assert t == {"NEW": {"Austin"}, "REMOVED": {"Houston"}, "UNCHANGED": {"Dallas", "San Antonio"}}
    assert [c for c in changes if c.change_type == "NEW"][0].reappeared
    assert len(store.select(T_LOCATIONS)) == 4                     # no duplicate Austin row
    assert len(store.select(T_SNAPSHOTS)) == 9                     # 3 per run


def test_updated_fields():
    store = LocalStore(Path(tempfile.mkdtemp()) / "db.json")
    run(store, 1, scrape("101"))
    changes, t = run(store, 2, scrape("101", overrides={"101": {"Address": "200 Main St", "Zip": "75002"}}))
    assert t == {"UPDATED": {"Dallas"}}
    ch = changes[0]
    assert ch.changed_fields == ["address", "zip_code"]
    assert ch.previous_values == {"address": "100 Elm St", "zip_code": "75001"}
    assert not ch.needs_review                                     # matched by store ID


def test_address_match_without_ids_and_review_fallback():
    cfg = {"company_name": "Shop", "id_field": None}
    a = normalize_frame(pd.DataFrame([
        {"Store Name": "Shop Dallas", "Address": "100 North Main Street, Suite 4", "City": "Dallas", "State": "Texas", "Zip": "75001-1234"},
        {"Store Name": "Shop Plano", "Address": "9 Elm Rd", "City": "Plano", "State": "TX", "Zip": "75023"},
    ]), cfg)[0]
    b = normalize_frame(pd.DataFrame([
        {"Store Name": "Shop Dallas", "Address": "100 N. Main St Ste 4", "City": "Dallas", "State": "TX", "Zip": "75001"},
        {"Store Name": "Shop Plano", "Address": "55 Oak Ave", "City": "Plano", "State": "TX", "Zip": "75024"},
    ]), cfg)[0]
    store = LocalStore(Path(tempfile.mkdtemp()) / "db.json")
    run(store, 1, a)
    changes, t = run(store, 2, b)
    assert t == {"UNCHANGED": {"Dallas"}, "UPDATED": {"Plano"}}    # same store, address normalized
    plano = [c for c in changes if c.change_type == "UPDATED"][0]
    assert plano.match_method == "name_city_state" and plano.needs_review


def test_mass_removal_flagged():
    prev = [{"id": i, "is_active": True, "external_store_id": str(i), "address_key": f"a{i}",
             "name_key": f"n{i}", "city": "X"} for i in range(10)]
    changes = compare([], prev)
    assert summarize(changes)["REMOVED"] == 10
    assert all(c.needs_review for c in changes)


def test_partial_scrape_is_rejected():
    from monitor import validate
    prev = [{"id": i, "is_active": True} for i in range(100)]
    ok = scrape("101", "102", "103", "104")
    assert validate([], prev, {"min_locations": 1})                 # blocked site -> problem
    assert validate(ok, prev, {"min_locations": 1})                 # 4 vs 100 active -> partial
    assert validate(ok, prev[:4], {"min_locations": 10})            # below minimum
    assert validate(ok, prev[:4], {"min_locations": 1}) == []       # healthy


def test_address_key():
    assert address_key("123 North Main Street, Suite #4", "75001-1234") == address_key("123 N Main St Ste 4", "75001")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
