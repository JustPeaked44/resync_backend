-- Run in the Supabase SQL editor before enabling the v7 report paywall.
-- The row lock serializes concurrent unlocks; wallet, ledger, and result_json
-- change in one transaction or all roll back.
CREATE OR REPLACE FUNCTION public.unlock_scan_report(
    p_user_id uuid,
    p_analysis_run_id uuid,
    p_charge boolean
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
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
     WHERE analysis_run_id = p_analysis_run_id;

    RETURN jsonb_build_object('unlocked', true, 'charged', p_charge, 'credits_remaining', v_balance);
END;
$$;

REVOKE ALL ON FUNCTION public.unlock_scan_report(uuid, uuid, boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.unlock_scan_report(uuid, uuid, boolean) TO service_role;
