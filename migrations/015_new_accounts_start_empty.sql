-- Run in the Supabase SQL editor so a new account starts with 0 credits and has to buy them.
-- (Migration 004 gave every new account one free credit.) Existing accounts keep their balance.
CREATE OR REPLACE FUNCTION public.handle_new_user_credit_wallet()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.credit_wallet (user_id, balance, lifetime_purchased)
    VALUES (NEW.id, 0, 0)
    ON CONFLICT (user_id) DO NOTHING;
    RETURN NEW;
END;
$$;
