"""
compare.py - the comparison engine.

Compares the latest scrape of one company with its master location list,
which always reflects the PREVIOUS SUCCESSFUL scrape (active rows) plus every
location ever seen (inactive rows). Pure Python, no database access, so it can
be unit-tested (see test_compare.py).

Matching priority (spec section 4):
  1. official store ID              -> confident
  2. normalized address + ZIP       -> confident
  3. store name + city + state      -> fallback, flagged for manual review

Change types:
  NEW        location appears for the first time, or reappears after removal
  REMOVED    previously active location is missing from the latest scrape
  UPDATED    matched location whose tracked fields changed
  UNCHANGED  matched location, no meaningful differences
"""
from __future__ import annotations

from dataclasses import dataclass, field

from normalize import address_key

# Fields whose change makes a location UPDATED. Hours / amenities live in
# raw_data and are deliberately NOT compared (they change too often).
TRACKED_FIELDS = [
    "store_name", "address", "city", "state", "zip_code", "phone", "status",
    "latitude", "longitude", "external_store_id",
]
COORD_TOLERANCE = 0.001  # ~100 m; smaller moves are geocoder noise

# Flag REMOVED rows for review when a run removes at least this many / share.
MASS_REMOVAL_MIN = 5
MASS_REMOVAL_SHARE = 0.10


@dataclass
class Change:
    change_type: str                    # NEW / REMOVED / UPDATED / UNCHANGED
    current: dict | None                # normalized scraped record (None for REMOVED)
    previous: dict | None               # master row (None for brand-new)
    match_method: str = ""              # store_id / address / name_city_state / ""
    changed_fields: list[str] = field(default_factory=list)
    previous_values: dict = field(default_factory=dict)
    new_values: dict = field(default_factory=dict)
    needs_review: bool = False
    review_reason: str = ""
    reappeared: bool = False


def _norm(v):
    if v is None:
        return ""
    return str(v).strip().lower()


def diff_fields(prev: dict, cur: dict) -> list[str]:
    changed = []
    for f in TRACKED_FIELDS:
        a, b = prev.get(f), cur.get(f)
        if f in ("latitude", "longitude"):
            if a is None or b is None or a == "" or b == "":
                continue  # missing coords on one side is not a move
            try:
                if abs(float(a) - float(b)) > COORD_TOLERANCE:
                    changed.append(f)
            except (TypeError, ValueError):
                pass
            continue
        if f == "external_store_id" and not _norm(a):
            continue  # gaining an ID is not a meaningful change
        if f == "address":
            # "100 North Main Street" vs "100 N. Main St" is formatting, not a move
            a, b = address_key(a or "", ""), address_key(b or "", "")
        if _norm(a) != _norm(b):
            changed.append(f)
    return changed


def _snapshot(rec: dict) -> dict:
    return {f: rec.get(f) for f in TRACKED_FIELDS}


def compare(current: list[dict], previous: list[dict], *, removal_warning: str = "") -> list[Change]:
    """
    current  - normalized records from the latest scrape (normalize.py)
    previous - master rows for the same company: dicts with at least id,
               is_active, external_store_id, address_key, name_key + TRACKED_FIELDS
    removal_warning - if set (e.g. "3 store pages failed to load"), every
               REMOVED change is flagged for manual review with this reason.
    """
    by_id = {p["external_store_id"]: p for p in previous if p.get("external_store_id")}
    by_addr = {}
    for p in previous:
        if p.get("address_key"):
            # prefer the active row when an address was used twice
            if p["address_key"] not in by_addr or p.get("is_active"):
                by_addr[p["address_key"]] = p

    used: set = set()           # ids of previous rows already matched
    matches: list[tuple[dict, dict | None, str]] = []
    unmatched: list[dict] = []

    # Pass 1 + 2: confident matches (store ID, then address)
    for cur in current:
        prev, method = None, ""
        cand = by_id.get(cur.get("external_store_id")) if cur.get("external_store_id") else None
        if cand is not None and cand["id"] not in used:
            prev, method = cand, "store_id"
        if prev is None and cur.get("address_key"):
            cand = by_addr.get(cur["address_key"])
            # Don't steal a row that carries a *different* official ID.
            if (cand is not None and cand["id"] not in used
                    and not (cur.get("external_store_id") and cand.get("external_store_id")
                             and cand["external_store_id"] != cur["external_store_id"])):
                prev, method = cand, "address"
        if prev is None:
            unmatched.append(cur)
        else:
            used.add(prev["id"])
            matches.append((cur, prev, method))

    # Pass 3: name + city + state fallback between leftovers on both sides.
    # Only a one-to-one pairing is accepted; it is always sent to review
    # because it usually means "address changed" (spec: a changed address
    # should not automatically become a new store).
    leftovers_prev = [p for p in previous if p["id"] not in used and p.get("name_key")]
    prev_by_name: dict[str, list[dict]] = {}
    for p in leftovers_prev:
        prev_by_name.setdefault(p["name_key"], []).append(p)
    cur_name_count: dict[str, int] = {}
    for c in unmatched:
        cur_name_count[c.get("name_key", "")] = cur_name_count.get(c.get("name_key", ""), 0) + 1

    still_unmatched = []
    for cur in unmatched:
        nk = cur.get("name_key", "")
        cands = prev_by_name.get(nk, [])
        cands = [p for p in cands if p["id"] not in used]
        if nk and len(cands) == 1 and cur_name_count.get(nk) == 1:
            used.add(cands[0]["id"])
            matches.append((cur, cands[0], "name_city_state"))
        else:
            still_unmatched.append(cur)

    changes: list[Change] = []
    for cur, prev, method in matches:
        changed = diff_fields(prev, cur)
        if not prev.get("is_active"):
            # Reappeared after being removed -> NEW again
            changes.append(Change(
                "NEW", cur, prev, method, changed,
                _snapshot(prev) if changed else {}, _snapshot(cur),
                needs_review=(method == "name_city_state"),
                review_reason="Matched to a removed location by name/city/state only" if method == "name_city_state" else "",
                reappeared=True,
            ))
        elif changed:
            review = method == "name_city_state"
            changes.append(Change(
                "UPDATED", cur, prev, method, changed,
                {f: prev.get(f) for f in changed}, {f: cur.get(f) for f in changed},
                needs_review=review,
                review_reason="Possible address change - matched by name/city/state only" if review else "",
            ))
        else:
            changes.append(Change("UNCHANGED", cur, prev, method))

    for cur in still_unmatched:
        changes.append(Change("NEW", cur, None, "", new_values=_snapshot(cur)))

    removed = [p for p in previous if p.get("is_active") and p["id"] not in used]
    active_before = sum(1 for p in previous if p.get("is_active"))
    mass = (len(removed) >= MASS_REMOVAL_MIN
            and active_before and len(removed) / active_before >= MASS_REMOVAL_SHARE)
    for p in removed:
        reason = removal_warning or (
            f"{len(removed)} of {active_before} locations removed in one run - possible false removal"
            if mass else "")
        changes.append(Change(
            "REMOVED", None, p, "", previous_values=_snapshot(p),
            needs_review=bool(reason), review_reason=reason,
        ))
    return changes


def summarize(changes: list[Change]) -> dict:
    out = {"NEW": 0, "REMOVED": 0, "UPDATED": 0, "UNCHANGED": 0, "needs_review": 0}
    for c in changes:
        out[c.change_type] += 1
        out["needs_review"] += int(c.needs_review)
    return out
