# Local migration 017 test

Run `python scripts/dbtest/run.py` with the project's pinned Python and `PYTHON_DOTENV_DISABLED=1`. The harness uses only local PostgreSQL on port 54329 and synthetic rows. It creates `resync_017_test`, refuses to overwrite an existing database of that name, and drops only the database it created. It starts the configured local cluster if needed and stops it only if it started it.

It loads the supplied schema and function export, applies migrations 012–016, checks preconditions, migrates, verifies the final catalog and function privileges, undoes and reapplies, and tests cached verdict and unlock behavior. It rejects nonempty legacy proof rows without modifying them. Migration 017a and the forward transaction are sent as separate SQL-editor-style requests; undo is executed as a transaction using psql's file input. A fake storage schema tests migration 018 locally.
