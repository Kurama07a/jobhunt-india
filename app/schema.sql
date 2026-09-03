CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS schema_meta (
    key text PRIMARY KEY,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_boards (
    ats text NOT NULL CHECK (ats IN ('ashby', 'greenhouse', 'lever', 'smartrecruiters', 'workable')),
    slug text NOT NULL,
    display_name text NOT NULL,
    is_india_company boolean NOT NULL DEFAULT false,
    discovered_via text NOT NULL DEFAULT 'seed',
    is_active boolean NOT NULL DEFAULT true,
    etag text,
    last_checked_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    consecutive_failures integer NOT NULL DEFAULT 0,
    jobs_seen integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ats, slug)
);

CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ats text NOT NULL CHECK (ats IN ('ashby', 'greenhouse', 'lever', 'smartrecruiters', 'workable')),
    source_job_id text NOT NULL,
    board_slug text NOT NULL,
    company text NOT NULL,
    title text NOT NULL,
    department text NOT NULL DEFAULT '',
    team text NOT NULL DEFAULT '',
    employment_type text NOT NULL DEFAULT '',
    location text NOT NULL DEFAULT '',
    city text,
    is_remote boolean NOT NULL DEFAULT false,
    workplace_type text NOT NULL DEFAULT '',
    published_at timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    is_active boolean NOT NULL DEFAULT true,
    description text NOT NULL DEFAULT '',
    description_excerpt text NOT NULL DEFAULT '',
    apply_url text NOT NULL,
    india_match_reason text NOT NULL,
    role_category text NOT NULL DEFAULT 'software engineering',
    experience_min numeric(4,1),
    experience_max numeric(4,1),
    experience_level text NOT NULL CHECK (
        experience_level IN ('internship', 'entry', 'mid', 'senior', 'unknown')
    ),
    experience_is_explicit boolean NOT NULL DEFAULT false,
    entry_level_score smallint NOT NULL CHECK (entry_level_score BETWEEN 0 AND 100),
    skills text[] NOT NULL DEFAULT '{}',
    salary_min numeric(14,2),
    salary_max numeric(14,2),
    salary_currency text,
    salary_period text,
    content_hash text NOT NULL,
    raw_metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    search_document tsvector GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(title, '') || ' ' || coalesce(company, '') || ' ' ||
            coalesce(location, '') || ' ' || coalesce(department, '') || ' ' ||
            coalesce(team, '') || ' ' || coalesce(description_excerpt, '')
        )
    ) STORED,
    CONSTRAINT jobs_source_unique UNIQUE (ats, source_job_id),
    CONSTRAINT jobs_board_fk FOREIGN KEY (ats, board_slug)
        REFERENCES job_boards (ats, slug) ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    mode text NOT NULL CHECK (mode IN ('incremental', 'refresh_recent', 'full_discovery', 'smoke')),
    status text NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    boards_total integer NOT NULL DEFAULT 0,
    boards_checked integer NOT NULL DEFAULT 0,
    boards_succeeded integer NOT NULL DEFAULT 0,
    boards_failed integer NOT NULL DEFAULT 0,
    boards_unchanged integer NOT NULL DEFAULT 0,
    boards_discovered integer NOT NULL DEFAULT 0,
    jobs_seen integer NOT NULL DEFAULT 0,
    jobs_targeted integer NOT NULL DEFAULT 0,
    jobs_upserted integer NOT NULL DEFAULT 0,
    jobs_closed integer NOT NULL DEFAULT 0,
    error text,
    metadata jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS job_boards_active_idx
    ON job_boards (is_active, ats, slug);
CREATE INDEX IF NOT EXISTS job_boards_india_idx
    ON job_boards (is_india_company) WHERE is_india_company;
CREATE INDEX IF NOT EXISTS jobs_active_recent_idx
    ON jobs (is_active, published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS jobs_entry_recent_idx
    ON jobs (entry_level_score DESC, published_at DESC NULLS LAST)
    WHERE is_active;
CREATE INDEX IF NOT EXISTS jobs_level_idx
    ON jobs (experience_level, is_active);
CREATE INDEX IF NOT EXISTS jobs_company_idx
    ON jobs (company, is_active);
CREATE INDEX IF NOT EXISTS jobs_location_trgm_idx
    ON jobs USING gin (location gin_trgm_ops);
CREATE INDEX IF NOT EXISTS jobs_title_trgm_idx
    ON jobs USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS jobs_skills_idx
    ON jobs USING gin (skills);
CREATE INDEX IF NOT EXISTS jobs_search_idx
    ON jobs USING gin (search_document);
CREATE INDEX IF NOT EXISTS ingestion_runs_requested_idx
    ON ingestion_runs (requested_at DESC);

-- Idempotent migrations for existing databases. schema.sql is executed on every
-- boot, so every statement here must be safe to run repeatedly.

-- Widen the ats CHECK constraints to cover platforms added after v1. CREATE TABLE
-- IF NOT EXISTS never alters a table that already exists, so a running database
-- keeps its original constraint until this runs. Target-list driven: to add a
-- platform, extend `want` and bump schema_version — the block re-derives both
-- constraints to match and is a no-op once they already do.
DO $$
DECLARE
    want text := 'ats IN (''ashby'', ''greenhouse'', ''lever'', ''smartrecruiters'', ''workable'')';
    c record;
BEGIN
    FOR c IN
        SELECT conrelid::regclass AS tbl, conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE contype = 'c'
            AND conrelid IN ('job_boards'::regclass, 'jobs'::regclass)
            AND conname IN ('job_boards_ats_check', 'jobs_ats_check')
    LOOP
        IF position('workable' IN c.def) = 0 THEN
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', c.tbl, c.conname);
        END IF;
    END LOOP;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conname = 'job_boards_ats_check' AND conrelid = 'job_boards'::regclass) THEN
        EXECUTE 'ALTER TABLE job_boards ADD CONSTRAINT job_boards_ats_check CHECK (' || want || ')';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_ats_check' AND conrelid = 'jobs'::regclass) THEN
        EXECUTE 'ALTER TABLE jobs ADD CONSTRAINT jobs_ats_check CHECK (' || want || ')';
    END IF;
END $$;

-- Board discovery bookkeeping: when a board slug was last confirmed to exist by a
-- directed probe. Lets full-discovery resurrect a board that 404'd transiently.
ALTER TABLE job_boards ADD COLUMN IF NOT EXISTS last_discovered_at timestamptz;

INSERT INTO schema_meta (key, value)
VALUES ('schema_version', '3')
ON CONFLICT (key) DO UPDATE
SET value = excluded.value, updated_at = now();
