-- 007_xai_evidence.sql
-- The XAI reasoning service has always computed evidence_a, evidence_b,
-- and objectives_unaddressed and returned them to the client on a live
-- scan -- but db_service.py never wrote them to public.inconsistency, so
-- reloading a scan from history silently lost every evidence quote and
-- unaddressed-objective list. This also widens coherence_score to
-- nullable: a deterministic numeric-audit finding has no coherence score
-- at all, and a hardcoded 0.0 was indistinguishable from a real (and
-- catastrophic) score of zero.
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
