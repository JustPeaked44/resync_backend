-- Paste as one script. PostgreSQL requires the enum addition outside the transaction.
ALTER TYPE public.credit_ledger_kind ADD VALUE IF NOT EXISTS 'adjustment';

BEGIN;

CREATE SCHEMA IF NOT EXISTS backup_012;
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['user','manuscript','analysis_run','inconsistency','recommendation','citation','credit_ledger','pymt_txn','user_device_token','subscription','subscription_plan','credit_wallet','issue_feedback','citation_crossmatch_issue','notification'] LOOP
    EXECUTE format('CREATE TABLE backup_012.%I AS SELECT * FROM public.%I', t, t);
  END LOOP;
  ALTER TABLE backup_012.analysis_run ALTER COLUMN analysis_run_status TYPE text USING analysis_run_status::text;
  ALTER TABLE backup_012.inconsistency ALTER COLUMN inconsistency_type TYPE text USING inconsistency_type::text;
  ALTER TABLE backup_012.inconsistency ALTER COLUMN inconsistency_severity TYPE text USING inconsistency_severity::text;
  ALTER TABLE backup_012.recommendation ALTER COLUMN recommendation_priority TYPE text USING recommendation_priority::text;
  ALTER TABLE backup_012.credit_ledger ALTER COLUMN kind TYPE text USING kind::text;
  IF to_regclass('public.manuscript_section_cache') IS NOT NULL THEN
    CREATE TABLE backup_012.manuscript_section_cache AS SELECT * FROM public.manuscript_section_cache;
  END IF;
  IF to_regclass('public.pair_verdict_cache') IS NOT NULL THEN
    CREATE TABLE backup_012.pair_verdict_cache AS SELECT * FROM public.pair_verdict_cache;
  END IF;
END $$;

-- Preserve exact rows before dropping source columns and tables.
ALTER TABLE public.pymt_txn ADD COLUMN user_id uuid;
UPDATE public.pymt_txn p SET user_id = s.user_id FROM public.subscription s WHERE p.subscription_id = s.subscription_id;
DO $$ DECLARE n integer; BEGIN
  SELECT count(*) INTO n FROM public.pymt_txn WHERE user_id IS NULL;
  RAISE NOTICE '012 payment rows without owner: %', n;
  IF n <> 0 THEN RAISE EXCEPTION '012: % payment rows have no user_id', n; END IF;
END $$;
ALTER TABLE public.pymt_txn ALTER COLUMN user_id SET NOT NULL;

ALTER TABLE public.inconsistency ADD COLUMN inconsistency_is_helpful boolean;
ALTER TABLE public.inconsistency ADD COLUMN inconsistency_created_at timestamptz NOT NULL DEFAULT now();
UPDATE public.inconsistency i SET inconsistency_is_helpful = f.is_helpful
FROM (SELECT DISTINCT ON (inconsistency_id) inconsistency_id, is_helpful
      FROM public.issue_feedback ORDER BY inconsistency_id, created_at DESC, feedback_id DESC) f
WHERE f.inconsistency_id = i.inconsistency_id;

-- Validate before casting, so a bad row reports a count and rolls back cleanly.
DO $$ DECLARE r record; n integer := 0; BEGIN
  FOR r IN SELECT analysis_run_id, analysis_run_template_toc FROM public.analysis_run
           WHERE nullif(btrim(analysis_run_template_toc), '') IS NOT NULL LOOP
    BEGIN PERFORM r.analysis_run_template_toc::jsonb;
    EXCEPTION WHEN OTHERS THEN n := n + 1;
    END;
  END LOOP;
  RAISE NOTICE '012 invalid template_toc rows: %', n;
  IF n <> 0 THEN RAISE EXCEPTION '012: % template_toc rows cannot cast to jsonb', n; END IF;
END $$;
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_template_toc TYPE jsonb
  USING nullif(btrim(analysis_run_template_toc), '')::jsonb;
ALTER TABLE public.analysis_run ADD COLUMN analysis_run_is_unlocked boolean NOT NULL DEFAULT false;
UPDATE public.analysis_run SET analysis_run_is_unlocked =
  result_json IS NOT NULL AND (NOT result_json ? 'access' OR result_json->'access'->>'unlocked' = 'true');

ALTER TABLE public.analysis_run DROP CONSTRAINT IF EXISTS analysis_run_status_check;
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_status DROP DEFAULT;
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_status TYPE text USING analysis_run_status::text;
UPDATE public.analysis_run SET analysis_run_status = CASE
  WHEN coalesce(status, analysis_run_status) = 'complete' THEN 'completed'
  ELSE coalesce(status, analysis_run_status) END;
DO $$ DECLARE n integer; BEGIN
  SELECT count(*) INTO n FROM public.analysis_run WHERE analysis_run_status NOT IN ('processing','completed','failed');
  RAISE NOTICE '012 unknown analysis statuses: %', n;
  IF n <> 0 THEN RAISE EXCEPTION '012: % unknown analysis statuses', n; END IF;
END $$;
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_status SET DEFAULT 'processing';
ALTER TABLE public.analysis_run ADD CONSTRAINT analysis_run_status_check
  CHECK (analysis_run_status IN ('processing','completed','failed'));

ALTER TABLE public."user" ADD COLUMN user_avatar_url text;
ALTER TABLE public.manuscript ADD COLUMN manuscript_content_hash text;
ALTER TABLE public.manuscript ADD COLUMN manuscript_structure text;
ALTER TABLE public.manuscript ADD COLUMN manuscript_parsed_sections jsonb;
ALTER TABLE public.manuscript ADD COLUMN manuscript_raw_text text;
DO $$ BEGIN
  IF to_regclass('public.manuscript_section_cache') IS NOT NULL THEN
    EXECUTE 'UPDATE public.manuscript m SET manuscript_content_hash=c.content_hash, manuscript_structure=c.structure, manuscript_parsed_sections=c.parsed, manuscript_raw_text=c.raw_text FROM public.manuscript_section_cache c WHERE m.manuscript_id=c.manuscript_id';
    EXECUTE 'DROP TABLE public.manuscript_section_cache';
  END IF;
END $$;
ALTER TABLE public.recommendation ADD COLUMN recommendation_created_at timestamptz NOT NULL DEFAULT now();

-- One adjustment makes the ledger sum equal each stored wallet balance.
INSERT INTO public.credit_ledger (user_id, kind, delta, balance_after, note)
SELECT w.user_id, 'adjustment', w.balance - coalesce(l.total,0), w.balance, 'Opening balance (migration 012)'
FROM public.credit_wallet w
LEFT JOIN (SELECT user_id, sum(delta) total FROM public.credit_ledger GROUP BY user_id) l USING (user_id)
WHERE w.balance <> coalesce(l.total,0);
DO $$ DECLARE n integer; BEGIN
  SELECT count(*) INTO n FROM public."user" u LEFT JOIN public.credit_wallet w USING (user_id)
  WHERE coalesce(w.balance,0) <> (SELECT coalesce(sum(delta),0) FROM public.credit_ledger l WHERE l.user_id=u.user_id);
  RAISE NOTICE '012 wallet/ledger mismatches after adjustment: %', n;
  IF n <> 0 THEN RAISE EXCEPTION '012: % wallet/ledger mismatches', n; END IF;
END $$;

-- Renames preserve values and update dependent policy expressions.
ALTER TABLE public."user" RENAME COLUMN institution TO user_institution;
ALTER TABLE public."user" RENAME COLUMN academic_role TO user_academic_role;
ALTER TABLE public."user" RENAME COLUMN bio TO user_bio;
ALTER TABLE public."user" RENAME COLUMN notif_enabled TO user_notif_enabled;
ALTER TABLE public."user" RENAME COLUMN created_at TO user_created_at;
ALTER TABLE public."user" RENAME COLUMN updated_at TO user_updated_at;
ALTER TABLE public.manuscript RENAME COLUMN created_at TO manuscript_created_at;
ALTER TABLE public.analysis_run RENAME COLUMN error_message TO analysis_run_error_message;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_coherence_score TO analysis_run_score;
ALTER TABLE public.analysis_run RENAME COLUMN functional_metric_band TO analysis_run_band;
ALTER TABLE public.analysis_run RENAME COLUMN citation_integrity_score TO analysis_run_citation_score;
ALTER TABLE public.analysis_run RENAME COLUMN ai_text_indicator_score TO analysis_run_ai_text_score;
ALTER TABLE public.analysis_run RENAME COLUMN sections_analyzed TO analysis_run_sections_analyzed;
ALTER TABLE public.analysis_run RENAME COLUMN inconsistencies_found TO analysis_run_inconsistency_count;
ALTER TABLE public.analysis_run RENAME COLUMN citations_audited TO analysis_run_citation_count;
ALTER TABLE public.analysis_run RENAME COLUMN result_json TO analysis_run_result_json;
ALTER TABLE public.analysis_run RENAME COLUMN created_at TO analysis_run_created_at;
ALTER TABLE public.inconsistency RENAME COLUMN primary_section_name TO inconsistency_section_a;
ALTER TABLE public.inconsistency RENAME COLUMN conflicting_section_name TO inconsistency_section_b;
ALTER TABLE public.inconsistency RENAME COLUMN coherence_score TO inconsistency_pair_score;
ALTER TABLE public.inconsistency RENAME COLUMN evidence_a TO inconsistency_evidence_a;
ALTER TABLE public.inconsistency RENAME COLUMN evidence_b TO inconsistency_evidence_b;
ALTER TABLE public.inconsistency RENAME COLUMN evidence_verified TO inconsistency_evidence_verified;
ALTER TABLE public.recommendation RENAME COLUMN target_section_name TO recommendation_section;
ALTER TABLE public.credit_ledger RENAME COLUMN ledger_id TO credit_ledger_id;
ALTER TABLE public.credit_ledger RENAME COLUMN kind TO credit_ledger_kind;
ALTER TABLE public.credit_ledger RENAME COLUMN delta TO credit_ledger_delta;
ALTER TABLE public.credit_ledger RENAME COLUMN balance_after TO credit_ledger_balance_after;
ALTER TABLE public.credit_ledger RENAME COLUMN idempotency_key TO credit_ledger_idempotency_key;
ALTER TABLE public.credit_ledger RENAME COLUMN note TO credit_ledger_note;
ALTER TABLE public.credit_ledger RENAME COLUMN created_at TO credit_ledger_created_at;
ALTER TABLE public.user_device_token RENAME COLUMN token_id TO user_device_token_id;
ALTER TABLE public.user_device_token RENAME COLUMN device_onesignal_player_id TO user_device_token_player_id;
ALTER TABLE public.user_device_token RENAME COLUMN device_platform TO user_device_token_platform;
ALTER TABLE public.user_device_token RENAME COLUMN registered_at TO user_device_token_created_at;

-- Remove duplicate foreign keys; retained constraints match the target actions.
ALTER TABLE public.analysis_run DROP CONSTRAINT fk_analysis_run_manuscript;
ALTER TABLE public.analysis_run DROP CONSTRAINT fk_analysis_run_user;
ALTER TABLE public.citation DROP CONSTRAINT fk_citation_analysis_run;
ALTER TABLE public.inconsistency DROP CONSTRAINT inconsistency_analysis_run_id_fkey;
ALTER TABLE public.manuscript DROP CONSTRAINT manuscript_user_id_fkey;
ALTER TABLE public.recommendation DROP CONSTRAINT recommendation_analysis_run_id_fkey;
ALTER TABLE public.credit_ledger DROP CONSTRAINT credit_ledger_user_id_fkey;
ALTER TABLE public.credit_ledger RENAME CONSTRAINT fk_credit_ledger_user TO credit_ledger_user_id_fkey;
ALTER TABLE public.credit_ledger RENAME CONSTRAINT credit_ledger_idempotency_key_key TO credit_ledger_credit_ledger_idempotency_key_key;
ALTER TABLE public.credit_ledger RENAME CONSTRAINT credit_ledger_pkey TO credit_ledger_credit_ledger_id_pkey;
ALTER TABLE public.user_device_token RENAME CONSTRAINT user_device_token_pkey TO user_device_token_user_device_token_id_pkey;
ALTER TABLE public.analysis_run RENAME CONSTRAINT analysis_run_analysis_run_coherence_score_check TO analysis_run_analysis_run_score_check;

-- Drop dependent index before deleted_at, then obsolete fields.
DROP INDEX public.idx_manuscript_active;
ALTER TABLE public.manuscript DROP COLUMN deleted_at, DROP COLUMN manuscript_research_type,
  DROP COLUMN manuscript_status, DROP COLUMN manuscript_upload_mode, DROP COLUMN updated_at;
ALTER TABLE public.analysis_run DROP COLUMN ai_text_indicator_json, DROP COLUMN analysis_run_started_at,
  DROP COLUMN analysis_run_style_reference_url, DROP COLUMN biggest_lever, DROP COLUMN biggest_lever_detail,
  DROP COLUMN cross_chapter_coherence_score, DROP COLUMN doc_url, DROP COLUMN functional_metric_score,
  DROP COLUMN has_all_required_sections, DROP COLUMN missing_sections, DROP COLUMN score_breakdown_json,
  DROP COLUMN structural_completeness_score, DROP COLUMN status;
ALTER TABLE public.inconsistency DROP COLUMN inconsistency_description, DROP COLUMN inconsistency_status,
  DROP COLUMN inconsistency_text_offset_end, DROP COLUMN inconsistency_text_offset_start,
  DROP COLUMN objectives_unaddressed;
ALTER TABLE public.pymt_txn DROP COLUMN subscription_id;
ALTER TABLE public.pymt_txn ADD CONSTRAINT pymt_txn_user_id_fkey FOREIGN KEY (user_id)
  REFERENCES public."user"(user_id) ON DELETE CASCADE;

-- New IDs must be unique for the backend's upsert. The full before image is in backup_012.
DO $$ DECLARE n integer; BEGIN
  WITH ranked AS (SELECT user_device_token_id, row_number() OVER
    (PARTITION BY user_device_token_player_id ORDER BY user_device_token_created_at DESC, user_device_token_id DESC) rn
    FROM public.user_device_token)
  DELETE FROM public.user_device_token t USING ranked r WHERE t.user_device_token_id=r.user_device_token_id AND r.rn>1;
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE '012 duplicate device-token rows removed: %', n;
END $$;
ALTER TABLE public.user_device_token ADD CONSTRAINT user_device_token_user_device_token_player_id_key UNIQUE (user_device_token_player_id);
ALTER TABLE public.credit_ledger ALTER COLUMN credit_ledger_kind TYPE public.credit_ledger_kind USING credit_ledger_kind::text::public.credit_ledger_kind;
ALTER TABLE public.credit_ledger ALTER COLUMN credit_ledger_created_at SET DEFAULT now();
DROP INDEX public.idx_credit_ledger_user_id;
CREATE INDEX credit_ledger_user_id_created_at_idx ON public.credit_ledger(user_id, credit_ledger_created_at DESC);

-- Drop the obsolete sign-up trigger before its referenced tables.
DROP TRIGGER IF EXISTS on_auth_user_created_credit_wallet ON auth.users;
DROP FUNCTION public.handle_new_user_credit_wallet();
DROP TABLE public.issue_feedback, public.citation_crossmatch_issue, public.notification,
  public.credit_wallet, public.subscription, public.subscription_plan;
DROP TYPE public.run_status_enum;

DO $$ BEGIN
  IF to_regclass('public.pair_verdict_cache') IS NOT NULL THEN
    ALTER TABLE public.pair_verdict_cache RENAME COLUMN cache_key TO pair_verdict_cache_key;
    ALTER TABLE public.pair_verdict_cache RENAME COLUMN prompt_version TO pair_verdict_cache_prompt_version;
    ALTER TABLE public.pair_verdict_cache RENAME COLUMN explanation TO pair_verdict_cache_explanation;
    ALTER TABLE public.pair_verdict_cache RENAME COLUMN created_at TO pair_verdict_cache_created_at;
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS public.pair_verdict_cache (
  pair_verdict_cache_key text PRIMARY KEY,
  pair_verdict_cache_prompt_version text NOT NULL,
  pair_verdict_cache_explanation jsonb NOT NULL,
  pair_verdict_cache_created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE public.pair_verdict_cache ENABLE ROW LEVEL SECURITY;
GRANT ALL ON public.pair_verdict_cache TO service_role;

CREATE OR REPLACE FUNCTION public.get_credit_balance(p_user_id uuid) RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path TO 'public' AS $$
  SELECT coalesce(sum(credit_ledger_delta),0)::integer FROM public.credit_ledger WHERE user_id=p_user_id
$$;
REVOKE ALL ON FUNCTION public.get_credit_balance(uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.get_credit_balance(uuid) TO service_role;

CREATE OR REPLACE FUNCTION public.handle_new_user() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public' AS $$
BEGIN
  INSERT INTO public."user" (user_id,user_first_name,user_last_name,user_email)
  VALUES (NEW.id,coalesce(NEW.raw_user_meta_data->>'first_name','Student'),
          coalesce(NEW.raw_user_meta_data->>'last_name','Researcher'),NEW.email)
  ON CONFLICT (user_id) DO UPDATE SET user_email=EXCLUDED.user_email,user_updated_at=now();
  INSERT INTO public.credit_ledger (user_id,credit_ledger_kind,credit_ledger_delta,credit_ledger_balance_after,credit_ledger_note)
  VALUES (NEW.id,'grant',1,1,'Free signup credit');
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION public.debit_scan_credit(p_user_id uuid,p_analysis_run_id uuid,p_idempotency_key text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public' AS $$
DECLARE v_balance integer; BEGIN
  PERFORM 1 FROM public."user" WHERE user_id=p_user_id FOR UPDATE;
  SELECT credit_ledger_balance_after INTO v_balance FROM public.credit_ledger
    WHERE credit_ledger_idempotency_key=p_idempotency_key;
  IF FOUND THEN RETURN v_balance; END IF;
  v_balance:=public.get_credit_balance(p_user_id);
  IF v_balance < 1 THEN RAISE EXCEPTION 'INSUFFICIENT_CREDITS' USING ERRCODE='P0001'; END IF;
  v_balance:=v_balance-1;
  INSERT INTO public.credit_ledger (user_id,credit_ledger_kind,credit_ledger_delta,credit_ledger_balance_after,analysis_run_id,credit_ledger_idempotency_key)
  VALUES (p_user_id,'debit',-1,v_balance,p_analysis_run_id,p_idempotency_key);
  RETURN v_balance;
END $$;

CREATE OR REPLACE FUNCTION public.refund_scan_credit(p_user_id uuid,p_analysis_run_id uuid,p_idempotency_key text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public' AS $$
DECLARE v_balance integer; BEGIN
  PERFORM 1 FROM public."user" WHERE user_id=p_user_id FOR UPDATE;
  SELECT credit_ledger_balance_after INTO v_balance FROM public.credit_ledger
    WHERE credit_ledger_idempotency_key=p_idempotency_key;
  IF FOUND THEN RETURN v_balance; END IF;
  v_balance:=public.get_credit_balance(p_user_id)+1;
  INSERT INTO public.credit_ledger (user_id,credit_ledger_kind,credit_ledger_delta,credit_ledger_balance_after,analysis_run_id,credit_ledger_idempotency_key)
  VALUES (p_user_id,'refund',1,v_balance,p_analysis_run_id,p_idempotency_key);
  RETURN v_balance;
END $$;

CREATE OR REPLACE FUNCTION public.purchase_credits(p_user_id uuid,p_amount integer,p_pymt_txn_id uuid,p_idempotency_key text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public' AS $$
DECLARE v_balance integer; BEGIN
  IF p_amount <= 0 THEN RAISE EXCEPTION 'INVALID_AMOUNT' USING ERRCODE='P0001'; END IF;
  PERFORM 1 FROM public."user" WHERE user_id=p_user_id FOR UPDATE;
  SELECT credit_ledger_balance_after INTO v_balance FROM public.credit_ledger
    WHERE credit_ledger_idempotency_key=p_idempotency_key;
  IF FOUND THEN RETURN v_balance; END IF;
  v_balance:=public.get_credit_balance(p_user_id)+p_amount;
  INSERT INTO public.credit_ledger (user_id,credit_ledger_kind,credit_ledger_delta,credit_ledger_balance_after,pymt_txn_id,credit_ledger_idempotency_key)
  VALUES (p_user_id,'purchase',p_amount,v_balance,p_pymt_txn_id,p_idempotency_key);
  RETURN v_balance;
END $$;

CREATE OR REPLACE FUNCTION public.unlock_scan_report(p_user_id uuid,p_analysis_run_id uuid,p_charge boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'public' AS $$
DECLARE v_result jsonb; v_unlocked boolean; v_balance integer; BEGIN
  PERFORM 1 FROM public."user" WHERE user_id=p_user_id FOR UPDATE;
  SELECT analysis_run_result_json,analysis_run_is_unlocked INTO v_result,v_unlocked
    FROM public.analysis_run WHERE analysis_run_id=p_analysis_run_id AND user_id=p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'SCAN_NOT_FOUND' USING ERRCODE='P0001'; END IF;
  IF v_result IS NULL THEN RAISE EXCEPTION 'SCAN_NOT_READY' USING ERRCODE='P0001'; END IF;
  v_balance:=public.get_credit_balance(p_user_id);
  IF v_unlocked THEN RETURN jsonb_build_object('unlocked',true,'charged',false,'credits_remaining',v_balance); END IF;
  IF p_charge THEN
    IF v_balance<1 THEN RAISE EXCEPTION 'INSUFFICIENT_CREDITS' USING ERRCODE='P0001'; END IF;
    v_balance:=v_balance-1;
    INSERT INTO public.credit_ledger (user_id,credit_ledger_kind,credit_ledger_delta,credit_ledger_balance_after,analysis_run_id,credit_ledger_idempotency_key,credit_ledger_note)
    VALUES (p_user_id,'debit',-1,v_balance,p_analysis_run_id,'scan_unlock:'||p_analysis_run_id::text,'Full report unlock');
  END IF;
  UPDATE public.analysis_run SET analysis_run_is_unlocked=true WHERE analysis_run_id=p_analysis_run_id;
  RETURN jsonb_build_object('unlocked',true,'charged',p_charge,'credits_remaining',v_balance);
END $$;

REVOKE ALL ON FUNCTION public.debit_scan_credit(uuid,uuid,text),public.refund_scan_credit(uuid,uuid,text),
  public.purchase_credits(uuid,integer,uuid,text),public.unlock_scan_report(uuid,uuid,boolean)
  FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.debit_scan_credit(uuid,uuid,text),public.refund_scan_credit(uuid,uuid,text),
  public.purchase_credits(uuid,integer,uuid,text),public.unlock_scan_report(uuid,uuid,boolean) TO service_role;

COMMIT;
