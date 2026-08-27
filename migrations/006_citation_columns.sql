-- 006_citation_columns.sql
-- Widens the citation table beyond the current two-field
-- (citation_raw_reference_text, citation_is_accessible) shape so the
-- rewritten citation service can persist real segmentation, link, and
-- verification detail instead of discarding it after the HTTP check.
-- Run in the Supabase SQL Editor after 005_scoring_columns.sql.

ALTER TABLE public.citation
    ADD COLUMN IF NOT EXISTS citation_entry_index integer,
    ADD COLUMN IF NOT EXISTS citation_authors_parsed text,
    ADD COLUMN IF NOT EXISTS citation_year_parsed integer,
    ADD COLUMN IF NOT EXISTS citation_links jsonb DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS citation_primary_link text,
    ADD COLUMN IF NOT EXISTS citation_status text DEFAULT 'no_link',
    ADD COLUMN IF NOT EXISTS citation_verification_tier text,
    ADD COLUMN IF NOT EXISTS citation_crossref_title text,
    ADD COLUMN IF NOT EXISTS citation_crossref_year integer,
    ADD COLUMN IF NOT EXISTS citation_title_match_score real,
    ADD COLUMN IF NOT EXISTS citation_http_status_code integer,
    ADD COLUMN IF NOT EXISTS citation_is_cited_in_text boolean,
    ADD COLUMN IF NOT EXISTS citation_error_detail text;

DO $$ BEGIN
    ALTER TABLE public.citation
        ADD CONSTRAINT citation_status_check CHECK (
            citation_status IN (
                'verified_metadata', 'metadata_mismatch', 'accessible',
                'bot_wall', 'broken', 'unknown_error', 'no_link'
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- In-text citation <-> reference-list cross-matching findings. One row
-- per orphan in-text citation or uncited reference detected for a run.
CREATE TABLE IF NOT EXISTS public.citation_crossmatch_issue (
    issue_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    analysis_run_id     uuid NOT NULL REFERENCES public.analysis_run(analysis_run_id) ON DELETE CASCADE,
    issue_type          text NOT NULL CHECK (issue_type IN ('orphan_intext', 'uncited_reference')),
    surname             text,
    year                integer,
    context_sentence    text,
    reference_raw_text  text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_citation_crossmatch_run_id
    ON public.citation_crossmatch_issue(analysis_run_id);

-- New tables created via the SQL Editor don't inherit the service_role
-- grants the Phase 1 init script gave the original 10 tables, so the
-- backend's Supabase client (which authenticates as service_role) gets
-- "permission denied for table citation_crossmatch_issue" on insert
-- without this.
GRANT SELECT, INSERT ON public.citation_crossmatch_issue TO service_role;
