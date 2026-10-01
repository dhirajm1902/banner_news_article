"""
Minimal demo backend for the retail news schema (master_events_schema_v3_clean.sql).
SQLite instead of Postgres on purpose -- zero setup, no pgAdmin/local Postgres
needed. Same table/column names as the real schema so wiring this up to
Supabase later is a find-and-replace, not a rewrite.

Run: python app.py   (creates demo.db + seed data next to this file on first run)
"""

import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "demo.db")

COMPLETION_STATUSES = {
    "Add", "Edit", "Already Updated", "Not Relevant",
    "Not Accessible", "Send to the Calling Team",
}

SCHEMA_SQL = """
CREATE TABLE companies (
    company_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name    TEXT UNIQUE NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE analysts (
    analyst_id      TEXT PRIMARY KEY,
    analyst_name    TEXT NOT NULL,
    email           TEXT UNIQUE,
    role            TEXT NOT NULL DEFAULT 'analyst' CHECK (role IN ('analyst', 'admin')),
    password_hash   TEXT NOT NULL,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE event_types (
    event_type_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL
);

CREATE TABLE observation_statuses (
    status_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type_id   INTEGER NOT NULL REFERENCES event_types(event_type_id),
    label           TEXT NOT NULL,
    UNIQUE (event_type_id, label)
);

CREATE TABLE event_reasons (
    reason_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT UNIQUE NOT NULL
);

CREATE TABLE store_events (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT NOT NULL,
    article_link            TEXT NOT NULL,
    published_date          TEXT,
    source_batch            TEXT,
    company_name            TEXT REFERENCES companies(company_name),
    store_name              TEXT,
    event_type_id           INTEGER REFERENCES event_types(event_type_id),
    observation_status_id   INTEGER REFERENCES observation_statuses(status_id),
    event_date_raw          TEXT,
    event_date              TEXT,
    reason_id                INTEGER REFERENCES event_reasons(reason_id),
    address_line1            TEXT,
    city                     TEXT,
    state                    TEXT,
    zip_code                 TEXT,
    county                   TEXT,
    comment                  TEXT,
    date_appended            TEXT DEFAULT (date('now')),
    entered_by               TEXT REFERENCES analysts(analyst_id)
);

CREATE TABLE scraped_articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    link            TEXT,
    title           TEXT,
    published_date  TEXT,
    company_name    TEXT REFERENCES companies(company_name),
    address         TEXT,
    summary         TEXT,
    state           TEXT,
    city            TEXT,
    extra_data      TEXT,
    date_appended   TEXT DEFAULT (date('now')),
    UNIQUE (source, link)
);

CREATE TABLE article_marks (
    article_key       TEXT PRIMARY KEY,
    company_name      TEXT REFERENCES companies(company_name),
    is_done           INTEGER DEFAULT 0,
    marked_by         TEXT REFERENCES analysts(analyst_id),
    marked_at         TEXT,
    assigned_to       TEXT REFERENCES analysts(analyst_id),
    assigned_by       TEXT REFERENCES analysts(analyst_id),
    assigned_at       TEXT,
    completion_status TEXT CHECK (completion_status IS NULL OR completion_status IN (
                            'Add', 'Edit', 'Already Updated', 'Not Relevant',
                            'Not Accessible', 'Send to the Calling Team'
                        ))
);
"""


def seed(cur):
    companies = [
        "Aldi", "Whole Foods Market", "TGI Fridays", "Grande Depot",
        "Commissary", "Tango Room", "Muchacho Tex-Mex", "Party City",
        "CVS Pharmacy", "Trader Joe's", "Target",
    ]
    cur.executemany(
        "INSERT INTO companies (company_name) VALUES (?)",
        [(c,) for c in companies],
    )

    cur.executemany(
        "INSERT INTO analysts (analyst_id, analyst_name, email, role, password_hash) VALUES (?,?,?,?,?)",
        [
            ("0000", "Admin", "admin@retailstat.com", "admin", generate_password_hash("admin123")),
            ("0001", "Parth Chudasama", "parthc@retailstat.com", "analyst", generate_password_hash("analyst123")),
            ("0002", "Alex Rivera", "alex.rivera@retailstat.com", "analyst", generate_password_hash("analyst123")),
        ],
    )

    cur.executemany(
        "INSERT INTO event_types (name) VALUES (?)",
        [("Opening",), ("Closing",), ("Remodel",)],
    )

    status_rows = [
        (1, "planned opening"), (1, "opening soon"), (1, "set to open"),
        (1, "under construction"), (1, "opened"), (1, "grand opening"),
        (2, "planned closing"), (2, "closing soon"), (2, "set to close"),
        (2, "closed"), (2, "permanently closed"), (2, "shut down"),
        (3, "under renovation"), (3, "remodeling"),
        (3, "renovation planned"), (3, "reopened after remodel"),
    ]
    cur.executemany(
        "INSERT INTO observation_statuses (event_type_id, label) VALUES (?,?)",
        status_rows,
    )

    reasons = [
        "Business Closing", "Store Closing", "Chain Closing",
        "Restaurant Closing", "Facility Closing", "DIP/Leasing Rejection",
        "Rebranding", "Relocating", "Temporary", "Mass Closing",
    ]
    cur.executemany(
        "INSERT INTO event_reasons (label) VALUES (?)",
        [(r,) for r in reasons],
    )

    def status_id(label):
        return cur.execute(
            "SELECT status_id FROM observation_statuses WHERE label=?", (label,)
        ).fetchone()[0]

    def reason_id(label):
        return cur.execute(
            "SELECT reason_id FROM event_reasons WHERE label=?", (label,)
        ).fetchone()[0]

    events = [
        dict(source="daily_news",
             article_link="https://patch.com/massachusetts/worcester/amp/34548336/longtime-restaurant-chain-closes-central-ma-location",
             published_date="2026-07-28 21:39:11", company_name="TGI Fridays",
             event_type_id=2, observation_status_id=status_id("closed"),
             event_date_raw="Not specified", city="Millbury", state="MA",
             reason_id=reason_id("Restaurant Closing"),
             comment="2026-07-28, According to source - The TGI Fridays at 70 Worcester-Providence Turnpike in Millbury, MA closed after the location's lease term, leaving 3 locations in the state. The chain filed for Chapter 11 bankruptcy in November 2024.",
             entered_by="0001"),
        dict(source="daily_news",
             article_link="https://www.stcloudlive.com/news/grande-depot-announces-permanent-closure-as-of-friday-july-31",
             published_date="2026-07-28 20:10:00", company_name="Grande Depot",
             event_type_id=2, observation_status_id=status_id("permanently closed"),
             event_date_raw="Friday, July 31", reason_id=reason_id("Store Closing"),
             comment="2026-07-28, According to source - Grande Depot announced a permanent closure effective Friday, July 31.",
             entered_by="0002"),
        dict(source="restaurant",
             article_link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             published_date="2026-07-28 21:45:00", company_name="Commissary",
             store_name="Commissary", event_type_id=1, observation_status_id=status_id("planned opening"),
             event_date_raw="Fall 2026", address_line1="3101 Gaylord Pkwy", city="Frisco", state="TX",
             comment="2026-07-28, According to source - Commissary is opening at Hall Park in Frisco in fall 2026, its first expansion north for Headington Companies.",
             entered_by="0001"),
        dict(source="restaurant",
             article_link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             published_date="2026-07-28 21:45:00", company_name="Tango Room",
             store_name="Tango Room", event_type_id=1, observation_status_id=status_id("planned opening"),
             event_date_raw="Fall 2026", address_line1="3101 Gaylord Pkwy", city="Frisco", state="TX",
             comment="2026-07-28, According to source - Tango Room is opening alongside Commissary at Hall Park in Frisco in fall 2026.",
             entered_by="0001"),
        dict(source="restaurant",
             article_link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             published_date="2026-07-28 21:45:00", company_name="Muchacho Tex-Mex",
             store_name="Muchacho Tex-Mex", event_type_id=1, observation_status_id=status_id("set to open"),
             event_date_raw="Fall 2026", city="Frisco", state="TX",
             comment="2026-07-28, According to source - Muchacho Tex-Mex will open in Frisco in fall 2026.",
             entered_by="0002"),
        dict(source="banner",
             article_link="https://example.com/aldi-opens-third-location",
             published_date="2026-07-20 10:00:00", company_name="Aldi",
             event_type_id=1, observation_status_id=status_id("grand opening"),
             event_date_raw="Aug 15, 2026", city="Naperville", state="IL",
             comment="2026-07-20, According to source - Aldi is holding a grand opening for its third Naperville location on Aug 15, 2026.",
             entered_by="0001"),
        dict(source="banner",
             article_link="https://example.com/whole-foods-remodel-uptown",
             published_date="2026-07-18 09:30:00", company_name="Whole Foods Market",
             event_type_id=3, observation_status_id=status_id("under renovation"),
             event_date_raw="Not specified", city="Denver", state="CO",
             comment="2026-07-18, According to source - The Whole Foods Market in Uptown Denver is under renovation.",
             entered_by="0002"),
        dict(source="ct_scoop",
             article_link="https://ctscoop.example.com/party-city-hartford-closing",
             published_date="2026-07-22 12:00:00", company_name="Party City",
             event_type_id=2, observation_status_id=status_id("closing soon"),
             event_date_raw="End of August 2026", city="Hartford", state="CT",
             reason_id=reason_id("Chain Closing"),
             comment="2026-07-22, According to source - The Party City in Hartford is closing soon as part of a wider chain contraction.",
             entered_by="0001"),
        dict(source="daily_news_bankruptcy",
             article_link="https://example.com/cvs-pharmacy-closures-2026",
             published_date="2026-07-25 08:00:00", company_name="CVS Pharmacy",
             event_type_id=2, observation_status_id=status_id("set to close"),
             event_date_raw="Q4 2026", reason_id=reason_id("Mass Closing"),
             comment="2026-07-25, According to source - CVS Pharmacy is set to close a number of locations in Q4 2026 as part of a mass closing plan.",
             entered_by="0002"),
        dict(source="businessdebut",
             article_link="https://businessdebut.example.com/trader-joes-new-store-austin",
             published_date="2026-07-15 14:00:00", company_name="Trader Joe's",
             event_type_id=1, observation_status_id=status_id("opening soon"),
             event_date_raw="September 2026", city="Austin", state="TX",
             comment="2026-07-15, According to source - Trader Joe's is opening a new store in Austin in September 2026.",
             entered_by="0001"),
    ]

    for e in events:
        cols = ", ".join(e.keys())
        placeholders = ", ".join("?" for _ in e)
        cur.execute(
            f"INSERT INTO store_events ({cols}) VALUES ({placeholders})",
            list(e.values()),
        )

    raw_rows = [
        dict(source="banner", link="https://example.com/aldi-opens-third-location",
             title="Aldi opens 3rd Naperville location", published_date="2026-07-20",
             company_name="Aldi", summary="Grand opening announced for Aug 15, 2026.",
             extra_data='{"analyst": "J. Smith", "industry": "Grocery", "type": "Opening"}'),
        dict(source="warn", link=None, title=None, published_date="2026-07-25",
             company_name="CVS Pharmacy", state="CA", city="Fresno",
             extra_data='{"layoff_date": "2026-08-01", "employees_affected": 45, "closure_type": "Facility Closing"}'),
        dict(source="bizjournals", link="https://bizjournals.example.com/target-new-format",
             title="Target tests new small-format store", published_date="2026-07-10",
             company_name="Target", summary="Og description text...",
             extra_data='{"full_text": "...", "jsonld_name": "Target"}'),
        dict(source="company_website", link="https://traderjoes.example.com/coming-soon/austin",
             title="Coming Soon - Austin", published_date="2026-07-15",
             company_name="Trader Joe's",
             extra_data='{"opening_date": "2026-09-01", "is_new": true}'),
        dict(source="ct_scoop", link="https://ctscoop.example.com/party-city-hartford-closing",
             title="Party City Hartford closing soon", published_date="2026-07-22",
             company_name="Party City", summary="Chain contraction continues in CT."),
        dict(source="restaurant", link="https://dallas.culturemap.com/news/restaurants-bars/new-restaurants-opening-frisco/",
             title="8 high-profile Dallas restaurants expanding to Frisco", published_date="2026-07-28",
             company_name="Commissary", summary="Eight Dallas restaurants are expanding to Frisco."),
    ]
    for r in raw_rows:
        cols = ", ".join(r.keys())
        placeholders = ", ".join("?" for _ in r)
        cur.execute(
            f"INSERT INTO scraped_articles ({cols}) VALUES ({placeholders})",
            list(r.values()),
        )

    now = datetime.now(timezone.utc).isoformat()
    cur.executemany(
        "INSERT INTO article_marks (article_key, company_name, is_done, marked_by, marked_at) VALUES (?,?,?,?,?)",
        [
            ("https://www.stcloudlive.com/news/grande-depot-announces-permanent-closure-as-of-friday-july-31",
             "Grande Depot", 1, "0002", now),
            ("https://example.com/aldi-opens-third-location", "Aldi", 1, "0001", now),
        ],
    )


def init_db():
    is_new = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    if is_new:
        cur = conn.cursor()
        cur.executescript(SCHEMA_SQL)
        seed(cur)
        conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


app = Flask(__name__)


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/summary")
def summary():
    db = get_db()
    by_type = db.execute(
        """SELECT et.name AS event_type, COUNT(*) AS cnt
           FROM store_events se JOIN event_types et ON se.event_type_id = et.event_type_id
           GROUP BY et.name"""
    ).fetchall()
    by_source = db.execute(
        "SELECT source, COUNT(*) AS cnt FROM store_events GROUP BY source"
    ).fetchall()
    total_events = db.execute("SELECT COUNT(*) AS c FROM store_events").fetchone()["c"]
    total_raw = db.execute("SELECT COUNT(*) AS c FROM scraped_articles").fetchone()["c"]
    total_companies = db.execute("SELECT COUNT(*) AS c FROM companies").fetchone()["c"]
    return jsonify({
        "total_events": total_events,
        "total_raw_articles": total_raw,
        "total_companies": total_companies,
        "by_event_type": {r["event_type"]: r["cnt"] for r in by_type},
        "by_source": {r["source"]: r["cnt"] for r in by_source},
    })


@app.route("/api/store_events")
def list_store_events():
    source = request.args.get("source")
    db = get_db()
    query = """
        SELECT se.*, et.name AS event_type_name, os.label AS status_label, er.label AS reason_label
        FROM store_events se
        LEFT JOIN event_types et ON se.event_type_id = et.event_type_id
        LEFT JOIN observation_statuses os ON se.observation_status_id = os.status_id
        LEFT JOIN event_reasons er ON se.reason_id = er.reason_id
    """
    params = []
    if source:
        query += " WHERE se.source = ?"
        params.append(source)
    query += " ORDER BY se.event_id DESC"
    rows = [dict(r) for r in db.execute(query, params).fetchall()]
    return jsonify(rows)


@app.route("/api/scraped_articles")
def list_scraped_articles():
    source = request.args.get("source")
    db = get_db()
    query = "SELECT * FROM scraped_articles"
    params = []
    if source:
        query += " WHERE source = ?"
        params.append(source)
    query += " ORDER BY id DESC"
    rows = [dict(r) for r in db.execute(query, params).fetchall()]
    return jsonify(rows)


@app.route("/api/companies")
def list_companies():
    db = get_db()
    rows = [dict(r) for r in db.execute("SELECT * FROM companies ORDER BY company_name").fetchall()]
    return jsonify(rows)


@app.route("/api/analysts")
def list_analysts():
    db = get_db()
    rows = [dict(r) for r in db.execute(
        "SELECT analyst_id, analyst_name, email, role, created_at FROM analysts ORDER BY analyst_id"
    ).fetchall()]
    return jsonify(rows)


@app.route("/api/login", methods=["POST", "OPTIONS"])
def login():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(force=True) or {}
    identifier = (data.get("identifier") or "").strip()
    password = data.get("password") or ""
    if not identifier or not password:
        return jsonify({"error": "identifier and password are required"}), 400

    db = get_db()
    row = db.execute(
        "SELECT * FROM analysts WHERE analyst_id = ? OR lower(email) = lower(?)",
        (identifier, identifier),
    ).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "invalid credentials"}), 401

    return jsonify({
        "analyst_id": row["analyst_id"],
        "analyst_name": row["analyst_name"],
        "email": row["email"],
        "role": row["role"],
    })


@app.route("/api/article_marks", methods=["GET", "POST", "OPTIONS"])
def article_marks():
    if request.method == "OPTIONS":
        return ("", 204)

    db = get_db()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        article_key = data.get("article_key")
        if not article_key:
            return jsonify({"error": "article_key is required"}), 400
        company_name = data.get("company_name")
        completion_status = (data.get("completion_status") or "").strip() or None

        if completion_status:
            if completion_status not in COMPLETION_STATUSES:
                return jsonify({"error": f"invalid completion_status: {completion_status}"}), 400
            is_done = 1
            marked_by = data.get("marked_by")
            marked_at = datetime.now(timezone.utc).isoformat()
        else:
            # Clearing a status back to blank ("reset"): an admin can reset
            # anyone's status; an analyst can only reset a status they
            # themselves set.
            actor_id = data.get("actor_analyst_id")
            actor = db.execute("SELECT * FROM analysts WHERE analyst_id = ?", (actor_id,)).fetchone()
            if not actor:
                return jsonify({"error": "unknown actor_analyst_id"}), 401
            if actor["role"] != "admin":
                existing = db.execute(
                    "SELECT marked_by FROM article_marks WHERE article_key = ?", (article_key,)
                ).fetchone()
                if not existing or existing["marked_by"] != actor_id:
                    return jsonify({"error": "you can only uncheck a status you set yourself"}), 403
            is_done = 0
            marked_by = None
            marked_at = None

        db.execute(
            """INSERT INTO article_marks
               (article_key, company_name, is_done, marked_by, marked_at, completion_status)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(article_key) DO UPDATE SET
                   company_name = excluded.company_name,
                   is_done = excluded.is_done,
                   marked_by = excluded.marked_by,
                   marked_at = excluded.marked_at,
                   completion_status = excluded.completion_status""",
            (article_key, company_name, is_done, marked_by, marked_at, completion_status),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM article_marks WHERE article_key = ?", (article_key,)
        ).fetchone()
        return jsonify(dict(row))

    rows = [dict(r) for r in db.execute("SELECT * FROM article_marks").fetchall()]
    return jsonify(rows)


@app.route("/api/article_assignments", methods=["POST", "OPTIONS"])
def article_assignments():
    if request.method == "OPTIONS":
        return ("", 204)

    db = get_db()
    data = request.get_json(force=True) or {}
    article_key = data.get("article_key")
    actor_id = data.get("actor_analyst_id")
    assigned_to = data.get("assigned_to") or None
    company_name = data.get("company_name")

    if not article_key or not actor_id:
        return jsonify({"error": "article_key and actor_analyst_id are required"}), 400

    actor = db.execute("SELECT * FROM analysts WHERE analyst_id = ?", (actor_id,)).fetchone()
    if not actor:
        return jsonify({"error": "unknown actor_analyst_id"}), 401

    # Non-admins may only assign an article to themselves, or unassign an
    # article that is currently assigned to them.
    if actor["role"] != "admin":
        if assigned_to not in (None, actor_id):
            return jsonify({"error": "analysts may only assign articles to themselves"}), 403
        if assigned_to is None:
            current = db.execute(
                "SELECT assigned_to FROM article_marks WHERE article_key = ?", (article_key,)
            ).fetchone()
            if current and current["assigned_to"] not in (None, actor_id):
                return jsonify({"error": "analysts may only unassign their own assignments"}), 403

    assigned_at = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO article_marks (article_key, company_name, assigned_to, assigned_by, assigned_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(article_key) DO UPDATE SET
               company_name = excluded.company_name,
               assigned_to = excluded.assigned_to,
               assigned_by = excluded.assigned_by,
               assigned_at = excluded.assigned_at""",
        (article_key, company_name, assigned_to, actor_id, assigned_at),
    )
    db.commit()
    row = db.execute("SELECT * FROM article_marks WHERE article_key = ?", (article_key,)).fetchone()
    return jsonify(dict(row))


@app.route("/api/analyst_activity")
def analyst_activity():
    """Per-analyst workload: how many store_events rows they entered (extracted),
    how many articles they've marked done, and how many are currently assigned
    to them but still open."""
    db = get_db()

    entered = {
        r["entered_by"]: r["cnt"]
        for r in db.execute(
            "SELECT entered_by, COUNT(*) AS cnt FROM store_events WHERE entered_by IS NOT NULL GROUP BY entered_by"
        ).fetchall()
    }
    completed = {
        r["marked_by"]: r["cnt"]
        for r in db.execute(
            "SELECT marked_by, COUNT(*) AS cnt FROM article_marks WHERE is_done = 1 AND marked_by IS NOT NULL GROUP BY marked_by"
        ).fetchall()
    }
    assigned_open = {
        r["assigned_to"]: r["cnt"]
        for r in db.execute(
            """SELECT assigned_to, COUNT(*) AS cnt FROM article_marks
               WHERE assigned_to IS NOT NULL AND is_done = 0 GROUP BY assigned_to"""
        ).fetchall()
    }

    analysts = db.execute(
        "SELECT analyst_id, analyst_name, role FROM analysts ORDER BY analyst_id"
    ).fetchall()

    rows = [
        {
            "analyst_id": a["analyst_id"],
            "analyst_name": a["analyst_name"],
            "role": a["role"],
            "entered_count": entered.get(a["analyst_id"], 0),
            "completed_count": completed.get(a["analyst_id"], 0),
            "assigned_open_count": assigned_open.get(a["analyst_id"], 0),
        }
        for a in analysts
    ]
    return jsonify(rows)


@app.route("/api/store_events/bulk", methods=["POST", "OPTIONS"])
def bulk_add_store_events():
    """Add one or more store_events rows from a CSV upload or a quick
    "just the URL" add. Only article_link is required per row — everything
    else (company_name, event_type, status, event_date, location) is
    optional and gets attached to a bare-URL row later as it's researched.
    Duplicate (article_link, company_name) pairs already on file are
    skipped rather than double-entered."""
    if request.method == "OPTIONS":
        return ("", 204)

    db = get_db()
    data = request.get_json(force=True) or {}
    source = (data.get("source") or "").strip()
    actor_id = data.get("actor_analyst_id")
    incoming_rows = data.get("rows") or []

    if not source:
        return jsonify({"error": "source is required"}), 400
    if not isinstance(incoming_rows, list) or not incoming_rows:
        return jsonify({"error": "rows must be a non-empty list"}), 400

    inserted = 0
    skipped_duplicate = 0
    skipped_invalid = 0
    row_errors = []

    for i, raw in enumerate(incoming_rows):
        article_link = (raw.get("article_link") or "").strip()
        if not article_link:
            skipped_invalid += 1
            row_errors.append(f"row {i + 1}: article_link is required")
            continue

        company_name = (raw.get("company_name") or "").strip() or None

        existing = db.execute(
            "SELECT 1 FROM store_events WHERE article_link = ? AND company_name IS ?",
            (article_link, company_name),
        ).fetchone()
        if existing:
            skipped_duplicate += 1
            continue

        if company_name:
            db.execute("INSERT OR IGNORE INTO companies (company_name) VALUES (?)", (company_name,))

        event_type_id = None
        status_id = None
        event_type_name = (raw.get("event_type") or "").strip()
        status_label = (raw.get("status") or "").strip()
        if event_type_name:
            et = db.execute(
                "SELECT event_type_id FROM event_types WHERE lower(name) = lower(?)", (event_type_name,)
            ).fetchone()
            if et:
                event_type_id = et["event_type_id"]
                if status_label:
                    st = db.execute(
                        """SELECT status_id FROM observation_statuses
                           WHERE event_type_id = ? AND lower(label) = lower(?)""",
                        (event_type_id, status_label),
                    ).fetchone()
                    if st:
                        status_id = st["status_id"]

        location = (raw.get("location") or "").strip()
        city, state = None, None
        if location:
            parts = [p.strip() for p in location.rsplit(",", 1)]
            if len(parts) == 2 and parts[1]:
                city, state = parts
            else:
                city = location

        event_date_raw = (raw.get("event_date") or "").strip() or None

        db.execute(
            """INSERT INTO store_events
               (source, article_link, company_name, event_type_id, observation_status_id,
                event_date_raw, city, state, entered_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (source, article_link, company_name, event_type_id, status_id,
             event_date_raw, city, state, actor_id),
        )
        inserted += 1

    db.commit()
    return jsonify({
        "inserted": inserted,
        "skipped_duplicate": skipped_duplicate,
        "skipped_invalid": skipped_invalid,
        "errors": row_errors,
    })


if __name__ == "__main__":
    init_db()
    print(f"Demo DB at {DB_PATH}")
    app.run(host="127.0.0.1", port=5000, debug=True)
