-- 005_scoring_columns.sql
-- Historical scoring columns retained for existing analysis_run rows.
-- Run in the Supabase SQL Editor after 004_credits.sql.

ALTER TABLE public.analysis_run
    ADD COLUMN IF NOT EXISTS structural_completeness_score real,
    ADD COLUMN IF NOT EXISTS cross_chapter_coherence_score real,
    ADD COLUMN IF NOT EXISTS citation_integrity_score real,
    ADD COLUMN IF NOT EXISTS functional_metric_score real,
    ADD COLUMN IF NOT EXISTS functional_metric_band text,
    ADD COLUMN IF NOT EXISTS biggest_lever text,
    ADD COLUMN IF NOT EXISTS biggest_lever_detail jsonb,
    ADD COLUMN IF NOT EXISTS score_breakdown_json jsonb,
    ADD COLUMN IF NOT EXISTS ai_text_indicator_score real,
    ADD COLUMN IF NOT EXISTS ai_text_indicator_json jsonb;

-- Current v7 scans store the exact weighted score in functional_metric_score
-- and result_json; the legacy integer analysis_run_coherence_score is rounded.
-- Other legacy columns remain available for older rows.
