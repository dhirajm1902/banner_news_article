-- ============================================================
-- master_events_schema_v3 — Supabase migration (additive)
--
-- Creates the NEW unified schema (companies, analysts, event_types,
-- observation_statuses, event_reasons, store_events, scraped_articles)
-- from master_events_schema_v3_clean.sql, plus the login/assignment/
-- completion-status extensions built in demo_app/backend/app.py.
--
-- Does NOT touch any existing table. In particular, the old
-- article_marks (banner_news_master / ct_scoop_master / ... era,
-- shape: article_key, is_done, marked_by, marked_at) is left alone —
-- the new workflow table here is named article_marks_v3 to avoid
-- colliding with it. index.html / sync_to_supabase.py keep working
-- against the old tables unchanged until you decide to cut over.
--
-- Safe to re-run: every statement is guarded with IF NOT EXISTS.
-- ============================================================


-- ── 1. Master company table ───────────────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    company_id      bigserial PRIMARY KEY,
    company_name    text UNIQUE NOT NULL,
    created_at      timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS companies_name_ci_idx ON companies (lower(company_name));


-- ── 2. Analysts ────────────────────────────────────────────────
-- analyst_id is a 4-digit text code assigned in order: '0001', '0002', ...
-- role/password_hash are additions on top of v3_clean, needed for the
-- login system built in demo_app (admin vs analyst).

CREATE SEQUENCE IF NOT EXISTS analyst_id_seq START 1;

CREATE TABLE IF NOT EXISTS analysts (
    analyst_id      text PRIMARY KEY DEFAULT lpad(nextval('analyst_id_seq')::text, 4, '0'),
    analyst_name    text NOT NULL,
    email           text UNIQUE,
    role            text NOT NULL DEFAULT 'analyst' CHECK (role IN ('analyst', 'admin')),
    password_hash   text NOT NULL,
    created_at      timestamptz DEFAULT now()
);


-- ── 3. Controlled-vocabulary lookup tables ───────────────────
CREATE TABLE IF NOT EXISTS event_types (
    event_type_id   smallserial PRIMARY KEY,
    name            text UNIQUE NOT NULL
);

INSERT INTO event_types (name)
SELECT * FROM (VALUES ('Opening'), ('Closing'), ('Remodel')) AS v(name)
WHERE NOT EXISTS (SELECT 1 FROM event_types);


CREATE TABLE IF NOT EXISTS observation_statuses (
    status_id       smallserial PRIMARY KEY,
    event_type_id   smallint NOT NULL REFERENCES event_types(event_type_id),
    label           text NOT NULL,
    UNIQUE (event_type_id, label)
);

INSERT INTO observation_statuses (event_type_id, label)
SELECT et.event_type_id, s.label
FROM event_types et
JOIN (VALUES
    ('Opening', 'planned opening'), ('Opening', 'opening soon'), ('Opening', 'set to open'),
    ('Opening', 'under construction'), ('Opening', 'opened'), ('Opening', 'grand opening'),
    ('Closing', 'planned closing'), ('Closing', 'closing soon'), ('Closing', 'set to close'),
    ('Closing', 'closed'), ('Closing', 'permanently closed'), ('Closing', 'shut down'),
    ('Remodel', 'under renovation'), ('Remodel', 'remodeling'),
    ('Remodel', 'renovation planned'), ('Remodel', 'reopened after remodel')
) AS s(type_name, label) ON s.type_name = et.name
ON CONFLICT (event_type_id, label) DO NOTHING;


CREATE TABLE IF NOT EXISTS event_reasons (
    reason_id       smallserial PRIMARY KEY,
    label           text UNIQUE NOT NULL
);

INSERT INTO event_reasons (label)
SELECT * FROM (VALUES
    ('Business Closing'), ('Store Closing'), ('Chain Closing'),
    ('Restaurant Closing'), ('Facility Closing'), ('DIP/Leasing Rejection'),
    ('Rebranding'), ('Relocating'), ('Temporary'), ('Mass Closing')
) AS v(label)
ON CONFLICT (label) DO NOTHING;


-- ── 4. Unified store events table (structured extraction output) ─
CREATE TABLE IF NOT EXISTS store_events (
    event_id               bigserial PRIMARY KEY,

    source                 text NOT NULL CHECK (source IN (
                                'banner', 'businessdebut', 'ct_scoop',
                                'restaurant', 'daily_news', 'daily_news_bankruptcy'
                            )),
    article_link            text NOT NULL,
    published_date          text,
    source_batch            text,

    company_name            text REFERENCES companies(company_name),
    store_name               text,

    event_type_id             smallint REFERENCES event_types(event_type_id),
    observation_status_id     smallint REFERENCES observation_statuses(status_id),
    event_date_raw             text,
    event_date                  date,
    reason_id                    smallint REFERENCES event_reasons(reason_id),

    address_line1                 text,
    city                           text,
    state                          text,
    zip_code                       text,
    county                         text,

    comment                        text CHECK (comment IS NULL OR comment ILIKE '%According to source%'),

    date_appended                  date DEFAULT CURRENT_DATE,
    entered_by                     text REFERENCES analysts(analyst_id)
);

CREATE INDEX IF NOT EXISTS store_events_company_idx    ON store_events (company_name);
CREATE INDEX IF NOT EXISTS store_events_type_idx       ON store_events (event_type_id);
CREATE INDEX IF NOT EXISTS store_events_state_idx      ON store_events (state);
CREATE INDEX IF NOT EXISTS store_events_date_idx       ON store_events (event_date);
CREATE INDEX IF NOT EXISTS store_events_article_idx    ON store_events (article_link);
CREATE INDEX IF NOT EXISTS store_events_entered_by_idx ON store_events (entered_by);


-- ── 5. Unified raw-scrape table ───────────────────────────────
CREATE TABLE IF NOT EXISTS scraped_articles (
    id              bigserial PRIMARY KEY,

    source          text NOT NULL CHECK (source IN (
                        'banner', 'businessdebut', 'ct_scoop', 'restaurant',
                        'daily_news', 'daily_news_bankruptcy', 'bizjournals',
                        'company_website', 'warn'
                    )),

    link            text,
    title           text,
    published_date  text,
    company_name    text REFERENCES companies(company_name),
    address         text,
    summary         text,
    state           text,
    city            text,

    extra_data      jsonb DEFAULT '{}'::jsonb,

    date_appended   date DEFAULT CURRENT_DATE,

    UNIQUE (source, link)
);

CREATE INDEX IF NOT EXISTS scraped_articles_company_idx  ON scraped_articles (company_name);
CREATE INDEX IF NOT EXISTS scraped_articles_source_idx   ON scraped_articles (source);
CREATE INDEX IF NOT EXISTS scraped_articles_state_idx    ON scraped_articles (state);
CREATE INDEX IF NOT EXISTS scraped_articles_extra_gin    ON scraped_articles USING gin (extra_data);


-- ── 6. Workflow / application state (renamed to avoid colliding ──
--       with the OLD article_marks table already live in Supabase)
-- Tracks: done/not-done, who marked it, who it's assigned to, and
-- the completion_status triage value (Add / Edit / Already Updated /
-- Not Relevant / Not Accessible / Send to the Calling Team).
CREATE TABLE IF NOT EXISTS article_marks_v3 (
    article_key       text PRIMARY KEY,   -- "{article_link}::{company_name}" — see markKey() in app.js
    company_name      text REFERENCES companies(company_name),
    is_done           boolean DEFAULT false,
    marked_by         text REFERENCES analysts(analyst_id),
    marked_at         timestamptz,
    assigned_to       text REFERENCES analysts(analyst_id),
    assigned_by       text REFERENCES analysts(analyst_id),
    assigned_at       timestamptz,
    completion_status text CHECK (completion_status IS NULL OR completion_status IN (
                            'Add', 'Edit', 'Already Updated', 'Not Relevant',
                            'Not Accessible', 'Send to the Calling Team'
                        ))
);

CREATE INDEX IF NOT EXISTS article_marks_v3_analyst_idx    ON article_marks_v3 (marked_by);
CREATE INDEX IF NOT EXISTS article_marks_v3_company_idx    ON article_marks_v3 (company_name);
CREATE INDEX IF NOT EXISTS article_marks_v3_assigned_idx   ON article_marks_v3 (assigned_to);
