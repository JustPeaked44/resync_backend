-- Run in the Supabase SQL editor to let researchers upload proof of payment (a screenshot of the GCash / bank transfer
-- receipt and its reference number) with a credit purchase.
--
-- One row per upload, tied to the payment transaction. The picture is kept as text (a data URL) so no storage bucket is
-- needed; the backend limits it to 2 MB. Only the backend (service role) uses this table.
CREATE TABLE IF NOT EXISTS public.payment_proof (
    proof_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pymt_txn_id      uuid NOT NULL REFERENCES public.pymt_txn(pymt_txn_id) ON DELETE CASCADE,
    user_id          uuid NOT NULL,
    method           text NOT NULL,           -- gcash | bank_transfer
    reference_number text NOT NULL,
    payer_name       text,
    file_name        text,
    content_type     text NOT NULL,
    proof_data       text NOT NULL,           -- the image as a data URL
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payment_proof_txn_idx ON public.payment_proof (pymt_txn_id);

ALTER TABLE public.payment_proof ENABLE ROW LEVEL SECURITY;   -- no policies: browsers can never read it
GRANT ALL ON public.payment_proof TO service_role;
