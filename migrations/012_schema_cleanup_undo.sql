-- Restore the live pre-012 public schema in place. backup_012 is retained for audit.
BEGIN;

-- Keep balances earned or spent after 012 before replacing the ledger functions.
CREATE TABLE backup_012.undo_wallet_balances AS
SELECT u.user_id, public.get_credit_balance(u.user_id) AS balance
FROM public."user" u;
-- Pre-012 has no place for these columns or the pair cache; keep their current values in backup_012.
CREATE TABLE backup_012.undo_new_fields AS
SELECT 'user'::text AS source_table,user_id::text AS row_id,
  jsonb_build_object('user_avatar_url',user_avatar_url) AS value FROM public."user"
UNION ALL SELECT 'manuscript',manuscript_id::text,
  jsonb_build_object('manuscript_content_hash',manuscript_content_hash,'manuscript_structure',manuscript_structure,
    'manuscript_parsed_sections',manuscript_parsed_sections,'manuscript_raw_text',manuscript_raw_text)
  FROM public.manuscript
UNION ALL SELECT 'recommendation',recommendation_id::text,
  jsonb_build_object('recommendation_created_at',recommendation_created_at) FROM public.recommendation
UNION ALL SELECT 'inconsistency',inconsistency_id::text,
  jsonb_build_object('inconsistency_created_at',inconsistency_created_at) FROM public.inconsistency;
CREATE TABLE backup_012.undo_pair_verdict_cache AS SELECT * FROM public.pair_verdict_cache;
CREATE TABLE backup_012.undo_feedback AS
SELECT i.inconsistency_id,r.user_id,i.inconsistency_is_helpful AS is_helpful,
  i.inconsistency_created_at AS created_at
FROM public.inconsistency i JOIN public.analysis_run r USING (analysis_run_id)
WHERE i.inconsistency_is_helpful IS NOT NULL;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
DROP FUNCTION public.get_credit_balance(uuid);
DROP TABLE public.pair_verdict_cache;

ALTER TABLE public.user_device_token DROP CONSTRAINT user_device_token_user_device_token_player_id_key;
ALTER TABLE public.user_device_token RENAME CONSTRAINT user_device_token_user_device_token_id_pkey TO user_device_token_pkey;
ALTER TABLE public.user_device_token RENAME COLUMN user_device_token_id TO token_id;
ALTER TABLE public.user_device_token RENAME COLUMN user_device_token_player_id TO device_onesignal_player_id;
ALTER TABLE public.user_device_token RENAME COLUMN user_device_token_platform TO device_platform;
ALTER TABLE public.user_device_token RENAME COLUMN user_device_token_created_at TO registered_at;
INSERT INTO public.user_device_token SELECT b.* FROM backup_012.user_device_token b
ON CONFLICT (token_id) DO NOTHING;

ALTER TABLE public."user" RENAME COLUMN user_institution TO institution;
ALTER TABLE public."user" RENAME COLUMN user_academic_role TO academic_role;
ALTER TABLE public."user" RENAME COLUMN user_bio TO bio;
ALTER TABLE public."user" RENAME COLUMN user_notif_enabled TO notif_enabled;
ALTER TABLE public."user" RENAME COLUMN user_created_at TO created_at;
ALTER TABLE public."user" RENAME COLUMN user_updated_at TO updated_at;
ALTER TABLE public."user" DROP COLUMN user_avatar_url;

ALTER TABLE public.manuscript RENAME COLUMN manuscript_created_at TO created_at;
ALTER TABLE public.manuscript ADD COLUMN manuscript_research_type varchar(50) NOT NULL DEFAULT 'Quantitative';
ALTER TABLE public.manuscript ADD COLUMN manuscript_upload_mode varchar(50) NOT NULL DEFAULT 'Google Docs Link';
ALTER TABLE public.manuscript ADD COLUMN manuscript_status varchar(50) NOT NULL DEFAULT 'active';
ALTER TABLE public.manuscript ADD COLUMN deleted_at timestamptz;
ALTER TABLE public.manuscript ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
UPDATE public.manuscript m SET manuscript_research_type=b.manuscript_research_type,
  manuscript_upload_mode=b.manuscript_upload_mode, manuscript_status=b.manuscript_status,
  deleted_at=b.deleted_at, updated_at=b.updated_at
FROM backup_012.manuscript b WHERE m.manuscript_id=b.manuscript_id;
ALTER TABLE public.manuscript DROP COLUMN manuscript_content_hash, DROP COLUMN manuscript_structure,
  DROP COLUMN manuscript_parsed_sections, DROP COLUMN manuscript_raw_text;
CREATE INDEX idx_manuscript_active ON public.manuscript(user_id) WHERE deleted_at IS NULL;

ALTER TABLE public.analysis_run DROP CONSTRAINT analysis_run_status_check;
ALTER TABLE public.analysis_run ADD COLUMN status text;
UPDATE public.analysis_run r SET status=coalesce(b.status,r.analysis_run_status)
FROM backup_012.analysis_run b WHERE r.analysis_run_id=b.analysis_run_id;
UPDATE public.analysis_run SET status=analysis_run_status WHERE status IS NULL;
-- The old CHECK forbids 'complete', so normalized current state wins for that legacy spelling.
UPDATE public.analysis_run SET status='completed' WHERE status='complete';
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_status DROP DEFAULT;
CREATE TYPE public.run_status_enum AS ENUM ('processing','completed','failed');
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_status TYPE public.run_status_enum
  USING analysis_run_status::public.run_status_enum;
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_status SET DEFAULT 'processing'::public.run_status_enum;
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_template_toc TYPE text USING analysis_run_template_toc::text;
UPDATE public.analysis_run r SET analysis_run_template_toc=b.analysis_run_template_toc
FROM backup_012.analysis_run b WHERE r.analysis_run_id=b.analysis_run_id;
UPDATE public.analysis_run SET analysis_run_result_json=jsonb_set(analysis_run_result_json,'{access,unlocked}','true'::jsonb)
WHERE analysis_run_is_unlocked AND analysis_run_result_json ? 'access';
ALTER TABLE public.analysis_run ADD COLUMN analysis_run_started_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.analysis_run ADD COLUMN analysis_run_style_reference_url text;
ALTER TABLE public.analysis_run ADD COLUMN missing_sections jsonb;
ALTER TABLE public.analysis_run ADD COLUMN has_all_required_sections boolean;
ALTER TABLE public.analysis_run ADD COLUMN structural_completeness_score real;
ALTER TABLE public.analysis_run ADD COLUMN cross_chapter_coherence_score real;
ALTER TABLE public.analysis_run ADD COLUMN functional_metric_score real;
ALTER TABLE public.analysis_run ADD COLUMN biggest_lever text;
ALTER TABLE public.analysis_run ADD COLUMN biggest_lever_detail jsonb;
ALTER TABLE public.analysis_run ADD COLUMN score_breakdown_json jsonb;
ALTER TABLE public.analysis_run ADD COLUMN ai_text_indicator_json jsonb;
ALTER TABLE public.analysis_run ADD COLUMN doc_url text;
UPDATE public.analysis_run r SET
  analysis_run_started_at=b.analysis_run_started_at,
  analysis_run_style_reference_url=b.analysis_run_style_reference_url,
  missing_sections=b.missing_sections,has_all_required_sections=b.has_all_required_sections,
  structural_completeness_score=b.structural_completeness_score,
  cross_chapter_coherence_score=b.cross_chapter_coherence_score,
  functional_metric_score=b.functional_metric_score,biggest_lever=b.biggest_lever,
  biggest_lever_detail=b.biggest_lever_detail,score_breakdown_json=b.score_breakdown_json,
  ai_text_indicator_json=b.ai_text_indicator_json,doc_url=b.doc_url
FROM backup_012.analysis_run b WHERE r.analysis_run_id=b.analysis_run_id;
UPDATE public.analysis_run r SET analysis_run_started_at=r.analysis_run_created_at,
  doc_url=m.manuscript_source_url FROM public.manuscript m
WHERE r.manuscript_id=m.manuscript_id AND NOT EXISTS
  (SELECT 1 FROM backup_012.analysis_run b WHERE b.analysis_run_id=r.analysis_run_id);
ALTER TABLE public.analysis_run ALTER COLUMN analysis_run_started_at SET DEFAULT now();
ALTER TABLE public.analysis_run DROP COLUMN analysis_run_is_unlocked;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_error_message TO error_message;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_score TO analysis_run_coherence_score;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_band TO functional_metric_band;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_citation_score TO citation_integrity_score;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_ai_text_score TO ai_text_indicator_score;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_sections_analyzed TO sections_analyzed;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_inconsistency_count TO inconsistencies_found;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_citation_count TO citations_audited;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_result_json TO result_json;
ALTER TABLE public.analysis_run RENAME COLUMN analysis_run_created_at TO created_at;
ALTER TABLE public.analysis_run RENAME CONSTRAINT analysis_run_analysis_run_score_check TO analysis_run_analysis_run_coherence_score_check;
ALTER TABLE public.analysis_run ADD CONSTRAINT analysis_run_status_check
  CHECK (status IN ('processing','completed','failed'));

ALTER TABLE public.inconsistency ADD COLUMN inconsistency_description text;
ALTER TABLE public.inconsistency ADD COLUMN inconsistency_text_offset_start integer NOT NULL DEFAULT 0;
ALTER TABLE public.inconsistency ADD COLUMN inconsistency_text_offset_end integer NOT NULL DEFAULT 0;
ALTER TABLE public.inconsistency ADD COLUMN inconsistency_status varchar(50) NOT NULL DEFAULT 'active';
ALTER TABLE public.inconsistency ADD COLUMN objectives_unaddressed jsonb DEFAULT '[]'::jsonb;
UPDATE public.inconsistency SET inconsistency_description=inconsistency_explanation_what;
UPDATE public.inconsistency i SET inconsistency_description=b.inconsistency_description,
  inconsistency_text_offset_start=b.inconsistency_text_offset_start,
  inconsistency_text_offset_end=b.inconsistency_text_offset_end,
  inconsistency_status=b.inconsistency_status,objectives_unaddressed=b.objectives_unaddressed
FROM backup_012.inconsistency b WHERE i.inconsistency_id=b.inconsistency_id;
ALTER TABLE public.inconsistency ALTER COLUMN inconsistency_description SET NOT NULL;
ALTER TABLE public.inconsistency DROP COLUMN inconsistency_is_helpful;
ALTER TABLE public.inconsistency DROP COLUMN inconsistency_created_at;
ALTER TABLE public.inconsistency RENAME COLUMN inconsistency_section_a TO primary_section_name;
ALTER TABLE public.inconsistency RENAME COLUMN inconsistency_section_b TO conflicting_section_name;
ALTER TABLE public.inconsistency RENAME COLUMN inconsistency_pair_score TO coherence_score;
ALTER TABLE public.inconsistency RENAME COLUMN inconsistency_evidence_a TO evidence_a;
ALTER TABLE public.inconsistency RENAME COLUMN inconsistency_evidence_b TO evidence_b;
ALTER TABLE public.inconsistency RENAME COLUMN inconsistency_evidence_verified TO evidence_verified;
ALTER TABLE public.recommendation RENAME COLUMN recommendation_section TO target_section_name;
ALTER TABLE public.recommendation DROP COLUMN recommendation_created_at;

-- Recreate obsolete tables before the old payment and ledger links.
CREATE TABLE public.subscription_plan (
  plan_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), plan_name varchar(50) NOT NULL UNIQUE,
  plan_price numeric(10,2) NOT NULL DEFAULT 0.00, ai_quota_limit integer NOT NULL DEFAULT 3);
INSERT INTO public.subscription_plan SELECT * FROM backup_012.subscription_plan;
CREATE TABLE public.subscription (
  subscription_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL,
  plan_id uuid NOT NULL,subscription_status varchar(50) NOT NULL DEFAULT 'active',
  subscription_started_at timestamptz NOT NULL DEFAULT now(),
  subscription_renews_at timestamptz NOT NULL DEFAULT (now() + interval '30 days'),
  subscription_cancelled_at timestamptz);
INSERT INTO public.subscription SELECT * FROM backup_012.subscription;
INSERT INTO public.subscription(user_id,plan_id)
SELECT u.user_id,p.plan_id FROM public."user" u CROSS JOIN public.subscription_plan p
WHERE p.plan_name='Free' AND NOT EXISTS (SELECT 1 FROM public.subscription s WHERE s.user_id=u.user_id);
CREATE UNIQUE INDEX ux_user_active_subscription ON public.subscription(user_id)
WHERE subscription_status='active';

ALTER TABLE public.pymt_txn DROP CONSTRAINT pymt_txn_user_id_fkey;
ALTER TABLE public.pymt_txn ADD COLUMN subscription_id uuid;
UPDATE public.pymt_txn p SET subscription_id=b.subscription_id
FROM backup_012.pymt_txn b WHERE p.pymt_txn_id=b.pymt_txn_id;
UPDATE public.pymt_txn p SET subscription_id=s.subscription_id
FROM public.subscription s WHERE p.subscription_id IS NULL AND p.user_id=s.user_id AND s.subscription_status='active';
DO $$ BEGIN IF EXISTS (SELECT 1 FROM public.pymt_txn WHERE subscription_id IS NULL)
THEN RAISE EXCEPTION 'undo 012: payment without subscription'; END IF; END $$;
ALTER TABLE public.pymt_txn ALTER COLUMN subscription_id SET NOT NULL;
ALTER TABLE public.pymt_txn DROP COLUMN user_id;

CREATE TABLE public.credit_wallet (
  user_id uuid PRIMARY KEY,balance integer NOT NULL DEFAULT 0 CHECK (balance>=0),
  lifetime_purchased integer NOT NULL DEFAULT 0,lifetime_spent integer NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO public.credit_wallet SELECT * FROM backup_012.credit_wallet;
INSERT INTO public.credit_wallet(user_id,balance)
SELECT b.user_id,b.balance FROM backup_012.undo_wallet_balances b
ON CONFLICT (user_id) DO UPDATE SET balance=EXCLUDED.balance,updated_at=now();

DELETE FROM public.credit_ledger WHERE credit_ledger_kind='adjustment'
  AND credit_ledger_note='Opening balance (migration 012)';
ALTER TABLE public.credit_ledger RENAME CONSTRAINT credit_ledger_credit_ledger_id_pkey TO credit_ledger_pkey;
ALTER TABLE public.credit_ledger RENAME CONSTRAINT credit_ledger_credit_ledger_idempotency_key_key TO credit_ledger_idempotency_key_key;
ALTER TABLE public.credit_ledger RENAME CONSTRAINT credit_ledger_user_id_fkey TO fk_credit_ledger_user;
ALTER TABLE public.credit_ledger RENAME COLUMN credit_ledger_id TO ledger_id;
ALTER TABLE public.credit_ledger RENAME COLUMN credit_ledger_kind TO kind;
ALTER TABLE public.credit_ledger RENAME COLUMN credit_ledger_delta TO delta;
ALTER TABLE public.credit_ledger RENAME COLUMN credit_ledger_balance_after TO balance_after;
ALTER TABLE public.credit_ledger RENAME COLUMN credit_ledger_idempotency_key TO idempotency_key;
ALTER TABLE public.credit_ledger RENAME COLUMN credit_ledger_note TO note;
ALTER TABLE public.credit_ledger RENAME COLUMN credit_ledger_created_at TO created_at;
ALTER TABLE public.credit_ledger ALTER COLUMN kind TYPE text USING kind::text;
DROP TYPE public.credit_ledger_kind;
CREATE TYPE public.credit_ledger_kind AS ENUM ('grant','purchase','debit','refund');
ALTER TABLE public.credit_ledger ALTER COLUMN kind TYPE public.credit_ledger_kind USING kind::public.credit_ledger_kind;
DROP INDEX public.credit_ledger_user_id_created_at_idx;
CREATE INDEX idx_credit_ledger_user_id ON public.credit_ledger(user_id,created_at DESC);

CREATE TABLE public.issue_feedback (
  feedback_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),inconsistency_id uuid NOT NULL,
  user_id uuid NOT NULL,is_helpful boolean NOT NULL,created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO public.issue_feedback SELECT * FROM backup_012.issue_feedback;
INSERT INTO public.issue_feedback(inconsistency_id,user_id,is_helpful,created_at)
SELECT u.inconsistency_id,u.user_id,u.is_helpful,
  CASE WHEN f.feedback_id IS NULL THEN u.created_at ELSE now() END
FROM backup_012.undo_feedback u
LEFT JOIN LATERAL (SELECT feedback_id,is_helpful FROM public.issue_feedback f
  WHERE f.inconsistency_id=u.inconsistency_id ORDER BY created_at DESC,feedback_id DESC LIMIT 1) f ON true
WHERE f.is_helpful IS DISTINCT FROM u.is_helpful;
CREATE INDEX idx_issue_feedback_inconsistency ON public.issue_feedback(inconsistency_id);
CREATE INDEX idx_issue_feedback_user ON public.issue_feedback(user_id);
CREATE TABLE public.citation_crossmatch_issue (
  issue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),analysis_run_id uuid NOT NULL,
  issue_type text NOT NULL CHECK (issue_type IN ('orphan_intext','uncited_reference')),
  surname text,year integer,context_sentence text,reference_raw_text text,
  created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO public.citation_crossmatch_issue SELECT * FROM backup_012.citation_crossmatch_issue;
CREATE INDEX idx_citation_crossmatch_run_id ON public.citation_crossmatch_issue(analysis_run_id);
CREATE TABLE public.notification (
  notification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid NOT NULL,
  analysis_run_id uuid,notification_type varchar(50) NOT NULL,notification_payload jsonb NOT NULL,
  notification_isread boolean NOT NULL DEFAULT false,notification_onesignal_id varchar(100),
  notification_sent_at timestamptz NOT NULL DEFAULT now());
INSERT INTO public.notification SELECT * FROM backup_012.notification;

-- Restore the FKs removed with duplicate and dropped tables.
ALTER TABLE public.analysis_run ADD CONSTRAINT fk_analysis_run_manuscript FOREIGN KEY (manuscript_id) REFERENCES public.manuscript(manuscript_id) ON DELETE CASCADE;
ALTER TABLE public.analysis_run ADD CONSTRAINT fk_analysis_run_user FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE public.citation ADD CONSTRAINT fk_citation_analysis_run FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_run(analysis_run_id) ON DELETE CASCADE;
ALTER TABLE public.inconsistency ADD CONSTRAINT inconsistency_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_run(analysis_run_id) ON DELETE CASCADE;
ALTER TABLE public.manuscript ADD CONSTRAINT manuscript_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE public.recommendation ADD CONSTRAINT recommendation_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_run(analysis_run_id) ON DELETE CASCADE;
ALTER TABLE public.credit_ledger ADD CONSTRAINT credit_ledger_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.credit_wallet ADD CONSTRAINT credit_wallet_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth.users(id) ON DELETE CASCADE;
ALTER TABLE public.credit_wallet ADD CONSTRAINT fk_credit_wallet_user FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE public.subscription ADD CONSTRAINT fk_subscription_user FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE public.subscription ADD CONSTRAINT subscription_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE public.subscription ADD CONSTRAINT subscription_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.subscription_plan(plan_id);
ALTER TABLE public.pymt_txn ADD CONSTRAINT fk_pymt_txn_subscription FOREIGN KEY (subscription_id) REFERENCES public.subscription(subscription_id) ON DELETE CASCADE;
ALTER TABLE public.pymt_txn ADD CONSTRAINT pymt_txn_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscription(subscription_id) ON DELETE CASCADE;
ALTER TABLE public.issue_feedback ADD CONSTRAINT issue_feedback_inconsistency_id_fkey FOREIGN KEY (inconsistency_id) REFERENCES public.inconsistency(inconsistency_id) ON DELETE CASCADE;
ALTER TABLE public.issue_feedback ADD CONSTRAINT issue_feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE public.citation_crossmatch_issue ADD CONSTRAINT citation_crossmatch_issue_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_run(analysis_run_id) ON DELETE CASCADE;
ALTER TABLE public.citation_crossmatch_issue ADD CONSTRAINT fk_crossmatch_analysis_run FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_run(analysis_run_id) ON DELETE CASCADE;
ALTER TABLE public.notification ADD CONSTRAINT fk_notification_analysis_run FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_run(analysis_run_id) ON DELETE SET NULL;
ALTER TABLE public.notification ADD CONSTRAINT notification_analysis_run_id_fkey FOREIGN KEY (analysis_run_id) REFERENCES public.analysis_run(analysis_run_id) ON DELETE SET NULL;
ALTER TABLE public.notification ADD CONSTRAINT fk_notification_user FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;
ALTER TABLE public.notification ADD CONSTRAINT notification_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."user"(user_id) ON DELETE CASCADE;

-- Reapply pre-012 RLS and grants to recreated tables.
ALTER TABLE public.subscription_plan ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.subscription ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_wallet ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.issue_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.citation_crossmatch_issue ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow public read for subscription plans" ON public.subscription_plan FOR SELECT USING (true);
CREATE POLICY "Users access own subscription" ON public.subscription USING (auth.uid()=user_id);
CREATE POLICY credit_wallet_select_own ON public.credit_wallet FOR SELECT USING (auth.uid()=user_id);
CREATE POLICY "Users can delete their own feedback" ON public.issue_feedback FOR DELETE USING (auth.uid()=user_id);
CREATE POLICY "Users can insert their own feedback" ON public.issue_feedback FOR INSERT WITH CHECK (auth.uid()=user_id);
CREATE POLICY "Users can read their own feedback" ON public.issue_feedback FOR SELECT USING (auth.uid()=user_id);
CREATE POLICY "Users can update their own feedback" ON public.issue_feedback FOR UPDATE USING (auth.uid()=user_id) WITH CHECK (auth.uid()=user_id);
CREATE POLICY "Users view own citation crossmatch issues" ON public.citation_crossmatch_issue FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.analysis_run WHERE analysis_run.analysis_run_id=citation_crossmatch_issue.analysis_run_id AND analysis_run.user_id=auth.uid()));
CREATE POLICY "Users access own notifications" ON public.notification USING (auth.uid()=user_id);
GRANT ALL ON public.subscription_plan,public.subscription,public.notification TO anon,authenticated,service_role;
GRANT REFERENCES,TRIGGER,TRUNCATE,MAINTAIN ON public.credit_wallet,public.issue_feedback,public.citation_crossmatch_issue TO anon,authenticated;
GRANT ALL ON public.credit_wallet,public.issue_feedback TO service_role;
GRANT SELECT,INSERT,REFERENCES,TRIGGER,TRUNCATE,MAINTAIN ON public.citation_crossmatch_issue TO service_role;

CREATE OR REPLACE FUNCTION public.debit_scan_credit(p_user_id uuid, p_analysis_run_id uuid, p_idempotency_key text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
    v_existing record;
    v_new_balance integer;
BEGIN
    SELECT * INTO v_existing FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing.balance_after;
    END IF;

    UPDATE public.credit_wallet
       SET balance = balance - 1,
           lifetime_spent = lifetime_spent + 1,
           updated_at = now()
     WHERE user_id = p_user_id AND balance >= 1
     RETURNING balance INTO v_new_balance;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'INSUFFICIENT_CREDITS' USING ERRCODE = 'P0001';
    END IF;

    INSERT INTO public.credit_ledger (user_id, kind, delta, balance_after, analysis_run_id, idempotency_key)
    VALUES (p_user_id, 'debit', -1, v_new_balance, p_analysis_run_id, p_idempotency_key);

    RETURN v_new_balance;
END;
$function$;

CREATE OR REPLACE FUNCTION public.handle_new_user()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    free_plan_id UUID;
BEGIN
    -- 1. Create public.user profile safely
    INSERT INTO public.user (user_id, user_first_name, user_last_name, user_email)
    VALUES (
        NEW.id,
        COALESCE(NEW.raw_user_meta_data->>'first_name', 'Student'),
        COALESCE(NEW.raw_user_meta_data->>'last_name', 'Researcher'),
        NEW.email
    )
    ON CONFLICT (user_id) DO UPDATE SET
        user_email = EXCLUDED.user_email,
        updated_at = now();

    -- 2. Fetch 'Free' plan ID
    SELECT plan_id INTO free_plan_id FROM public.subscription_plan WHERE plan_name = 'Free' LIMIT 1;

    -- 3. Assign Free Subscription safely
    IF free_plan_id IS NOT NULL THEN
        INSERT INTO public.subscription (user_id, plan_id, subscription_status)
        VALUES (NEW.id, free_plan_id, 'active')
        ON CONFLICT DO NOTHING;
    END IF;

    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.handle_new_user_credit_wallet()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
BEGIN
    INSERT INTO public.credit_wallet (user_id, balance, lifetime_purchased)
    VALUES (NEW.id, 1, 0)
    ON CONFLICT (user_id) DO NOTHING;

    INSERT INTO public.credit_ledger (user_id, kind, delta, balance_after, note)
    VALUES (NEW.id, 'grant', 1, 1, 'Free signup credit');

    RETURN NEW;
END;
$function$;

CREATE OR REPLACE FUNCTION public.purchase_credits(p_user_id uuid, p_amount integer, p_pymt_txn_id uuid, p_idempotency_key text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
    v_existing record;
    v_new_balance integer;
BEGIN
    IF p_amount <= 0 THEN
        RAISE EXCEPTION 'INVALID_AMOUNT' USING ERRCODE = 'P0001';
    END IF;

    SELECT * INTO v_existing FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing.balance_after;
    END IF;

    INSERT INTO public.credit_wallet (user_id, balance, lifetime_purchased)
    VALUES (p_user_id, p_amount, p_amount)
    ON CONFLICT (user_id) DO UPDATE
        SET balance = credit_wallet.balance + p_amount,
            lifetime_purchased = credit_wallet.lifetime_purchased + p_amount,
            updated_at = now()
    RETURNING balance INTO v_new_balance;

    INSERT INTO public.credit_ledger (user_id, kind, delta, balance_after, pymt_txn_id, idempotency_key)
    VALUES (p_user_id, 'purchase', p_amount, v_new_balance, p_pymt_txn_id, p_idempotency_key);

    RETURN v_new_balance;
END;
$function$;

CREATE OR REPLACE FUNCTION public.refund_scan_credit(p_user_id uuid, p_analysis_run_id uuid, p_idempotency_key text)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
    v_existing record;
    v_new_balance integer;
BEGIN
    SELECT * INTO v_existing FROM public.credit_ledger WHERE idempotency_key = p_idempotency_key;
    IF FOUND THEN
        RETURN v_existing.balance_after;
    END IF;

    UPDATE public.credit_wallet
       SET balance = balance + 1,
           updated_at = now()
     WHERE user_id = p_user_id
     RETURNING balance INTO v_new_balance;

    IF NOT FOUND THEN
        INSERT INTO public.credit_wallet (user_id, balance) VALUES (p_user_id, 1)
        RETURNING balance INTO v_new_balance;
    END IF;

    INSERT INTO public.credit_ledger (user_id, kind, delta, balance_after, analysis_run_id, idempotency_key)
    VALUES (p_user_id, 'refund', 1, v_new_balance, p_analysis_run_id, p_idempotency_key);

    RETURN v_new_balance;
END;
$function$;

CREATE OR REPLACE FUNCTION public.unlock_scan_report(p_user_id uuid, p_analysis_run_id uuid, p_charge boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
    v_result jsonb;
    v_balance integer;
BEGIN
    SELECT result_json INTO v_result
      FROM public.analysis_run
     WHERE analysis_run_id = p_analysis_run_id AND user_id = p_user_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'SCAN_NOT_FOUND' USING ERRCODE = 'P0001';
    END IF;
    IF v_result IS NULL THEN
        RAISE EXCEPTION 'SCAN_NOT_READY' USING ERRCODE = 'P0001';
    END IF;

    IF v_result->'access' IS NULL OR (v_result->'access'->>'unlocked')::boolean IS TRUE THEN
        SELECT balance INTO v_balance FROM public.credit_wallet WHERE user_id = p_user_id;
        RETURN jsonb_build_object('unlocked', true, 'charged', false, 'credits_remaining', v_balance);
    END IF;

    IF p_charge THEN
        UPDATE public.credit_wallet
           SET balance = balance - 1,
               lifetime_spent = lifetime_spent + 1,
               updated_at = now()
         WHERE user_id = p_user_id AND balance >= 1
         RETURNING balance INTO v_balance;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'INSUFFICIENT_CREDITS' USING ERRCODE = 'P0001';
        END IF;
        INSERT INTO public.credit_ledger
            (user_id, kind, delta, balance_after, analysis_run_id, idempotency_key, note)
        VALUES
            (p_user_id, 'debit', -1, v_balance, p_analysis_run_id,
             'scan_unlock:' || p_analysis_run_id::text, 'Full report unlock');
    ELSE
        SELECT balance INTO v_balance FROM public.credit_wallet WHERE user_id = p_user_id;
    END IF;

    UPDATE public.analysis_run
       SET result_json = jsonb_set(v_result, '{access}',
           (v_result->'access') || jsonb_build_object(
               'unlocked', true, 'unlock_cost', 1,
               'locked_counts', jsonb_build_object('sections', 0, 'recommendations', 0, 'citations', 0)))
           || jsonb_build_object('credits_remaining', v_balance)
     WHERE analysis_run_id = p_analysis_run_id;

    RETURN jsonb_build_object('unlocked', true, 'charged', p_charge, 'credits_remaining', v_balance);
END;
$function$;

CREATE TRIGGER on_auth_user_created AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION handle_new_user();

CREATE TRIGGER on_auth_user_created_credit_wallet AFTER INSERT ON auth.users FOR EACH ROW EXECUTE FUNCTION handle_new_user_credit_wallet();

GRANT EXECUTE ON FUNCTION public.debit_scan_credit(uuid,uuid,text),public.refund_scan_credit(uuid,uuid,text),
  public.purchase_credits(uuid,integer,uuid,text) TO PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.unlock_scan_report(uuid,uuid,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.unlock_scan_report(uuid,uuid,boolean) TO service_role;

COMMIT;
