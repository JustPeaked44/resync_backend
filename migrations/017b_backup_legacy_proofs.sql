-- Run after 017a and before 017, while proof uploads are paused.
BEGIN;
CREATE SCHEMA IF NOT EXISTS backup_012;
CREATE TABLE IF NOT EXISTS backup_012.payment_proof_legacy (
  LIKE public.payment_proof INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES,
  moved_at timestamptz NOT NULL DEFAULT now()
);
LOCK TABLE public.payment_proof IN ACCESS EXCLUSIVE MODE;
DO $$
DECLARE source_count bigint; matched_count bigint;
BEGIN
  SELECT count(*) INTO source_count FROM public.payment_proof;
  INSERT INTO backup_012.payment_proof_legacy
    (proof_id,pymt_txn_id,user_id,method,reference_number,payer_name,file_name,content_type,proof_data,created_at)
  SELECT * FROM public.payment_proof ON CONFLICT (proof_id) DO NOTHING;
  SELECT count(*) INTO matched_count FROM public.payment_proof p
  JOIN backup_012.payment_proof_legacy b USING (proof_id)
  WHERE (p.pymt_txn_id,p.user_id,p.method,p.reference_number,p.payer_name,p.file_name,p.content_type,p.proof_data,p.created_at)
    IS NOT DISTINCT FROM
    (b.pymt_txn_id,b.user_id,b.method,b.reference_number,b.payer_name,b.file_name,b.content_type,b.proof_data,b.created_at);
  IF matched_count <> source_count THEN
    RAISE EXCEPTION 'legacy proof backup mismatch: % source rows, % matching backup rows', source_count, matched_count;
  END IF;
  DELETE FROM public.payment_proof;
END $$;
COMMIT;
