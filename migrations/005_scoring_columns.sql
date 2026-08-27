-- 005_scoring_columns.sql
-- Adds the three-criteria functional metric score to analysis_run.
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

-- overall_coherence_score / analysis_run_coherence_score remain the
-- authoritative columns the existing frontend history query and the
-- mobile app already read. The pipeline sets both of those to the new
-- functional_metric_score so nothing downstream needs to change to keep
-- working, while functional_metric_score plus the three sub-scores give
-- the new criteria breakdown UI something to render.
