-- Run in the Supabase SQL editor (safe to run twice).
-- The credit functions below are SECURITY DEFINER and take any user id. Without a REVOKE, Postgres lets PUBLIC execute them,
-- and Supabase exposes public-schema functions as /rest/v1/rpc/<name> to anyone holding the anon key (it ships in the website
-- bundle) or a logged-in session. That would let a caller debit, refund or credit ANY user's wallet directly, around the backend.
-- Only the backend (service_role) may call them.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT p.oid::regprocedure AS sig
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND p.proname IN ('debit_scan_credit', 'refund_scan_credit', 'purchase_credits', 'unlock_scan_report', 'email_registered')
  LOOP
    EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, anon, authenticated', r.sig);
    EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role', r.sig);
    RAISE NOTICE 'locked %', r.sig;
  END LOOP;
END $$;

-- Check afterwards (read-only): every row should show false, false for anon_can_run and signed_in_can_run.
-- SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args,
--        has_function_privilege('anon', p.oid, 'execute') AS anon_can_run,
--        has_function_privilege('authenticated', p.oid, 'execute') AS signed_in_can_run
-- FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'public' AND p.prosecdef AND p.prorettype <> 'trigger'::regtype ORDER BY 1;
