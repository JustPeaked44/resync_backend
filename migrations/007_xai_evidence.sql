-- 007_xai_evidence.sql
-- Historical migration: adds evidence columns used by earlier scan results
-- and permits legacy findings without a coherence score. Current v7 findings
-- use verified evidence and retain stable IDs; old columns remain for rows
-- written before v7.
-- Run in the Supabase SQL Editor after 006_citation_columns.sql, and
-- BEFORE deploying the corresponding db_service.py change -- PostgREST
-- rejects an insert containing any column it doesn't recognize, failing
-- the whole batch, not just the new fields.

ALTER TABLE public.inconsistency
    ADD COLUMN IF NOT EXISTS evidence_a text,
    ADD COLUMN IF NOT EXISTS evidence_b text,
    ADD COLUMN IF NOT EXISTS evidence_verified boolean DEFAULT false,
    ADD COLUMN IF NOT EXISTS objectives_unaddressed jsonb DEFAULT '[]'::jsonb;

ALTER TABLE public.inconsistency
    ALTER COLUMN coherence_score DROP NOT NULL;
