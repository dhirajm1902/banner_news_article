-- ============================================================
-- banner_news_article — PROPOSED consistent schema (v2)
--
-- Goal: one normalized "master company" table that every other
-- table maps to through company_name, and one unified store_events
-- table that the chatbot can search across ALL sources (banner,
-- businessdebut, ct_scoop, restaurant, daily_news) instead of
-- querying five differently-shaped extraction tables.
--
-- This is a TARGET schema to review, not a live migration —
-- the current deployed schema is in supabase_schema.sql.
-- Paste this file into dbdiagram.io to view the ER diagram.
--
-- Design decisions locked in with the team:
--  • ONE unified store_events table (not 5 per-source tables)
--  • companies table = just company_id + company_name (no extra fields yet)
--  • "reason" can apply to any event_type, not just Closing
--  • plain column names (Arms/Appien field-name aliases noted as comments)
--  • EVERY content table maps to companies via company_name directly
--    (companies.company_name is UNIQUE, so it's a valid FK target) —
--    no separate company_id column duplicated on every table.
--
-- Judgment call: article_marks is excluded from company_name mapping.
-- It's a UI bookmark flag keyed by article_key, not article content —
-- say the word and I'll add it there too.
-- ============================================================


-- ── 1. Master company table ──────────────────────────────────
-- Every other table's company_name column maps back to exactly one
-- row here. company_name carries the real UNIQUE constraint (required
-- so other tables can REFERENCE it); the lower(company_name) index
-- additionally blocks case-only duplicates like "Aldi" vs "ALDI".

CREATE TABLE companies (
    company_id      bigserial PRIMARY KEY,
    company_name    text UNIQUE NOT NULL,
    created_at      timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX companies_name_ci_idx ON companies (lower(company_name));


-- ── 2. Controlled-vocabulary lookup tables ───────────────────
-- These replace free-text "whatever the LLM felt like writing"
-- with a fixed set of values, so search/filter/group-by in the
-- chatbot actually works. Seeded with the exact wording you gave.

CREATE TABLE event_types (
    event_type_id   smallserial PRIMARY KEY,
    name            text UNIQUE NOT NULL      -- Arms: "TNT Type" · Appien: "Event Type"
);

INSERT INTO event_types (name) VALUES ('Opening'), ('Closing'), ('Remodel');


CREATE TABLE observation_statuses (
    status_id       smallserial PRIMARY KEY,
    event_type_id   smallint NOT NULL REFERENCES event_types(event_type_id),
    label           text NOT NULL,
    UNIQUE (event_type_id, label)
);

-- Opening-flavored wording
INSERT INTO observation_statuses (event_type_id, label)
SELECT event_type_id, label FROM event_types, unnest(ARRAY[
    'planned opening', 'opening soon', 'set to open', 'under construction',
    'opened', 'grand opening'
]) AS label WHERE event_types.name = 'Opening';

-- Closing-flavored wording
INSERT INTO observation_statuses (event_type_id, label)
SELECT event_type_id, label FROM event_types, unnest(ARRAY[
    'planned closing', 'closing soon', 'set to close', 'closed',
    'permanently closed', 'shut down'
]) AS label WHERE event_types.name = 'Closing';

-- Remodel-flavored wording
INSERT INTO observation_statuses (event_type_id, label)
SELECT event_type_id, label FROM event_types, unnest(ARRAY[
    'under renovation', 'remodeling', 'renovation planned', 'reopened after remodel'
]) AS label WHERE event_types.name = 'Remodel';


CREATE TABLE event_reasons (
    reason_id       smallserial PRIMARY KEY,
    label           text UNIQUE NOT NULL
);

INSERT INTO event_reasons (label) VALUES
    ('Business Closing'), ('Store Closing'), ('Chain Closing'),
    ('Restaurant Closing'), ('Facility Closing'), ('DIP/Leasing Rejection'),
    ('Rebranding'), ('Relocating'), ('Temporary'), ('Mass Closing');
-- Applies to any event_type (e.g. "Relocating" can describe an Opening
-- that's really a relocated Closing) — leave NULL when the article
-- doesn't clearly support one of these values.


-- ── 3. Unified store events table ────────────────────────────
-- One row = one company's one event, extracted from one article.
-- Replaces businessdebut_master_extraction, ct_scoop_master_extraction,
-- restaurant_master_extraction and daily_news_master_extraction.

CREATE TABLE store_events (
    event_id            bigserial PRIMARY KEY,

    -- provenance
    source              text NOT NULL CHECK (source IN (
                            'banner', 'businessdebut', 'ct_scoop',
                            'restaurant', 'daily_news', 'daily_news_bankruptcy'
                        )),
    article_link        text NOT NULL,
    published_date      text,
    source_batch        text,

    -- who
    company_name        text REFERENCES companies(company_name),
    store_name           text,      -- specific store/shop/restaurant name, if it
                                     -- differs from the parent company. Only populate
                                     -- for closures when the article explicitly supports it.

    -- what / when
    event_type_id         smallint REFERENCES event_types(event_type_id),          -- Arms: "TNT Type" · Appien: "Event Type"
    observation_status_id smallint REFERENCES observation_statuses(status_id),     -- normalized article wording (see lookup table)
    event_date_raw         text,    -- exact wording from the article, e.g. "by year's end", "Not specified"
    event_date              date,   -- best-effort parsed date; NULL if the article gives no clean date  — Arms/Appien: "Date Effective"
    reason_id               smallint REFERENCES event_reasons(reason_id),          -- nullable; only when clearly supported by the article

    -- where (split out of the old single "location" free-text field)
    address_line1         text,
    city                   text,
    state                  text,
    zip_code               text,
    county                 text,

    -- summary
    comment                text CHECK (comment IS NULL OR comment ILIKE '%According to source%'),
    -- Required format: "[extraction date], According to source - <2-3 sentence factual summary>"
    -- Summary must only restate the event date/status from the article — no opinions or outside context.

    date_appended          date DEFAULT CURRENT_DATE
);

CREATE INDEX store_events_company_idx  ON store_events (company_name);
CREATE INDEX store_events_type_idx     ON store_events (event_type_id);
CREATE INDEX store_events_state_idx    ON store_events (state);
CREATE INDEX store_events_date_idx     ON store_events (event_date);
CREATE INDEX store_events_article_idx  ON store_events (article_link);


-- ── 4. Raw scrapes — every table now carries company_name, mapped
--      straight to companies.company_name (no separate id column to
--      keep in sync). For a roundup article that names several
--      companies, this holds the single most-prominent company
--      mentioned (title company, first match, etc.) — the full
--      per-company breakdown always lives in store_events, which
--      supports many rows per article_link. ──

-- Banner store news — Store is matched against analyst.csv at scrape
-- time, so company_name is populated directly and reliably.
CREATE TABLE banner_news_master (
    "Link"          text PRIMARY KEY,
    company_name    text NOT NULL REFERENCES companies(company_name),   -- was "Store"
    "Analyst"       text,
    "Industry"      text,
    "Type"          text,
    "Title"         text,
    "Published"     timestamptz,
    "Summary"       text,
    "Date_Appended" date
);

CREATE TABLE businessdebut_master (
    link            text PRIMARY KEY,
    title           text,
    date            text,
    company_name    text REFERENCES companies(company_name),   -- primary company mentioned, if identifiable
    "Date_Appended" date
);

CREATE TABLE ct_scoop_master (
    link            text PRIMARY KEY,
    heading         text,
    date            text,
    company_name    text REFERENCES companies(company_name),
    "Date_Appended" date
);

CREATE TABLE restaurant_master (
    url             text PRIMARY KEY,
    date            text,
    title           text,
    address         text,
    company_name    text REFERENCES companies(company_name),
    "Date_Appended" date
);

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
    company_name     text REFERENCES companies(company_name),
    "Date_Appended"  date
);

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
    company_name     text REFERENCES companies(company_name),
    "Date_Appended"  date
);

CREATE TABLE bizjournals_master (
    id              bigserial PRIMARY KEY,
    title           text,
    url             text,
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
    company_name    text REFERENCES companies(company_name),   -- often same value as jsonld_name once resolved
    "Date_Appended" date
);

-- "Coming soon" tracker — one page per company, so links straight in.
CREATE TABLE company_website_master (
    id              bigserial PRIMARY KEY,
    company_name    text NOT NULL REFERENCES companies(company_name),   -- was "company"
    address         text,
    opening_date    text,
    link            text,
    is_new          boolean DEFAULT false,
    first_seen      date,
    "Date_Appended" date
);

-- State WARN Act notices — one notice per company, so links straight in.
-- ~40 additional state-portal-specific raw columns exist in the live
-- CSV and are intentionally omitted here for diagram readability.
CREATE TABLE warn_master (
    id                  bigserial PRIMARY KEY,
    company_name        text NOT NULL REFERENCES companies(company_name),   -- was "company"
    state               text,
    city                text,
    notice_date         date,
    layoff_date         date,
    employees_affected  integer,
    closure_type        text,
    notes               text,
    "Date_Appended"     date
);

CREATE INDEX banner_news_master_company_idx           ON banner_news_master (company_name);
CREATE INDEX businessdebut_master_company_idx         ON businessdebut_master (company_name);
CREATE INDEX ct_scoop_master_company_idx              ON ct_scoop_master (company_name);
CREATE INDEX restaurant_master_company_idx            ON restaurant_master (company_name);
CREATE INDEX daily_news_master_company_idx            ON daily_news_master (company_name);
CREATE INDEX daily_news_master_bankruptcy_company_idx ON daily_news_master_bankruptcy (company_name);
CREATE INDEX bizjournals_master_company_idx           ON bizjournals_master (company_name);
CREATE INDEX company_website_master_company_idx       ON company_website_master (company_name);
CREATE INDEX warn_master_company_idx                  ON warn_master (company_name);


-- ── 5. Supabase-only application state ───────────────────────

CREATE TABLE article_marks (
    article_key text PRIMARY KEY,
    is_done     boolean DEFAULT false,
    marked_by   text,
    marked_at   timestamptz
);
