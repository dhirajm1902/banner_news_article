-- ============================================================
-- banner_news_article — PROPOSED consistent schema (v3)
--
-- Builds on master_events_schema.sql (v2). v2 already unified the
-- 5 per-source EXTRACTION tables into one store_events table.
-- v3 adds the pieces discussed on 2026-07-28:
--
--  1. UNIFY THE RAW SCRAPES TOO — the 9 raw per-source tables
--     (banner_news_master, businessdebut_master, ct_scoop_master,
--     restaurant_master, daily_news_master, daily_news_master_bankruptcy,
--     bizjournals_master, company_website_master, warn_master) become
--     ONE raw_articles table. Every source has link/title/date/summary/
--     company_name in common; whatever is source-specific (banner's
--     Analyst/Industry, daily_news's region/keyword/relevance_score,
--     bizjournals' jsonld_* fields, warn's ~40 state-portal columns,
--     etc.) goes in one jsonb "extra" column instead of 9 different
--     shapes of table.
--
--  2. ANALYSTS TABLE — analyst_id is a 4-digit text code ('0001',
--     '0002', ...) assigned automatically from a sequence, plus
--     analyst_name. This is the "researcher" identity referenced by
--     store_events below.
--
--  3. REVIEW / OWNERSHIP TRACKING ON store_events — entered_by,
--     review_status ('pending' / 'in_review' / 'done'), reviewed_by,
--     reviewed_at. This is tracked in the database; WHO is actually
--     allowed to write is enforced by the app, not by Postgres
--     row-level security (kept simple on purpose — say the word if
--     you want real DB-level permission enforcement later).
--
--  4. DUPLICATE-ARTICLE HANDLING — "already entered" = same
--     article_link + same company_name already exists in
--     store_events. A trigger checks this on every insert attempt:
--     if a match exists, the EXISTING row is copied into
--     already_existed_articles (so you can see what's already on
--     file) and the new insert is silently skipped — nothing gets
--     double-entered.
--
-- Everything from v2 that nobody asked to change (event_types,
-- observation_statuses, event_reasons, article_marks, the companies
-- table itself) is carried over as-is.
-- ============================================================


-- ── 1. Master company table (unchanged from v2) ──────────────

CREATE TABLE companies (
    company_id      bigserial PRIMARY KEY,
    company_name    text UNIQUE NOT NULL,
    created_at      timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX companies_name_ci_idx ON companies (lower(company_name));


-- ── 2. Analysts (researchers) ─────────────────────────────────
-- analyst_id is a 4-digit text code assigned in order: '0001', '0002', ...
-- Stored literally as text (not zero-padded only for display) so any
-- table can reference it directly as a plain string.

CREATE SEQUENCE analyst_id_seq START 1;

CREATE TABLE analysts (
    analyst_id      text PRIMARY KEY DEFAULT lpad(nextval('analyst_id_seq')::text, 4, '0'),
    analyst_name    text NOT NULL,
    email           text UNIQUE,
    is_active       boolean DEFAULT true,
    created_at      timestamptz DEFAULT now()
);

-- e.g. INSERT INTO analysts (analyst_name, email) VALUES ('Parth Chudasama', 'parthc@retailstat.com');
--      -> analyst_id auto-assigned as '0001'


-- ── 3. Controlled-vocabulary lookup tables (unchanged from v2) ─

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

INSERT INTO observation_statuses (event_type_id, label)
SELECT event_type_id, label FROM event_types, unnest(ARRAY[
    'planned opening', 'opening soon', 'set to open', 'under construction',
    'opened', 'grand opening'
]) AS label WHERE event_types.name = 'Opening';

INSERT INTO observation_statuses (event_type_id, label)
SELECT event_type_id, label FROM event_types, unnest(ARRAY[
    'planned closing', 'closing soon', 'set to close', 'closed',
    'permanently closed', 'shut down'
]) AS label WHERE event_types.name = 'Closing';

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


-- ── 4. Unified store events table (the "extraction" table) ───
-- One row = one company's one event, extracted from one article by
-- one analyst. Adds entered_by / review_status / reviewed_by /
-- reviewed_at on top of the v2 shape.

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

    -- who (company)
    company_name        text REFERENCES companies(company_name),
    store_name           text,      -- specific store/shop/restaurant name, if it
                                     -- differs from the parent company. Only populate
                                     -- for closures when the article explicitly supports it.

    -- what / when
    event_type_id         smallint REFERENCES event_types(event_type_id),
    observation_status_id smallint REFERENCES observation_statuses(status_id),
    event_date_raw         text,
    event_date              date,
    reason_id               smallint REFERENCES event_reasons(reason_id),

    -- where
    address_line1         text,
    city                   text,
    state                  text,
    zip_code               text,
    county                 text,

    -- summary
    comment                text CHECK (comment IS NULL OR comment ILIKE '%According to source%'),

    -- who (analyst / review workflow)
    entered_by             text REFERENCES analysts(analyst_id),   -- who extracted & added this row
    review_status           text NOT NULL DEFAULT 'pending'
                                CHECK (review_status IN ('pending', 'in_review', 'done')),
    reviewed_by             text REFERENCES analysts(analyst_id),   -- who checked/signed off on it
    reviewed_at              timestamptz,

    date_appended          date DEFAULT CURRENT_DATE,

    -- "already entered" = same article + same company. NULL company_name
    -- rows aren't covered by this constraint (Postgres treats each NULL as
    -- distinct) — the duplicate trigger below only fires when company_name
    -- is populated.
    UNIQUE (article_link, company_name)
);

CREATE INDEX store_events_company_idx  ON store_events (company_name);
CREATE INDEX store_events_type_idx     ON store_events (event_type_id);
CREATE INDEX store_events_state_idx    ON store_events (state);
CREATE INDEX store_events_date_idx     ON store_events (event_date);
CREATE INDEX store_events_article_idx  ON store_events (article_link);
CREATE INDEX store_events_review_idx   ON store_events (review_status);


-- ── 5. Duplicate-article archive ──────────────────────────────
-- Mirrors store_events. When an analyst tries to add a row for an
-- article_link + company_name that's already on file, the EXISTING
-- row gets copied here (so you can see what's already recorded) and
-- the new duplicate insert is skipped — see trigger below.

CREATE TABLE already_existed_articles (
    archive_id              bigserial PRIMARY KEY,
    original_event_id       bigint,     -- store_events.event_id this was copied from

    source                  text,
    article_link            text,
    published_date          text,
    source_batch            text,
    company_name            text,
    store_name              text,
    event_type_id           smallint,
    observation_status_id   smallint,
    event_date_raw          text,
    event_date              date,
    reason_id                smallint,
    address_line1            text,
    city                      text,
    state                     text,
    zip_code                  text,
    county                    text,
    comment                   text,
    entered_by                text,
    review_status             text,
    reviewed_by                text,
    reviewed_at                 timestamptz,
    date_appended              date,

    attempted_by             text REFERENCES analysts(analyst_id),  -- who tried to re-add it
    attempted_at              timestamptz DEFAULT now()
);

CREATE OR REPLACE FUNCTION fn_block_duplicate_store_event()
RETURNS trigger AS $$
DECLARE
    existing store_events%ROWTYPE;
BEGIN
    IF NEW.company_name IS NOT NULL THEN
        SELECT * INTO existing
        FROM store_events
        WHERE article_link = NEW.article_link
          AND company_name = NEW.company_name
        LIMIT 1;

        IF FOUND THEN
            INSERT INTO already_existed_articles (
                original_event_id, source, article_link, published_date, source_batch,
                company_name, store_name, event_type_id, observation_status_id,
                event_date_raw, event_date, reason_id, address_line1, city, state,
                zip_code, county, comment, entered_by, review_status, reviewed_by,
                reviewed_at, date_appended, attempted_by
            ) VALUES (
                existing.event_id, existing.source, existing.article_link, existing.published_date, existing.source_batch,
                existing.company_name, existing.store_name, existing.event_type_id, existing.observation_status_id,
                existing.event_date_raw, existing.event_date, existing.reason_id, existing.address_line1, existing.city, existing.state,
                existing.zip_code, existing.county, existing.comment, existing.entered_by, existing.review_status, existing.reviewed_by,
                existing.reviewed_at, existing.date_appended, NEW.entered_by
            );

            RAISE NOTICE 'store_events: % / % already recorded as event_id % — archived to already_existed_articles, new insert skipped',
                NEW.article_link, NEW.company_name, existing.event_id;

            RETURN NULL;  -- cancels the incoming INSERT
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_block_duplicate_store_event
    BEFORE INSERT ON store_events
    FOR EACH ROW
    EXECUTE FUNCTION fn_block_duplicate_store_event();


-- ── 6. Unified raw scrapes ────────────────────────────────────
-- Replaces banner_news_master, businessdebut_master, ct_scoop_master,
-- restaurant_master, daily_news_master, daily_news_master_bankruptcy,
-- bizjournals_master, company_website_master and warn_master.
-- Common fields are real columns; everything source-specific lives
-- in "extra" (jsonb) instead of each source having its own shape:
--
--   banner              -> extra: {analyst, industry, type}
--   businessdebut        -> (no extra fields)
--   ct_scoop              -> (no extra fields; "heading" -> title)
--   restaurant            -> extra: {address}
--   daily_news / _bankruptcy -> extra: {status, industry, region,
--                              original_source, keyword, relevance_score}
--   bizjournals            -> extra: {original_source, snippet, query,
--                              full_text, keywords, jsonld_name,
--                              jsonld_date, jsonld_address, og_description}
--   company_website          -> extra: {address, opening_date, is_new,
--                              first_seen}
--   warn                      -> extra: {state, city, notice_date,
--                              layoff_date, employees_affected,
--                              closure_type, notes, ...state-portal fields}
--
-- link is nullable because a few sources (warn notices, company
-- pages) aren't keyed by an article URL the same way news scrapes are.

CREATE TABLE raw_articles (
    article_id      bigserial PRIMARY KEY,
    source          text NOT NULL CHECK (source IN (
                        'banner', 'businessdebut', 'ct_scoop', 'restaurant',
                        'daily_news', 'daily_news_bankruptcy', 'bizjournals',
                        'company_website', 'warn'
                    )),
    link            text,
    title           text,
    published_date  text,
    summary         text,
    company_name    text REFERENCES companies(company_name),
    extra           jsonb,
    date_appended   date DEFAULT CURRENT_DATE,

    UNIQUE (source, link)
);

CREATE INDEX raw_articles_company_idx ON raw_articles (company_name);
CREATE INDEX raw_articles_source_idx  ON raw_articles (source);
CREATE INDEX raw_articles_extra_gin   ON raw_articles USING gin (extra);


-- ── 7. Supabase-only application state (unchanged from v2) ───

CREATE TABLE article_marks (
    article_key text PRIMARY KEY,
    is_done     boolean DEFAULT false,
    marked_by   text,
    marked_at   timestamptz
);
