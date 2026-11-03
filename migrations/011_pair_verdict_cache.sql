-- Run in the Supabase SQL editor to make the AI's verdict on each pair of sections repeatable.
-- Without this table the backend still works: it keeps verdicts in memory only (lost when the server restarts).
--
-- One row per pair as the AI saw it, keyed by a fingerprint of the two text windows, the section names, the model's
-- ratings and the prompt version. Scanning the same text again reuses the row, so the score cannot drift between runs.
-- Only the backend (service role) uses this table.
CREATE TABLE IF NOT EXISTS public.pair_verdict_cache (
    cache_key      text PRIMARY KEY,
    prompt_version text NOT NULL,
    explanation    jsonb NOT NULL,   -- the AI's reviewed answer for the pair: severity, category, quotes, recommendations, linking sentences
    created_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.pair_verdict_cache ENABLE ROW LEVEL SECURITY;   -- no policies: browsers can never read it
GRANT ALL ON public.pair_verdict_cache TO service_role;
