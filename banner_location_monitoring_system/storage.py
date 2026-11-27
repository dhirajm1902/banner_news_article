"""
storage.py - two interchangeable backends with the same tiny interface.

  SupabaseStore  production; tables from schema.sql (blms_* prefix)
  LocalStore     a single JSON file - lets you run/test the whole pipeline
                 offline (python monitor.py --local)

Interface: select(table, **eq), insert(table, rows) -> rows with ids,
upsert(table, rows) (rows carry id), update(table, id, fields).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

T_COMPANIES = "blms_companies"
T_RUNS = "blms_scrape_runs"
T_LOCATIONS = "blms_store_locations"
T_SNAPSHOTS = "blms_location_snapshots"
T_CHANGES = "blms_location_changes"
ALL_TABLES = [T_COMPANIES, T_RUNS, T_LOCATIONS, T_SNAPSHOTS, T_CHANGES]

CHUNK = 500
PAGE = 1000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SupabaseStore:
    name = "supabase"

    def __init__(self, url: str | None = None, key: str | None = None):
        from supabase import create_client
        # BLMS lives in its own Supabase project; BLMS_* wins so it never
        # mixes with the SUPABASE_* secrets used by the other scrapers.
        url = url or os.environ.get("BLMS_SUPABASE_URL") or os.environ.get("SUPABASE_URL", "")
        key = key or os.environ.get("BLMS_SUPABASE_KEY") or os.environ.get("SUPABASE_KEY", "")
        if not url or not key:
            raise RuntimeError("Set BLMS_SUPABASE_URL and BLMS_SUPABASE_KEY (service_role key), or use --local")
        self.sb = create_client(url, key)

    def select(self, table: str, **eq) -> list[dict]:
        rows, start = [], 0
        while True:
            q = self.sb.table(table).select("*")
            for k, v in eq.items():
                q = q.eq(k, v)
            data = q.order("id").range(start, start + PAGE - 1).execute().data or []
            rows.extend(data)
            if len(data) < PAGE:
                return rows
            start += PAGE

    def insert(self, table: str, rows: list[dict]) -> list[dict]:
        out = []
        for i in range(0, len(rows), CHUNK):
            out.extend(self.sb.table(table).insert(rows[i:i + CHUNK]).execute().data or [])
        return out

    def upsert(self, table: str, rows: list[dict]) -> None:
        for i in range(0, len(rows), CHUNK):
            self.sb.table(table).upsert(rows[i:i + CHUNK], on_conflict="id").execute()

    def update(self, table: str, row_id: int, fields: dict) -> None:
        self.sb.table(table).update(fields).eq("id", row_id).execute()

    def save(self) -> None:
        pass


class LocalStore:
    name = "local"

    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            self.db = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.db = {t: [] for t in ALL_TABLES}
            self.db["_seq"] = {t: 0 for t in ALL_TABLES}

    def select(self, table: str, **eq) -> list[dict]:
        return [dict(r) for r in self.db[table] if all(r.get(k) == v for k, v in eq.items())]

    def insert(self, table: str, rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            self.db["_seq"][table] += 1
            row = {"id": self.db["_seq"][table], **r}
            self.db[table].append(row)
            out.append(dict(row))
        return out

    def upsert(self, table: str, rows: list[dict]) -> None:
        index = {r["id"]: i for i, r in enumerate(self.db[table])}
        for r in rows:
            if r["id"] in index:
                self.db[table][index[r["id"]]].update(r)
            else:
                self.db[table].append(dict(r))

    def update(self, table: str, row_id: int, fields: dict) -> None:
        for r in self.db[table]:
            if r["id"] == row_id:
                r.update(fields)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.db, indent=1, default=str), encoding="utf-8")
