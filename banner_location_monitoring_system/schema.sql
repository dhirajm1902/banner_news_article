-- ============================================================
-- Banner Location Monitoring System (BLMS) - Supabase schema
--
-- Run once in the SEPARATE BLMS Supabase project -> SQL Editor.
-- Safe to re-run. (blms_ prefix kept so the schema could also live
-- next to the dashboard's other tables without name clashes.)
--
--   blms_companies           master list of monitored companies
--   blms_scrape_runs         one row per company per execution
--   blms_store_locations     current master data (one row per unique store,
--                            never deleted - removed stores get is_active=false)
--   blms_location_snapshots  what every store looked like in every successful run
--   blms_location_changes    NEW / REMOVED / UPDATED / UNCHANGED per run
--
-- Writes come from monitor.py with the service_role key (bypasses RLS).
-- The dashboard logs users in through the MAIN project, so this project
-- never sees a logged-in user: the dashboard reads with this project's
-- anon key, and the tables are read-only for anon (public store-locator data).
-- ============================================================

CREATE TABLE IF NOT EXISTS blms_companies (
    id                     bigserial PRIMARY KEY,
    company_key            text UNIQUE NOT NULL,
    company_name           text NOT NULL,
    website_url            text,
    locator_url            text,
    scraper_module         text,
    scraper_type           text,                 -- API / HTML / Browser ...
    scrape_frequency       text DEFAULT 'weekly',
    is_active              boolean NOT NULL DEFAULT true,
    last_scraped_at        timestamptz,
    last_run_status        text,
    last_success_at        timestamptz,
    last_successful_run_id bigint,
    created_at             timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS blms_scrape_runs (
    id               bigserial PRIMARY KEY,
    company_id       bigint NOT NULL REFERENCES blms_companies(id),
    started_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,
    duration_seconds numeric,
    status           text NOT NULL,              -- running / completed / incomplete / failed
    trigger          text,                       -- schedule / manual / workflow_dispatch
    is_baseline      boolean DEFAULT false,      -- first successful run of a company
    locations_found  integer,
    new_count        integer,
    removed_count    integer,
    updated_count    integer,
    unchanged_count  integer,
    review_count     integer,
    warnings         jsonb,
    error_message    text
);
CREATE INDEX IF NOT EXISTS blms_runs_company_idx ON blms_scrape_runs (company_id, started_at DESC);

CREATE TABLE IF NOT EXISTS blms_store_locations (
    id                bigserial PRIMARY KEY,
    company_id        bigint NOT NULL REFERENCES blms_companies(id),
    location_key      text NOT NULL,             -- id:<store id> | addr:<normalized address|zip> | name:<...>
    external_store_id text,
    store_name        text,
    address           text,
    city              text,
    state             text,
    zip_code          text,
    country           text,
    phone             text,
    latitude          double precision,
    longitude         double precision,
    status            text,                      -- Open / Coming Soon / Temporarily Closed / Closed
    store_page        text,
    address_key       text,
    name_key          text,
    raw_data          jsonb,                     -- every scraper column (hours, amenities...)
    is_active         boolean NOT NULL DEFAULT true,
    first_seen_at     timestamptz,
    last_seen_at      timestamptz,
    removed_at        timestamptz,
    first_seen_run_id bigint,
    last_seen_run_id  bigint,
    updated_at        timestamptz,
    UNIQUE (company_id, location_key)
);
CREATE INDEX IF NOT EXISTS blms_loc_company_idx ON blms_store_locations (company_id, is_active);

CREATE TABLE IF NOT EXISTS blms_location_snapshots (
    id                bigserial PRIMARY KEY,
    run_id            bigint NOT NULL REFERENCES blms_scrape_runs(id),
    company_id        bigint NOT NULL REFERENCES blms_companies(id),
    location_id       bigint NOT NULL REFERENCES blms_store_locations(id),
    external_store_id text,
    store_name        text,
    address           text,
    city              text,
    state             text,
    zip_code          text,
    phone             text,
    latitude          double precision,
    longitude         double precision,
    status            text,
    captured_at       timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS blms_snap_run_idx ON blms_location_snapshots (run_id);
CREATE INDEX IF NOT EXISTS blms_snap_loc_idx ON blms_location_snapshots (location_id);

CREATE TABLE IF NOT EXISTS blms_location_changes (
    id              bigserial PRIMARY KEY,
    run_id          bigint NOT NULL REFERENCES blms_scrape_runs(id),
    company_id      bigint NOT NULL REFERENCES blms_companies(id),
    location_id     bigint REFERENCES blms_store_locations(id),
    change_type     text NOT NULL CHECK (change_type IN ('NEW','REMOVED','UPDATED','UNCHANGED')),
    match_method    text,                        -- store_id / address / name_city_state
    changed_fields  text[],
    previous_values jsonb,
    new_values      jsonb,
    reappeared      boolean DEFAULT false,       -- NEW because a removed store came back
    needs_review    boolean DEFAULT false,
    review_reason   text,
    review_status   text,                        -- pending / confirmed / rejected (NULL = no review needed)
    reviewed_by     text,
    reviewed_at     timestamptz,
    -- denormalized for fast dashboard rendering
    store_name      text,
    address         text,
    city            text,
    state           text,
    zip_code        text,
    detected_at     timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS blms_chg_run_idx  ON blms_location_changes (run_id);
CREATE INDEX IF NOT EXISTS blms_chg_type_idx ON blms_location_changes (change_type, detected_at DESC);
CREATE INDEX IF NOT EXISTS blms_chg_review_idx ON blms_location_changes (needs_review, review_status);

-- ── Row level security ─────────────────────────────────────────
ALTER TABLE blms_companies          ENABLE ROW LEVEL SECURITY;
ALTER TABLE blms_scrape_runs        ENABLE ROW LEVEL SECURITY;
ALTER TABLE blms_store_locations    ENABLE ROW LEVEL SECURITY;
ALTER TABLE blms_location_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE blms_location_changes   ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['blms_companies','blms_scrape_runs','blms_store_locations',
                           'blms_location_snapshots','blms_location_changes'] LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_read', t);
    -- read-only for the dashboard (anon key); no INSERT/UPDATE/DELETE policies,
    -- so only monitor.py (service_role) can write.
    EXECUTE format('CREATE POLICY %I ON %I FOR SELECT TO anon, authenticated USING (true)', t || '_read', t);
  END LOOP;
END $$;

-- Newer Supabase projects don't auto-grant table access to API roles.
-- Dashboard (anon) may only SELECT; monitor.py (service_role) may write.
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
GRANT SELECT ON blms_companies, blms_scrape_runs, blms_store_locations,
                blms_location_snapshots, blms_location_changes TO anon, authenticated;
GRANT ALL ON blms_companies, blms_scrape_runs, blms_store_locations,
             blms_location_snapshots, blms_location_changes TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- ── OPTIONAL: Confirm / Reject buttons in the dashboard's Manual Review ──
-- Off by default: with the anon key, ANYONE holding the page's key could
-- change review status. To enable, run the block below AND set
-- LM_REVIEW_WRITABLE = true in index.html.
--
-- DROP POLICY IF EXISTS blms_location_changes_review ON blms_location_changes;
-- CREATE POLICY blms_location_changes_review ON blms_location_changes
--     FOR UPDATE TO anon USING (needs_review) WITH CHECK (needs_review);
-- REVOKE UPDATE ON blms_location_changes FROM anon;
-- GRANT  UPDATE (review_status, reviewed_by, reviewed_at) ON blms_location_changes TO anon;
