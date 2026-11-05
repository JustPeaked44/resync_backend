-- Run in the Supabase SQL editor. Buying credits fails with "Could not find the 'user_id' column of 'pymt_txn'" until it is added:
-- every purchase is recorded against the person who made it, and only that person can confirm it.
ALTER TABLE public.pymt_txn
    ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_pymt_txn_user_id ON public.pymt_txn (user_id);

-- Payments no longer have to hang off a subscription row.
ALTER TABLE public.pymt_txn ALTER COLUMN subscription_id DROP NOT NULL;

-- Make the API see the new column right away.
NOTIFY pgrst, 'reload schema';
