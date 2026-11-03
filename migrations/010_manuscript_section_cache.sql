-- Run in the Supabase SQL editor to turn on section caching.
-- Without this table the backend still works: it parses every manuscript from scratch, as before.
--
-- One row per scanned manuscript holds its parsed sections (name + text), so scanning the same file again, or calling
-- /analyze_v7 with a manuscript_id, skips reading and parsing it. Only the backend (service role) uses this table.
CREATE TABLE IF NOT EXISTS public.manuscript_section_cache (
    manuscript_id uuid PRIMARY KEY,
    user_id       uuid NOT NULL,
    content_hash  text NOT NULL,
    structure     text,
    parsed        jsonb NOT NULL,   -- {"parsed_sections": {name: text}, "section_order": [...], "section_roles": {name: role}, "structure": "..."}
    raw_text      text NOT NULL,    -- the extracted text, kept for the citation checks
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS manuscript_section_cache_lookup
    ON public.manuscript_section_cache (user_id, content_hash, created_at DESC);

ALTER TABLE public.manuscript_section_cache ENABLE ROW LEVEL SECURITY;   -- no policies: browsers can never read it
GRANT ALL ON public.manuscript_section_cache TO service_role;
