-- Run in the Supabase SQL editor so "Forgot password" can tell whether an email belongs to an account.
-- Without it the backend still works: it falls back to scanning the user list, which is slower.
--
-- Only the backend (service role) may call it; browsers never can.
CREATE OR REPLACE FUNCTION public.email_registered(p_email text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, auth
AS $$
    SELECT EXISTS (SELECT 1 FROM auth.users WHERE lower(email) = lower(trim(p_email)));
$$;

REVOKE ALL ON FUNCTION public.email_registered(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.email_registered(text) TO service_role;
