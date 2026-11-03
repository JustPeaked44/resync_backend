-- Run this FIRST, on its own (one Run in the Supabase SQL editor), then run 012_schema_cleanup.sql.
-- PostgreSQL only lets a new enum value be used after the statement that added it has been committed,
-- so it cannot share one script with the migration that uses it.
ALTER TYPE public.credit_ledger_kind ADD VALUE IF NOT EXISTS 'adjustment';
