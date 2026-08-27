-- 004_credits.sql
-- Pay-per-scan credits ledger. Run in the Supabase SQL Editor.
-- Replaces the never-enforced subscription_ai_quota_used counter with a
-- real, server-enforced balance plus an append-only ledger.

-- ---------------------------------------------------------------------
-- Widen the existing pymt_txn table (Phase 1 schema) with the columns
-- the simulated checkout flow needs. Column names are guessed defensively
-- with IF NOT EXISTS since the original Phase 1 script that created this
-- table is not in this repo -- adjust names here first if they collide
-- with existing columns of a different name.
-- ---------------------------------------------------------------------
ALTER TABLE public.pymt_txn
    ADD COLUMN IF NOT EXISTS pymt_txn_status text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS pymt_txn_credits integer,
    ADD COLUMN IF NOT EXISTS pymt_txn_amount numeric(10, 2),
    ADD COLUMN IF NOT EXISTS pymt_txn_provider text NOT NULL DEFAULT 'simulated';

-- ---------------------------------------------------------------------
-- Wallet: one row per user, current balance only.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.credit_wallet (
    user_id             uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    balance             integer NOT NULL DEFAULT 0 CHECK (balance >= 0),
    lifetime_purchased  integer NOT NULL DEFAULT 0,
    lifetime_spent      integer NOT NULL DEFAULT 0,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Ledger: append-only history of every balance change.
-- idempotency_key prevents a retried request (for example a client that
-- resends a checkout confirmation after a timeout) from double-crediting
-- or double-debiting the same event.
-- ---------------------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE public.credit_ledger_kind AS ENUM ('grant', 'purchase', 'debit', 'refund');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS public.credit_ledger (
    ledger_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    kind                public.credit_ledger_kind NOT NULL,
    delta               integer NOT NULL,
    balance_after       integer NOT NULL,
    analysis_run_id     uuid REFERENCES public.analysis_run(analysis_run_id) ON DELETE SET NULL,
    pymt_txn_id         uuid REFERENCES public.pymt_txn(pymt_txn_id) ON DELETE SET NULL,
    idempotency_key     text UNIQUE,
    note                text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_id ON public.credit_ledger(user_id, created_at DESC);

-- ---------------------------------------------------------------------
-- RPC: debit_scan_credit -- atomic conditional debit. Raises
-- INSUFFICIENT_CREDITS (caught by the backend as HTTP 402) when the
-- user has no balance. SECURITY DEFINER plus the WHERE balance >= 1
-- guard is what makes two concurrent scans against balance = 1 resolve
-- to exactly one success.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.debit_scan_credit(
    p_user_id uuid,
    p_analysis_run_id uuid,
    p_idempotency_key text
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
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
$$;

-- ---------------------------------------------------------------------
-- RPC: refund_scan_credit -- called when a scan pipeline fails after the
-- debit already happened. A failed scan must never cost the student a
-- credit.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.refund_scan_credit(
    p_user_id uuid,
    p_analysis_run_id uuid,
    p_idempotency_key text
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
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
$$;

-- ---------------------------------------------------------------------
-- RPC: purchase_credits -- called by the (simulated) checkout confirm
-- handler. This is the exact seam a real PayMongo or Stripe webhook
-- would call instead of the mock confirm endpoint.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.purchase_credits(
    p_user_id uuid,
    p_amount integer,
    p_pymt_txn_id uuid,
    p_idempotency_key text
) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
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
$$;

-- ---------------------------------------------------------------------
-- Seed a free credit for new users. Attached as its own trigger rather
-- than folded into the existing handle_new_user() function, so it can
-- be added without touching that function's body.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_new_user_credit_wallet()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.credit_wallet (user_id, balance, lifetime_purchased)
    VALUES (NEW.id, 1, 0)
    ON CONFLICT (user_id) DO NOTHING;

    INSERT INTO public.credit_ledger (user_id, kind, delta, balance_after, note)
    VALUES (NEW.id, 'grant', 1, 1, 'Free signup credit');

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created_credit_wallet ON auth.users;
CREATE TRIGGER on_auth_user_created_credit_wallet
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user_credit_wallet();

-- ---------------------------------------------------------------------
-- RLS: users may read their own wallet and ledger; only the service
-- role (used by the FastAPI backend) writes.
-- ---------------------------------------------------------------------
ALTER TABLE public.credit_wallet ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_ledger ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS credit_wallet_select_own ON public.credit_wallet;
CREATE POLICY credit_wallet_select_own ON public.credit_wallet
    FOR SELECT USING (auth.uid() = user_id);

DROP POLICY IF EXISTS credit_ledger_select_own ON public.credit_ledger;
CREATE POLICY credit_ledger_select_own ON public.credit_ledger
    FOR SELECT USING (auth.uid() = user_id);
