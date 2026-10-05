-- 008_analysis_run_async_columns.sql
--
-- Adds the columns that the backend writes/reads on the analysis_run table
-- but which were never included in any tracked migration, causing the
-- _provision_analysis_run() insert to fail with:
--   "Database setup failed before scan could start."
--
-- Columns added:
--   status          - plain-text job-state mirror of analysis_run_status.
--                     The polling endpoint (GET /api/scans/{id}) reads this
--                     because the analysis_run_status enum has no 'failed'
--                     member (it only has 'processing' / 'completed').
--                     Values: 'processing' | 'completed' | 'failed'
--   doc_url         - Google Docs share URL stored at provision time so the
--                     scan history can display the source link without
--                     re-joining through manuscript.
--   result_json     - Full ScanResponse payload persisted by the async job
--                     wrapper (_run_scan_job) once the pipeline finishes, so
--                     the polling endpoint can return it verbatim.
--   error_message   - Human-readable failure reason written by _run_scan_job
--                     when the pipeline throws; returned by the polling
--                     endpoint when status = 'failed'.
--
-- Run in the Supabase SQL Editor BEFORE deploying the backend code that
-- performs these writes (PostgREST rejects an insert containing any column
-- it does not recognise, failing the whole row -- not just the new fields).
-- Safe to run more than once (all ALTER TABLE ADD COLUMN IF NOT EXISTS).

ALTER TABLE public.analysis_run
    ADD COLUMN IF NOT EXISTS status         text,
    ADD COLUMN IF NOT EXISTS doc_url        text,
    ADD COLUMN IF NOT EXISTS result_json    jsonb,
    ADD COLUMN IF NOT EXISTS error_message  text;

-- Optional: add a CHECK constraint so status only accepts known states.
-- Wrapped in a DO block so it is idempotent (re-running skips gracefully).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM   pg_constraint
        WHERE  conname = 'analysis_run_status_check'
           AND conrelid = 'public.analysis_run'::regclass
    ) THEN
        ALTER TABLE public.analysis_run
            ADD CONSTRAINT analysis_run_status_check
            CHECK (status IN ('processing', 'completed', 'failed'));
    END IF;
END
$$;
