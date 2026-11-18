-- Read-only inventory before migration 017. Every expected object must be present;
-- function privilege rows must show false for anon and authenticated.
WITH expected_tables(name) AS (VALUES ('user'),('manuscript'),('analysis_run'),('inconsistency'),('recommendation'),('citation'),('credit_ledger'),('pymt_txn'),('user_device_token'),('subscription'),('subscription_plan'),('credit_wallet'),('issue_feedback'),('citation_crossmatch_issue'),('notification'),('payment_proof')),
expected_columns(tbl,col) AS (VALUES ('pymt_txn','user_id'),('pymt_txn','subscription_id'),('payment_proof','proof_data'),('analysis_run','status'),('analysis_run','result_json'),('credit_wallet','balance')),
expected_functions(sig) AS (VALUES ('public.email_registered(text)'),('public.handle_new_user_credit_wallet()'),('public.purchase_credits(uuid,integer,uuid,text)'),('public.debit_scan_credit(uuid,uuid,text)'),('public.refund_scan_credit(uuid,uuid,text)'),('public.unlock_scan_report(uuid,uuid,boolean)')),
inventory AS (
 SELECT 'table' AS object_type, name AS object_name, to_regclass(format('public.%I',name)) IS NOT NULL AS present, NULL::boolean AS anon_execute, NULL::boolean AS authenticated_execute FROM expected_tables
 UNION ALL SELECT 'column',tbl||'.'||col, EXISTS(SELECT 1 FROM information_schema.columns c WHERE c.table_schema='public' AND c.table_name=tbl AND c.column_name=col),NULL,NULL FROM expected_columns
 UNION ALL SELECT 'function',sig, to_regprocedure(sig) IS NOT NULL, CASE WHEN to_regprocedure(sig) IS NOT NULL THEN has_function_privilege('anon',to_regprocedure(sig),'EXECUTE') END, CASE WHEN to_regprocedure(sig) IS NOT NULL THEN has_function_privilege('authenticated',to_regprocedure(sig),'EXECUTE') END FROM expected_functions
)
SELECT * FROM inventory ORDER BY object_type,object_name;

-- Only run these two checks if the inventory shows these tables are present.
-- Duplicate device IDs must be zero; move legacy proof rows with 017b after 017a.
SELECT 'legacy_proof_rows' AS check_name, count(*) AS rows_requiring_review FROM public.payment_proof
UNION ALL
SELECT 'duplicate_device_ids', count(*) FROM (SELECT device_onesignal_player_id FROM public.user_device_token GROUP BY device_onesignal_player_id HAVING count(*) > 1) duplicates;
