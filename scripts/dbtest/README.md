# Migration 012 local test

Run from this worktree with:

```powershell
& 'D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\python.exe' scripts\dbtest\run.py
```

The script starts the local PostgreSQL 17 cluster on `127.0.0.1:54329`, recreates **only** `resync_012_test`, loads the supplied live schema and function export, seeds synthetic rows, and tests forward migration, credit functions, parallel debits, undo, and forward migration again. It stops PostgreSQL in `finally`, including on assertion failure. It uses the Python standard library and `psql.exe`; no Supabase, app, or deployed service is called. Do not point it at another database.

The exported live `analysis_run_status_check` rejects the legacy text value `complete`. The synthetic fixture drops that check to exercise migration 012's normalization of such a row; the harness compares the undo against the unmodified live schema loaded before the fixture change. PostgreSQL 17 provides `gen_random_uuid()` without a separate `pgcrypto` install.

The local database has no Supabase Storage schema, so migration 013 is reviewed as SQL only. The harness drops `backup_012` after verifying undo so it can test a fresh forward run; production backups must be retained until the owner authorizes cleanup.
