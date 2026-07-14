-- ============================================================
-- banner_news_article — full data model (as of 2026-07-02)
--
-- Sources: master_file/*.csv headers, sync_to_supabase.py
-- (TABLE_CONFIG), index.html (CSV_META + _sb.from('article_marks')).
--
-- Scope note: banner_news_master, businessdebut_master,
-- ct_scoop_master, ct_scoop_master_extraction, daily_news_master,
-- restaurant_master and article_marks are the tables that actually
-- live in Supabase today (sync_to_supabase.py). The rest
-- (*_extraction tables besides ct_scoop, warn_master,
-- company_website_master, bizjournals_master,
-- daily_news_master_bankruptcy) exist only as CSVs under
-- master_file/ and are read directly by the frontend — they're
-- included here so you can see the whole picture in one diagram.
--
-- How to view: paste this whole file into dbdiagram.io
-- ("Create new" → paste into the editor, it auto-detects SQL) or
-- import as DDL in drawSQL / DataGrip / TablePlus.
-- ============================================================


-- ── Raw scrapes ────────────────────────────────────────────────

-- Banner store news (fetch_banner_store_news.py, Google News RSS)
-- Already structured at scrape time, so no separate extraction table.
CREATE TABLE banner_news_master (
    "Link"          text PRIMARY KEY,
    "Store"         text NOT NULL,
    "Analyst"       text,
    "Industry"      text,
    "Type"          text,           -- Opening / Closing / Remodel
    "Title"         text,
    "Published"     timestamptz,
    "Summary"       text,
    "Date_Appended" date
);

-- BusinessDebut.com scraper (businessdebut_scraper.py)
CREATE TABLE businessdebut_master (
    link           text PRIMARY KEY,
    title          text,
    date           text,
    "Date_Appended" date
);

-- CT Scoop scraper (ct_scoop_scraper.py)
CREATE TABLE ct_scoop_master (
    link           text PRIMARY KEY,
    heading        text,
    date           text,
    "Date_Appended" date
);

-- Restaurant listings scraper (restaurant_scraper.py)
CREATE TABLE restaurant_master (
    url             text PRIMARY KEY,
    date            text,
    title           text,
    address         text,
    "Date_Appended" date
);

-- Google News RSS retail pipeline (daily_news_prepare.py input)
CREATE TABLE daily_news_master (
    direct_link      text PRIMARY KEY,
    status           text,
    industry         text,
    region           text,
    title            text,
    source           text,
    published_date   text,
    keyword          text,
    relevance_score  numeric,
    "Date_Appended"  date
);

-- Same shape as daily_news_master, filtered to bankruptcy stories.
-- Standalone — not currently split out into its own extraction table.
CREATE TABLE daily_news_master_bankruptcy (
    direct_link      text PRIMARY KEY,
    status           text,
    industry         text,
    region           text,
    title            text,
    source           text,
    published_date   text,
    keyword          text,
    relevance_score  numeric,
    "Date_Appended"  date
);

-- BizJournals scraper — standalone, richer scrape (JSON-LD + og: tags)
CREATE TABLE bizjournals_master (
    id              bigserial PRIMARY KEY,
    title           text,
    url             text,          -- not unique: same story re-scraped across runs
    source          text,
    pub_date        text,
    snippet         text,
    query           text,
    full_text       text,
    summary         text,
    keywords        text,
    jsonld_name     text,
    jsonld_date     text,
    jsonld_address  text,
    og_description  text,
    "Date_Appended" date
);


-- ── Claude extraction pipeline ──────────────────────────────────
-- One raw article can yield MULTIPLE structured store events
-- (e.g. a "6 stores closing this month" roundup), so article_link
-- is a many-to-one FK back to the raw table, not a unique key —
-- except ct_scoop_master_extraction, which Supabase upserts on
-- article_link today (see sync_to_supabase.py); that means two
-- events from the same CT Scoop article currently overwrite each
-- other in Supabase — worth revisiting if that table gets a real
-- migration.

CREATE TABLE businessdebut_master_extraction (
    id                bigserial PRIMARY KEY,
    store_name        text,
    location          text,
    event_type        text,        -- Opening / Closing / Remodel
    event_date        text,
    status            text,
    short_description text,
    article_link      text REFERENCES businessdebut_master(link),
    published_date    text,
    source_batch      text,
    "Date_Appended"   date
);

CREATE TABLE ct_scoop_master_extraction (
    article_link      text PRIMARY KEY REFERENCES ct_scoop_master(link),
    store_name        text,
    location          text,
    event_type        text,
    event_date        text,
    status            text,
    short_description text,
    published_date    text,
    source_batch      text,
    "Date_Appended"   date
);

CREATE TABLE restaurant_master_extraction (
    id                bigserial PRIMARY KEY,
    store_name        text,
    location          text,
    event_type        text,
    event_date        text,
    status            text,
    short_description text,
    article_link      text REFERENCES restaurant_master(url),
    published_date    text,
    source_batch      text,
    "Date_Appended"   date
);

CREATE TABLE daily_news_master_extraction (
    id                bigserial PRIMARY KEY,
    store_name        text,
    location          text,
    event_type        text,
    event_date        text,
    status            text,
    short_description text,
    article_link      text REFERENCES daily_news_master(direct_link),
    published_date    text,
    source_batch      text,
    "Date_Appended"   date
);


-- ── Other standalone trackers ────────────────────────────────────

-- "Coming soon" pages on company websites (company_website_master.csv)
CREATE TABLE company_website_master (
    id              bigserial PRIMARY KEY,
    company         text NOT NULL,
    address         text,
    opening_date    text,
    link            text,          -- not unique: same page re-checked across runs
    is_new          boolean DEFAULT false,
    first_seen      date,
    "Date_Appended" date
);

-- State WARN Act notices (warn.py). The scraper has ~40 state-specific
-- raw columns beyond these (varies per state portal format); only the
-- canonical fields actually consumed by the frontend/chat widget are
-- modeled here to keep the diagram readable.
CREATE TABLE warn_master (
    id                  bigserial PRIMARY KEY,
    state               text NOT NULL,
    company             text,
    city                text,
    notice_date         date,
    layoff_date         date,
    employees_affected  integer,
    closure_type        text,
    notes               text,
    "Date_Appended"     date
);


-- ── Supabase-only application state ─────────────────────────────

-- Dashboard "mark as done" toggle (index.html). article_key is a
-- synthetic key built client-side from an article's source + link,
-- so it can point at a row in any of the tables above — it's a
-- polymorphic reference, not a real foreign key.
CREATE TABLE article_marks (
    article_key text PRIMARY KEY,
    is_done     boolean DEFAULT false,
    marked_by   text,
    marked_at   timestamptz
);
