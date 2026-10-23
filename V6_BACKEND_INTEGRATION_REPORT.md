# V6 backend integration report

## Changes

- `services/v6_model.py`: added the requested five-head MiniLM architecture. The backbone loads from local `models/model_v5b` with `local_files_only=True` to keep startup offline.
- `services/embedding.py`: kept the v5 `EmbeddingService._model` intact; added a strict-loaded v6 singleton, local tokenizer, five-word guard, 256-token truncation, and five-float inference result.
- `services/xai_utils.py`: added sentence masking in both directions and supporting/undermining evidence ranking.
- `main.py`: attached advisory dimensions for seven role pairs, added `key_strength` findings without duplicating Gemini issues, and filled missing issue evidence where possible. Strengths are excluded from revision recommendations. Scoring and band computation are unchanged.
- `tests/test_v6_model.py`, `tests/test_xai_utils.py`: added real-model tests for output, empty text, long input, and explanation shape.
- `requirements.txt`: unchanged.

## Verification

Commands use the repository's `.venv` Python, which is the installed project environment. Progress-bar carriage returns are rendered here as lines.

### 1. Predictor sample — PASS

```text
> .venv/Scripts/python.exe -c "from services.embedding import predict_coherence; print(predict_coherence('Test A', 'Test B'))"
This model was created with Sentence Transformers version 5.7.0, but you're using version 5.6.1. Consider updating to the latest version to avoid potential issues.
Loading weights:   0%|          | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|##########| 103/103 [00:00<00:00, 1596.42it/s]
Loading weights:   0%|          | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|##########| 103/103 [00:00<00:00, 2608.85it/s]
{'objectives_alignment': None, 'methodology_fit': None, 'numeric_consistency': None, 'claim_support': None, 'overall_coherence': None}
```

The two supplied inputs have fewer than five words, so the specified guard returns five `None` values. The real-model float path is exercised by the tests below.

### 2. Explainer sample — PASS

```text
> .venv/Scripts/python.exe -c "from services.xai_utils import explain_pair; print(explain_pair('The method uses a survey of 200 participants. Data was collected over six weeks. Results were analyzed using regression.', 'The study found no significant effect. Prior work suggested a strong correlation. The sample size was too small to generalize.'))"
This model was created with Sentence Transformers version 5.7.0, but you're using version 5.6.1. Consider updating to the latest version to avoid potential issues.
Loading weights:   0%|          | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|##########| 103/103 [00:00<00:00, 1401.49it/s]
Loading weights:   0%|          | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|##########| 103/103 [00:00<00:00, 1768.34it/s]
{'baseline': {'objectives_alignment': 0.6815671920776367, 'methodology_fit': 0.63407963514328, 'numeric_consistency': 0.7457568645477295, 'claim_support': 0.6799832582473755, 'overall_coherence': 0.7028965353965759}, 'evidence_a': {'top_supporting': [], 'top_undermining': [{'sentence': 'The method uses a survey of 200 participants.', 'delta': -0.07711911201477051}, {'sentence': 'Data was collected over six weeks.', 'delta': -0.027799665927886963}, {'sentence': 'Results were analyzed using regression.', 'delta': -0.004437744617462158}]}, 'evidence_b': {'top_supporting': [{'sentence': 'The sample size was too small to generalize.', 'delta': 0.19550418853759766}, {'sentence': 'The study found no significant effect.', 'delta': 0.19463348388671875}, {'sentence': 'Prior work suggested a strong correlation.', 'delta': 0.09257078170776367}], 'top_undermining': []}}
```

### 3. Tests — PASS

```text
> .venv/Scripts/python.exe -m pytest tests/test_v6_model.py tests/test_xai_utils.py -v
============================= test session starts =============================
platform win32 -- Python 3.12.4, pytest-9.1.1, pluggy-1.6.0 -- D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: D:\RESYNC_DEV\backend\bcknd\resync - backend
plugins: anyio-4.14.2, langsmith-0.10.12, asyncio-1.4.0
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 5 items

tests/test_v6_model.py::test_real_v6_prediction PASSED                   [ 20%]
tests/test_v6_model.py::test_near_empty_input PASSED                     [ 40%]
tests/test_v6_model.py::test_long_input_truncates PASSED                 [ 60%]
tests/test_xai_utils.py::test_sentence_explanations PASSED               [ 80%]
tests/test_xai_utils.py::test_single_sentence_and_empty_side PASSED      [100%]

============================== warnings summary ===============================
.venv\Lib\site-packages\_pytest\cacheprovider.py:469
  D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Lib\site-packages\_pytest\cacheprovider.py:469: PytestCacheWarning: cache could not write path D:\RESYNC_DEV\backend\bcknd\resync - backend\.pytest_cache\v\cache\nodeids: [Errno 13] Permission denied: 'D:\\RESYNC_DEV\\backend\\bcknd\\resync - backend\\.pytest_cache\\v\\cache\\nodeids'
    config.cache.set("cache/nodeids", sorted(self.cached_nodeids))

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
======================== 5 passed, 1 warning in 29.44s ========================
```

```text
> .venv/Scripts/python.exe -m pytest -q
....................................                                     [100%]
============================== warnings summary ===============================
.venv\Lib\site-packages\_pytest\cacheprovider.py:469
  D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Lib\site-packages\_pytest\cacheprovider.py:469: PytestCacheWarning: could not create cache path D:\RESYNC_DEV\backend\bcknd\resync - backend\.pytest_cache\v\cache\nodeids: [WinError 5] Access is denied: 'D:\\RESYNC_DEV\\backend\\bcknd\\resync - backend\\.pytest_cache\\v\\cache'
    config.cache.set("cache/nodeids", sorted(self.cached_nodeids))

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
36 passed, 1 warning in 32.49s
```

### 4. App import — PASS

```text
> .venv/Scripts/python.exe -c "import main"
This model was created with Sentence Transformers version 5.7.0, but you're using version 5.6.1. Consider updating to the latest version to avoid potential issues.
Loading weights:   0%|          | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|##########| 103/103 [00:00<00:00, 2260.52it/s]
Loading weights:   0%|          | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|##########| 103/103 [00:00<00:00, 2715.54it/s]
```

Exit code: 0.

### 5. Dependencies — PASS

```text
> .venv/Scripts/python.exe -m pip list --format=freeze | Select-String '^(spacy|torch|transformers|sentence-transformers)=='
sentence-transformers==5.6.1
spacy==3.8.14
torch==2.13.0
transformers==5.14.1

> git -c 'safe.directory=D:/RESYNC_DEV/backend/bcknd/resync - backend' diff -- requirements.txt
```

The second command produced no output: `requirements.txt` has no changes. Transformers is already installed through sentence-transformers.

## Deviations and limits

- The checkpoint's head keys are `head_obj`, `head_meth`, `head_num`, `head_claim`, `head_overall`, and `shared.0`, while the required class names the layers differently. The loader translates only these keys, then uses strict `load_state_dict`, so missing files, unexpected keys, and tensor shape mismatches still fail at startup. An initial run before the translation failed with the expected strict mismatch error.
- The backbone uses the local `./models/model_v5b` path instead of the Hub identifier, so startup never fetches weights.
- Sentence masking checks the first 40 sentences per side. Raise this ceiling if real manuscript sections need broader evidence coverage. The predictor independently truncates each side to 256 tokens.
- `analysis_run.result_json` retains the full response, including `finding_status`. The older separate `inconsistency` table has no `finding_status` column; consumers reading that table alone cannot distinguish strengths. Add a database migration if that table must support strength-aware history.
- Pytest passed but could not write its cache in this workspace. The v5 model prints a version compatibility notice; loading and inference succeeded.

## Live Scan Diagnostic

### Actual saved production response (before v6 integration)

The project owner queried `public.analysis_run.result_json` in Supabase SQL Editor for a completed scan. The relevant read-only query selected the missing-section list, advisory dimensions, objectives/methodology finding, and strength findings from the saved JSON. The matching run is `79b3c5c4-3b55-4628-bc96-676cc300c98d`, completed `2026-10-22 17:04:16.126038+00`. This is a real app scan, not a test fixture. The returned raw JSON excerpt was:

```json
{
  "analysis_run_id": "79b3c5c4-3b55-4628-bc96-676cc300c98d",
  "analysis_run_completed_at": "2026-10-22 17:04:16.126038+00",
  "missing_sections": ["abstract", "conclusion", "discussion", "results"],
  "v6_dimensions": null,
  "objectives_methodology_finding": {
    "section_a": "objectives",
    "section_b": "methodology",
    "evidence_a": "",
    "evidence_b": "Goals: Draft and finalize the foundational structural parameters of Chapter 1 — introduction, rationale, Literature Synthesis, Comparative Analysis & Full Technical Design Package",
    "finding_status": "material_issue",
    "coherence_score": 76.23,
    "inconsistency_id": "6c4f851b-7b07-45af-b2c0-a19e17a76969",
    "evidence_verified": false,
    "objectives_unaddressed": []
  },
  "key_strength_findings": null
}
```

**Root cause for absent v6 fields/strengths in this run:** the scan completed on October 22, before the October 23 local v6 integration. Its saved `result_json` has `v6_dimensions: null`, so the v6 pair loop and strength-generation branch did not run for this response. The absence of strengths cannot be attributed to a measured score below 0.80: this response contains no v6 scores. The five actual v6 dimensions for this historic scan are unavailable.

**Evidence path for this run:** `POST /api/scans/start` or `/api/scans/upload` schedules `_run_scan_job`, which calls `_execute_scan_pipeline`; the frontend polls `GET /api/scans/{analysis_run_id}` and receives the saved `result_json`. The existing reasoning code verifies Gemini's quotes in `services/reasoning.py` (`_verify_quote` and `_analyze_pair`). It clears a quote that cannot be found in its source role text. The observed empty `evidence_a`, non-empty `evidence_b`, and `evidence_verified: false` are consistent with Section A's Gemini quote failing verification while Section B's quote passed. The new Phase 3 `explain_pair` fallback in `main.py` was not present when this response was produced, so it was not reached for this run. A stored result is not reprocessed when polled again.

**Inline highlight path:** the frontend's `App.tsx` matches each non-empty evidence string with `manuscriptText.indexOf(...)`. If that exact string is absent from the preview text, it creates no highlight. It also requests preview text through `GET /api/scans/{scan_id}/manuscript`; the backend endpoint only serves Google Docs sources. The four missing-section findings have no source quotes. The saved `doc_url` and preview response for this run have not yet been supplied, so the reason its surviving `evidence_b` produced no highlight is not established from the available data.

**Reproduction limit:** an authenticated new scan through the frontend's production endpoint requires the owner's Supabase session and consumes a scan credit. No authenticated new scan or corrected raw response has been captured yet. No backend code change is justified solely by this historic response; it demonstrates a pre-v6 result and a failed quote verification, not a failure of the new fallback. A new scan after the running backend loads the v6 code is needed to observe current v6 numbers and whether the fallback populates evidence on this manuscript.

## No New Supabase Row Diagnostic

Run date: 2026-10-23 (Asia/Manila). Supabase timestamps below are returned with `+00:00`. No application code was changed during this diagnostic; this section was appended after the checks.

### 1. Backend process and startup

The repository's documented container start command and port are:

```text
# Expose port
EXPOSE 8000

# Run the FastAPI application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Before the restart, a server was listening on port 8000, but its process tree had started before today's v6 files were modified:

```text
--- PID 32008 ---
ProcessId       : 32008
ParentProcessId : 10308
Name            : uvicorn.exe
CommandLine     : "D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\uvicorn.exe" main:app --host 127.0.0.1 
                  --port 8000

--- PID 32456 ---
ProcessId       : 32456
ParentProcessId : 32008
Name            : python.exe
CommandLine     : "D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\python.exe"  
                  "D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\uvicorn.exe" main:app --host 127.0.0.1 
                  --port 8000

--- PID 29924 ---
ProcessId       : 29924
ParentProcessId : 32456
Name            : python.exe
CommandLine     : "C:\Users\NHMIER\AppData\Local\Programs\Python\Python312\python.exe" 
                  "D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\uvicorn.exe" main:app --host 127.0.0.1 
                  --port 8000

   Id ProcessName StartTime             Path
   -- ----------- ---------             ----
29924 python      10/22/2026 7:15:18 PM C:\Users\NHMIER\AppData\Local\Programs\Python\Python312\python.exe
32456 python      10/22/2026 7:15:18 PM D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\python.exe
32008 uvicorn     10/22/2026 7:15:18 PM D:\RESYNC_DEV\backend\bcknd\resync - backend\.venv\Scripts\uvicorn.exe

  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       29924
```

The current files were timestamped after that process started:

```text
Name          : main.py
Length        : 77108
LastWriteTime : 10/23/2026 12:48:40 AM

Name          : v6_model.py
Length        : 1432
LastWriteTime : 10/23/2026 12:41:45 AM

Name          : xai_utils.py
Length        : 1485
LastWriteTime : 10/23/2026 12:41:48 AM
```

I stopped those three exact old process IDs and started the documented command again. The captured startup output was:

```text
This model was created with Sentence Transformers version 5.7.0, but you're using version 5.6.1. Consider updating to the latest version to avoid potential issues.
Loading weights:   0%|                                 | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|█████████████████████| 103/103 [00:00<00:00, 1696.15it/s]
Loading weights:   0%|                                 | 0/103 [00:00<?, ?it/s]
Loading weights: 100%|█████████████████████| 103/103 [00:00<00:00, 2574.68it/s]
INFO:     Started server process [19788]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     127.0.0.1:49957 - "GET / HTTP/1.1" 200 OK
INFO:     Shutting down
INFO:     Waiting for application shutdown.
INFO:     Application shutdown complete.
INFO:     Finished server process [19788]
```

After the diagnostic, the current-code server was started again and left listening:

```text
  TCP    0.0.0.0:8000           0.0.0.0:0              LISTENING       32140
HTTP 200
{"status":"Resync FastAPI Server is Live!"}
```

The startup output contains only the Sentence Transformers version warning and model-load progress; there was no import error or torch/model-load failure. The full test suite also passed against the current files:

```text
................................                                     [100%]
36 passed in 59.07s
```

### 2. Supabase target and pre-reproduction rows

The root `.env` is the only root env-like file found. Secret values are intentionally masked in the captured output below:

```text
GEMINI_API_KEY=<redacted>
SUPABASE_URL=https://jahxqvagbhyqrwpjilix.supabase.co
SUPABASE_ANON_KEY=<redacted>
SUPABASE_SERVICE_ROLE_KEY=<redacted>

HOST=0.0.0.0
PORT=8000
CREDITS_ENABLED=false
```

The database service constructs its client from the service-role key, falling back to the anon key:

```text
 23:     def get_client(cls) -> Client:
 24:         """
 25:         Initializes and returns a Supabase Client using service role credentials.
 26:         """
 27:         if cls._client is None:
 28:             supabase_url = os.getenv("SUPABASE_URL")
 29:             supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
 30:             if not supabase_url or not supabase_key:
 31:                 raise ValueError(
 32:                     "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be configured in environment."
 33:                 )
 34:             cls._client = create_client(supabase_url, supabase_key)
 35:         return cls._client
```

The read-only query used the same configured client and sorted `public.analysis_run` by `created_at` descending before the reproduction. Its top ten rows were all from October 22 UTC; no newer completed row was returned:

```text
SUPABASE_PROJECT_REF=jahxqvagbhyqrwpjilix
ROWS_RETURNED=10
{'analysis_run_id': 'dc78e74b-300f-4117-a1a2-c1c9bb2e0232', 'created_at': '2026-10-22T17:36:11.047964+00:00', 'analysis_run_started_at': '2026-10-22T17:36:11.047964+00:00', 'analysis_run_completed_at': '2026-10-22T17:37:19.489214+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': '79b3c5c4-3b55-4628-bc96-676cc300c98d', 'created_at': '2026-10-22T17:02:57.514993+00:00', 'analysis_run_started_at': '2026-10-22T17:02:57.514993+00:00', 'analysis_run_completed_at': '2026-10-22T17:04:16.126038+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': '74041db7-d80d-4831-b868-6b82dc7396d0', 'created_at': '2026-10-22T15:51:41.85565+00:00', 'analysis_run_started_at': '2026-10-22T15:51:41.85565+00:00', 'analysis_run_completed_at': '2026-10-22T15:52:44.023915+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': '4a9a842f-a976-4f98-bbe2-6dd1d35bd593', 'created_at': '2026-10-22T15:44:12.656699+00:00', 'analysis_run_started_at': '2026-10-22T15:44:12.656699+00:00', 'analysis_run_completed_at': '2026-10-22T15:45:37.340565+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': 'dfa1a532-ade0-44bf-8f62-d4ba18f786d2', 'created_at': '2026-10-22T12:36:52.191916+00:00', 'analysis_run_started_at': '2026-10-22T12:36:52.191916+00:00', 'analysis_run_completed_at': '2026-10-22T12:37:52.61245+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': 'bd0e0d30-c192-4f31-a58f-e40bdf79c23b', 'created_at': '2026-10-22T12:24:02.542342+00:00', 'analysis_run_started_at': '2026-10-22T12:24:02.542342+00:00', 'analysis_run_completed_at': '2026-10-22T12:25:12.220186+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': '1d85f14b-51e3-4585-8bde-817004b45e9d', 'created_at': '2026-10-22T12:10:39.631044+00:00', 'analysis_run_started_at': '2026-10-22T12:10:39.631044+00:00', 'analysis_run_completed_at': '2026-10-22T12:12:04.882041+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': '48055d44-f5fa-4f27-a47f-4577882c7153', 'created_at': '2026-10-22T11:23:05.68008+00:00', 'analysis_run_started_at': '2026-10-22T11:23:05.68008+00:00', 'analysis_run_completed_at': '2026-10-22T11:24:10.957631+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': '04d13dc1-3fcb-4543-99b6-975731db382d', 'created_at': '2026-10-22T11:16:42.431348+00:00', 'analysis_run_started_at': '2026-10-22T11:16:42.431348+00:00', 'analysis_run_completed_at': '2026-10-22T11:17:43.570714+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
{'analysis_run_id': '209e2555-37a8-4bce-a95b-069c12246308', 'created_at': '2026-10-22T04:23:55.777301+00:00', 'analysis_run_started_at': '2026-10-22T04:23:55.777301+00:00', 'analysis_run_completed_at': '2026-10-22T04:24:54.084937+00:00', 'analysis_run_status': 'completed', 'status': 'completed', 'error_message': None, 'result_json_present': True, 'v6_dimensions_present': False}
```

There is no second local Supabase URL in the root configuration. The owner's SQL Editor project was not available to inspect, so a project mismatch between that editor and `jahxqvagbhyqrwpjilix` cannot be ruled out from this workspace.

### 3. Actual scan path and swallowed-exception points

The frontend-facing asynchronous file endpoint provisions the row before scheduling the background job and returns 202:

```text
1234:     analysis_run_id, resolved_manuscript_id, credits_remaining = await _provision_analysis_run(
1235:         req, default_source_name=file.filename
1236:     )
1237:     background_tasks.add_task(
1238:         _run_scan_job,
1239:         req,
1240:         analysis_run_id,
1241:         resolved_manuscript_id,
1242:         credits_remaining,
1243:         file_bytes=file_bytes,
1244:         filename=file.filename,
1245:     )
1246:     logger.info("Accepted async file scan job: %s (file: %s)", analysis_run_id, file.filename)
1247:     return ScanJobResponse(status="processing", analysis_run_id=analysis_run_id)
```

The JSON async endpoint has the same behavior:

```text
1167:     _assert_owner(authenticated_user_id, req.user_id)
1168:     analysis_run_id, manuscript_id, credits_remaining = await _provision_analysis_run(req)
1169:     background_tasks.add_task(_run_scan_job, req, analysis_run_id, manuscript_id, credits_remaining)
1170:     logger.info("Accepted async scan job: %s", analysis_run_id)
1171:     return ScanJobResponse(status="processing", analysis_run_id=analysis_run_id)
```

The background wrapper catches all pipeline exceptions. It attempts to mark the row failed and logs if that update itself fails; it does not re-raise to the already-returned HTTP request:

```text
1063:     async with SCAN_SLOT:
1064:         try:
1065:             result = await _execute_scan_pipeline(
1066:                 req,
1067:                 analysis_run_id,
1068:                 manuscript_id,
1069:                 credits_remaining,
1070:                 file_bytes=file_bytes,
1071:                 filename=filename,
1072:             )
1073:             db_client = DatabasePersistenceService.get_client()
1074:             await asyncio.to_thread(
1075:                 lambda: db_client.table("analysis_run").update({
1076:                     "status": "completed",
1077:                     "result_json": result.model_dump(mode="json"),
1078:                 }).eq("analysis_run_id", analysis_run_id).execute()
1079:             )
1080:             logger.info("Async scan job completed: %s", analysis_run_id)
1081:         except Exception as exc:
1082:             logger.exception("Async scan job failed: %s", analysis_run_id)
1083:             try:
1084:                 db_client = DatabasePersistenceService.get_client()
1085:                 await asyncio.to_thread(
1086:                     lambda: db_client.table("analysis_run").update({
1087:                         "status": "failed",
1088:                         "error_message": str(exc)[:1000],
1089:                     }).eq("analysis_run_id", analysis_run_id).execute()
1090:                 )
1091:             except Exception:
1092:                 logger.exception("Could not mark scan job %s as failed", analysis_run_id)
1093:             await credits_service.refund_scan_credit(req.user_id, analysis_run_id)
```

The polling endpoint returns a stored failure when `status == "failed"`, a stored completed response when `result_json` exists, and otherwise returns `processing`:

```text
1381:     if job_status == "failed":
1382:         return ScanStatusResponse(
1383:             status="failed",
1384:             analysis_run_id=analysis_run_id,
1385:             error=row.get("error_message") or "Scan failed for an unknown reason."
1386:         )
...
1392:     result_json = row.get("result_json")
1393:     if job_status == "completed" and result_json:
1394:         return ScanStatusResponse(
1395:             status="completed",
1396:             analysis_run_id=analysis_run_id,
1397:             result=ScanResponse(**result_json)
1398:         )
...
1400:     return ScanStatusResponse(status="processing", analysis_run_id=analysis_run_id)
```

Other exception swallowing found in the scan path is non-fatal: the manuscript-title lookup at `main.py:959-960` falls back silently, malformed optional upload TOC JSON at `main.py:1221-1222` and `1290-1291` is ignored, and notification failures return `False`. The whole-job catch at `main.py:1081-1093` is the only candidate that can hide a pipeline failure from the original 202 response; if the failure update succeeds, the polling row still exposes `failed`, and if the update fails, the row can remain `processing`.

### 4. Authenticated local reproduction

There was no manuscript fixture or bearer token in this repository, so I created a temporary confirmed Supabase auth user, generated an in-memory DOCX, signed in through Supabase Auth, and sent a real multipart HTTP request to `POST /api/scans/upload`. The temporary user was removed after the row was queried.

The captured HTTP sequence was:

```text
TEMP_USER_ID=dd4c86e8-806c-4e71-a164-40a1078d87cf
PUBLIC_USER_ROW_COUNT=1
AUTH_SIGN_IN_OK=True
START_HTTP_STATUS=202
START_HTTP_BODY={"status":"processing","analysis_run_id":"653dc31f-4882-4418-a639-4944405833bb"}
POLL_001_HTTP_STATUS=200 BODY={"status":"processing","analysis_run_id":"653dc31f-4882-4418-a639-4944405833bb","error":null,"result":null}
POLL_018_HTTP_STATUS=200 BODY={"status":"processing","analysis_run_id":"653dc31f-4882-4418-a639-4944405833bb","error":null,"result":null}
POLL_019_HTTP_STATUS=200 BODY={"status":"completed","analysis_run_id":"653dc31f-4882-4418-a639-4944405833bb","error":null,"result":{"status":"completed","analysis_run_id":"653dc31f-4882-4418-a639-4944405833bb","user_id":"dd4c86e8-806c-4e71-a164-40a1078d87cf","manuscript_id":"5fa35e13-4591-410e-8b80-6ae6d612c8c5","doc_url":"codex-local-diagnostic.docx","overall_coherence_score":59.35,"sections_analyzed":["abstract","introduction","objectives","methodology","results","discussion","conclusion","references"],"missing_sections":["introduction","methodology","conclusion"],"has_all_required_sections":false,
```

The same captured completed response contained the v6 output and persisted-save status:

```text
"score_breakdown":{"overall_score":59.35,"band":"Needs Revision","structural_completeness_score":50.0,"cross_chapter_coherence_score":65.59,"citation_integrity_score":0.0,"biggest_lever":{"reason":"Weakest chapter-to-chapter alignment: results \u2194 discussion, introduction \u2194 objectives.","criterion":"cross_chapter_coherence","current_score":65.59,"potential_point_gain":20.6},"structural_detail":{"stub_sections":["introduction","methodology","conclusion"],"missing_optional":[],"missing_required":["introduction","methodology","conclusion"],"present_optional":[],"present_required":["abstract","objectives","results","discussion","references"]},"coherence_detail":{"pair_scores":[{"score":69.47,"role_a":"objectives","role_b":"methodology","weight":0.22,"included":true,"raw_similarity":0.621},{"score":71.17,"role_a":"methodology","role_b":"results","weight":0.2,"included":true,"raw_similarity":0.6337},{"score":38.1,"role_a":"results","role_b":"discussion","weight":0.16,"included":true,"raw_similarity":0.404},{"score":70.93,"role_a":"objectives","role_b":"conclusion","weight":0.16,"included":true,"raw_similarity":0.632},{"score":54.37,"role_a":"introduction","role_b":"objectives","weight":0.12,"included":true,"raw_similarity":0.5125},{"score":100.0,"role_a":"abstract","role_b":"__document__","weight":0.08,"included":true,"raw_similarity":0.8877},{"score":68.42,"role_a":"discussion","role_b":"conclusion","weight":0.06,"included":true,"raw_similarity":0.6131}],"verifications":[],"dismissed_pairs":[{"score":69.47,"reason":"Although the embedding-similarity score is relatively low, a human review shows that Section A outlines high-level evaluative objectives (checking research design, measurements, and conclusions) and Section B outlines the structured review methodology designed to execute those exact evaluations. The two sections are logically consistent and complement each other appropriately without material contradiction or gap.","role_a":"objectives","role_b":"methodology"}],"unevaluable_weight_fraction":0.0},"v6_dimensions":{"results::discussion":{"claim_support":0.8042782545089722,"methodology_fit":0.7609840035438538,"overall_coherence":0.8339194655418396,"numeric_consistency":0.8218637704849243,"objectives_alignment":0.7873675227165222},"methodology::results":{"claim_support":0.7606773972511292,"methodology_fit":0.7347826957702637,"overall_coherence":0.8123648166656494,"numeric_consistency":0.7510645985603333,"objectives_alignment":0.7637524604797363},"abstract::__document__":{"claim_support":0.8988707661628723,"methodology_fit":0.8617833852767944,"overall_coherence":0.9237965941429138,"numeric_consistency":0.9200170636177063,"objectives_alignment":0.9079762101177063},"discussion::conclusion":{"claim_support":0.8764187693595886,"methodology_fit":0.8363843560218811,"overall_coherence":0.9056549072265625,"numeric_consistency":0.8980300426483154,"objectives_alignment":0.8773127198219299},"objectives::conclusion":{"claim_support":0.9027771353721619,"methodology_fit":0.8758942484855652,"overall_coherence":0.9349786639213562,"numeric_consistency":0.9247681498527527,"objectives_alignment":0.9083319306373596},"objectives::methodology":{"claim_support":0.5777323842048645,"methodology_fit":0.5326731204986572,"overall_coherence":0.6368834972381592,"numeric_consistency":0.6718987822532654,"objectives_alignment":0.5978404879570007},"introduction::objectives":{"claim_support":0.5976804494857788,"methodology_fit":0.5374757647514343,"overall_coherence":0.6440587639808655,"numeric_consistency":0.7368295788764954,"objectives_alignment":0.633798360824585}}
"db_save_status":"success","notification_dispatched":true,"credits_remaining":999
```

The database query ran before cleanup and returned the new row in the same configured project. The following is the verbatim prefix of its captured response, including the row state and the start of `result_json`:

```text
DB_ROW_QUERY_RESPONSE=[{"analysis_run_id":"653dc31f-4882-4418-a639-4944405833bb","created_at":"2026-10-22T18:06:16.889798+00:00","analysis_run_started_at":"2026-10-22T18:06:16.889798+00:00","analysis_run_completed_at":"2026-10-22T18:06:57.08574+00:00","analysis_run_status":"completed","status":"completed","error_message":null,"result_json":{"status":"completed","doc_url":"codex-local-diagnostic.docx","user_id":"dd4c86e8-806c-4e71-a164-40a1078d87cf","citations":[],"manuscript_id":"5fa35e13-4591-410e-8b80-6ae6d612c8c5","verifications":[],"db_save_status":"success",
```

Cleanup output:

```text
TEMP_USER_DELETED=True
```

The server-side request log for this run showed 202 acceptance followed by 200 polling responses. The captured log also showed Gemini quota degradation, not a pipeline crash. The API key embedded in the Gemini URL is redacted here:

```text
INFO:     127.0.0.1:62949 - "POST /api/scans/upload HTTP/1.1" 202 Accepted
INFO:     127.0.0.1:62949 - "GET /api/scans/653dc31f-4882-4418-a639-4944405833bb HTTP/1.1" 200 OK
Strong-pair verification failed, degrading to empty: Client error '429 Too Many Requests' for url 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key=<API_KEY_REDACTED>'
INFO:     127.0.0.1:62949 - "GET /api/scans/653dc31f-4882-4418-a639-4944405833bb HTTP/1.1" 200 OK
```

### Finding

Confirmed:

1. The listener that existed before this diagnostic was an October 22 process and therefore was not started after the October 23 v6 files were written. It was replaced with a current-code process.
2. The current code starts cleanly, loads the v6 model, passes the 36 tests, accepts an authenticated real HTTP scan, runs the background job, writes `analysis_run.result_json`, and returns v6 dimensions and `db_save_status: "success"` in the configured Supabase project.
3. Gemini quota exhaustion occurred during XAI verification, but the current scan still completed and persisted. It is not the cause of a missing `analysis_run` row.
4. The async worker has a real observability weakness at `main.py:1081-1093`: a pipeline exception is caught after the HTTP 202, and a second database exception while writing `status="failed"` is only logged. That can leave a row stuck at `processing`, but it cannot explain a request for which no provisional row was ever inserted.

Best-evidenced hypothesis for the owner's reported attempt, not a confirmed fact: the attempt did not reach the current local process/database target that was inspected—most plausibly the pre-v6 stale listener or a different Supabase project/URL was used. The owner’s SQL Editor project and the original attempt’s request log or analysis-run ID were not available, so this workspace cannot prove which of those two external-routing possibilities occurred. The local current-code path and the `.env` target are ruled out as causes of failure by the successful reproduction above.

No backend fix was made because the reproduction did not fail. The current-code server is left running on port 8000.

## Google Docs Manuscript Ordering Diagnostic

Run date: 2026-10-23 (Asia/Manila).

### What the backend actually does

The Google Docs path does not use the Google Docs API and does not scrape the rendered editor page. `DocumentIngestionService.extract_google_doc_id()` extracts the `/document/d/{id}` segment at `services/ingestion.py:14-23`, then `fetch_plaintext_from_gdoc()` performs an `httpx` request to:

```text
https://docs.google.com/document/d/{id}/export?format=txt
```

That URL is constructed at `services/ingestion.py:31-32` and fetched at `services/ingestion.py:39-46`. The production scan calls that service at `main.py:603-617`, then passes the returned plaintext to `ManuscriptParserService.parse_manuscript_sections()`.

The preview route is a second, duplicated plaintext-export fetch: it builds the same `format=txt` URL at `main.py:1473`, fetches it at `main.py:1481-1494`, and returns `strip_document_noise()` output at `main.py:1505-1519`. That route does not call the parser. Its cleaner normalizes line endings/blank lines and removes figure/table/chart caption lines; it does not sort the remaining lines.

No concrete Google Docs URL exists in this repository, and no owner-provided public/authenticated document was available for a real network reproduction. The reproduction below therefore uses a local plaintext fixture matching the exact `format=txt` payload shape requested by the code. It confirms backend parser ordering loss without making a claim about Google’s handling of tables, columns, headers/footers, or other visual-only layout constructs that are not represented in this export.

### Confirmed root cause

Before this fix, the auto-detection loop in the current `services/parser.py:353-392` area stored content under `sections_lines[role]` (the pre-fix file used that role key directly). A later heading that mapped to an already-seen canonical role, such as `Background` mapping to `introduction`, was appended to the first role bucket. The output dictionary consequently exposed one `introduction` entry before `methodology`, even when the later introduction-role content appeared after methodology in the source.

The template path had a separate ordering defect: `parsed_sections_lines` is initialized from `template_toc` at `services/parser.py:238`, and the pre-fix comprehension (originally `services/parser.py:258`) emitted that template order rather than source encounter order. The `canonical_order` list is now at `services/parser.py:343-347` and is used only for the confidence penalty at `services/parser.py:409-418`; it is not an output sort.

### Fix

- Repeated auto-detected roles now receive deterministic occurrence keys such as `introduction` and `introduction (2)` at `services/parser.py:373-383`, preserving each content block and its source insertion order.
- The template path records encountered section keys and emits present sections in encounter order, then appends template sections that were absent (`services/parser.py:239-286`).
- `section_roles` maps each occurrence key back to its canonical role. The production response now uses the parser’s explicit `section_order` at `main.py:620`, `main.py:934`, and `main.py:980`.
- Scoring remains role-based by design: `services/scoring.py:204-229` still concatenates repeated occurrence keys under the canonical role, in source order, for reasoning/coherence. This preserves the existing scoring contract while keeping the returned/analyzed section sequence faithful to the source. Repeated role occurrences are therefore separate in ordered output but intentionally grouped for role-level scoring.

### Reproduction and captured output

Fixture: `tests/fixtures/google_docs_plaintext_ordering_export.txt`. The source heading sequence is `Introduction -> Methodology -> Background -> Results`, where `Background` maps to the same role as `Introduction`.

Running the pre-fix `HEAD` parser against that fixture produced:

```text
FORMAT=txt_SIMULATION=local_plaintext_export
SOURCE_HEADINGS= ['Introduction', 'Methodology', 'Background', 'Results']
PARSED_KEYS= ['introduction', 'methodology', 'results']
INTRODUCTION_BODY_MARKERS= ['INTRODUCTION_FIRST', 'INTRODUCTION_SECOND']
METHODOLOGY_BODY_MARKERS= ['METHODOLOGY_BODY']
RESULTS_BODY_MARKERS= ['RESULTS_BODY']
```

After the fix, the same fixture and parsing path produced:

```text
FORMAT=txt_SIMULATION=local_plaintext_export
SOURCE_HEADINGS= ['Introduction', 'Methodology', 'Background', 'Results']
PARSED_KEYS= ['introduction', 'methodology', 'introduction (2)', 'results']
SECTION_ORDER= ['introduction', 'methodology', 'introduction (2)', 'results']
INTRODUCTION_BODY_MARKERS= ['INTRODUCTION_FIRST']
METHODOLOGY_BODY_MARKERS= ['METHODOLOGY_BODY']
INTRODUCTION (2)_BODY_MARKERS= ['INTRODUCTION_SECOND']
RESULTS_BODY_MARKERS= ['RESULTS_BODY']
ROLE_GROUPED_INTRO_MARKERS= ['INTRODUCTION_FIRST', 'INTRODUCTION_SECOND']
```

The template-order reproduction also passed after the fix:

```text
SOURCE_HEADINGS= Results -> Introduction -> Methodology
TEMPLATE_ORDER= Introduction -> Methodology -> Results -> Discussion
PARSED_KEYS= ['Results', 'Introduction', 'Methodology', 'Discussion']
SECTION_ORDER= ['Results', 'Introduction', 'Methodology', 'Discussion']
```

### Verification

```text
.venv\Scripts\python.exe -m pytest -q
......................................                                   [100%]
38 passed in 47.48s
```

The new ordering regression tests are in `tests/test_parser_ordering.py`; the full suite passed. A real Google Docs fetch was not verified because this workspace had no concrete public/authenticated document URL, so visual-layout flattening outside the plaintext export remains an external limitation rather than a confirmed backend behavior.

## Modal ML Offload

The API defaults to `ML_BACKEND=local`. `ML_BACKEND=modal` uses the Modal SDK with `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`; no web endpoint is exposed. The existing scoring formulas, PAR score, bands, v6 checkpoint key translation, and 40-sentence masking cap were not changed.

### Call-site map

| Before | After |
| --- | --- |
| `main.py:27-33,49` imported torch, the embedding singleton, explainability, and AI text at startup | `main.py:25` imports the light `ml_client`; `main.py:199,221,252,614` load the parser only when needed. Modal-mode `import main` loads none of the heavy ML modules. |
| `main.py:233,263` called `embedding_service.compute_coherence` in the debug endpoints | `main.py:227-228,260-261` call the ML client batch. |
| `main.py:626` called `embedding_service.compute_coherence` for a production scan | `main.py:627-630` sends parsed sections, aggregated role text, and all seven role pairs in one batch to `services/ml_worker.py:6-17`. The worker calls the unchanged `services/embedding.py:92` and `services/embedding.py:203`. |
| `main.py:674` called `predict_coherence` once per role pair | `main.py:671` consumes all v6 dimensions returned by the first batch. |
| `main.py:683,690` called `explain_pair` per qualifying pair | `main.py:673-684` collects qualifying pairs and calls the second batch once; `services/ml_worker.py:20-23` invokes the unchanged `services/xai_utils.py:9` for each pair inside the ML container. |
| `main.py:892-894` called `compute_manuscript_ai_text_indicator` with `embedding_service._model` | `main.py:899` uses the first batch's AI-text result; the same function runs with the same embedder in `services/ml_worker.py:16`. |

`services/ml_client.py:6-33` selects local or Modal execution. `modal_app.py:27-41` defines the CPU worker and loads the v5, v6, and spaCy language models at container entry. A production scan makes **one Modal call** for embeddings, v6 dimensions, and AI-text, plus **one Modal call** for all qualifying sentence-masking explanations. The second call is skipped when no pair qualifies. Inputs and outputs are JSON-shaped plain data.

### Dependencies and deployment

`requirements.txt` is the Render light set, including `modal~=1.6.2` and excluding torch, sentence-transformers, transformers, and `en_core_web_sm`. `requirements-ml.txt` holds the CPU PyTorch index, `torch==2.13.0+cpu`, sentence-transformers, and the `en_core_web_sm` 3.8.0 wheel. Local development installs both files. `modal_app.py:5-14` adds the ML requirements, spaCy, NumPy, service code, and both model artifacts to the Modal Python 3.12 image. It requests 1 CPU, 2048 MiB, a 600-second timeout, and `min_containers=0`. The Render Dockerfile installs only `requirements.txt`; `.dockerignore:8` excludes `models/` from its build context. `render.yaml:13-18` sets `ML_BACKEND=modal` and declares both token variables as unsynced secrets.

The fresh Windows Python 3.12 venv at `C:\Users\NHMIER\.claude\jobs\4a24cd9e\tmp\render-venv` installed **only** `requirements.txt`. With `ML_BACKEND=modal`, `import main` measured **75.0 MB total RSS** via psutil (4.2 MB venv launcher plus 70.8 MB Python child), well under the 512 MB target. Its output was:

```text
READY torch=False sentence_transformers=False en_core_web_sm=False
pid=10268 rss_mb=4.2
pid=29504 rss_mb=70.8
total_rss_mb=75.0
```

### Verification

```text
.venv\Scripts\python.exe -m pytest -q
........................................                                 [100%]
40 passed in 63.15s (0:01:03)

.venv\Scripts\python.exe -m pytest -q tests/test_ml_modal_parity.py
.                                                                        [100%]
1 passed in 93.66s (0:01:33)

.venv\Scripts\python.exe -m pytest -q tests/test_ml_modal_parity.py::test_modal_error_is_visible
.                                                                        [100%]
1 passed in 2.88s

modal_app.py AST parse and import: app resync-ml; modal 1.6.2
ML_BACKEND=modal import main (full dev venv): torch=False, transformers=False,
sentence_transformers=False, en_core_web_sm=False, spacy=False
```

`tests/test_ml_modal_parity.py` runs a realistic multi-section scoring and finding path through the same function bodies exposed by `modal_app.py`, with JSON encode/decode on arguments and results. It compares local and simulated Modal scores, v6 dimensions, findings, and sentence evidence, and confirms two batch calls. Its second test confirms a Modal connection error is surfaced with a clear message. The existing `main.py:1041-1098` background-job exception path records that message as `status="failed"` and refunds the scan credit; no fallback score is produced.

The installed Modal SDK is **1.6.2**. The verified API used is `modal.App("resync-ml")`, `modal.Image.debian_slim(python_version="3.12").pip_install(...).pip_install_from_requirements(...).workdir(...).add_local_dir(...).add_local_file(...)`, `@app.cls(cpu=1.0, memory=2048, timeout=600, min_containers=0)`, `@modal.enter()`, `@modal.method()`, and `modal.Cls.from_name("resync-ml", "MLWorker")().analyze_batch.remote(...)` / `.explain_batch.remote(...)`. The installed SDK's `modal/config.py` confirms `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are the token environment variables.

Deviation: the parser import was also made lazy so spaCy cannot opportunistically import an installed torch package during modal-mode `import main` in a full local venv. The parser still runs on Render during each scan. No Modal deployment or live remote call was made because the owner has no Modal account yet; image build and remote authentication remain unverified.

### Owner setup steps

1. Create a Modal account. Install the CLI with `python -m pip install modal` (or install both repository requirement files for local development: `python -m pip install -r requirements.txt` and `python -m pip install -r requirements-ml.txt`).
2. Run `modal token new` on your machine.
3. From the repository root, run `modal deploy modal_app.py`. Confirm that the deployed app is named `resync-ml` and exposes `MLWorker`.
4. In Render, set `ML_BACKEND=modal`, `MODAL_TOKEN_ID=<your token ID>`, and `MODAL_TOKEN_SECRET=<your token secret>` on the backend service. Keep the existing Supabase and Gemini settings. Redeploy Render so it builds from the light requirements.
5. Submit an authenticated manuscript to `POST /api/scans/start`, then poll `GET /api/scans/{analysis_run_id}` until `status` is `completed`. Confirm the response contains section scores, `score_breakdown.v6_dimensions`, and any qualifying finding evidence. If it reports `failed`, inspect the returned error and the Render/Modal logs; Modal errors are not replaced with default scores.

## Modal Live Test

Run on 2026-10-24 against the deployed `resync-ml` / `MLWorker`, using the repository `.venv`. No deployment, commit, or push was made. The direct probe used eight three-sentence sections: abstract, introduction, objectives, methodology, results, discussion, conclusion, and references. `aggregate_role_texts` supplied the role text and synthetic `__document__` text. The pair list came from `ROLE_PAIR_WEIGHTS`.

### Task 1 — direct worker call: PASS

Captured output: first `analyze_batch.remote` **19.985 s**; immediate second call **0.895 s**. Response keys were `coherence`, `v6_dimensions`, `ai_text`. `coherence` contained `overall_score`, `section_scores`, `empty_sections`, `embeddings`; `ai_text` contained `overall_score`, `section_scores`, `flagged_sections`, `disclaimer`. Each of the eight section embeddings had **384** values. Every pair returned all five non-null v6 dimensions (`objectives_alignment`, `methodology_fit`, `numeric_consistency`, `claim_support`, `overall_coherence`); there were no `None` values because every role, including `__document__`, had text longer than the worker's five-word gate.

| Pair | Objectives alignment | Methodology fit | Numeric consistency | Claim support | Overall coherence |
| --- | ---: | ---: | ---: | ---: | ---: |
| objectives::methodology | 0.46689129 | 0.46370196 | 0.50068617 | 0.46723956 | 0.50735891 |
| methodology::results | 0.48693544 | 0.44479162 | 0.51105148 | 0.48752189 | 0.51079369 |
| results::discussion | 0.30931848 | 0.24956772 | 0.33050087 | 0.26293278 | 0.27857506 |
| objectives::conclusion | 0.37245554 | 0.35242769 | 0.41630989 | 0.33033130 | 0.37112427 |
| introduction::objectives | 0.47873306 | 0.46718201 | 0.51006758 | 0.45756757 | 0.53076863 |
| abstract::__document__ | 0.49906191 | 0.45207563 | 0.54089171 | 0.45655629 | 0.54741514 |
| discussion::conclusion | 0.32962707 | 0.28809288 | 0.35818169 | 0.25707066 | 0.28365481 |

### Task 2 — local parity and explanation: PASS

The same input ran through `services.ml_worker.analyze_batch` locally. Maximum absolute difference across all v6 values was **1.4901161193847656e-7**; across all embedding coordinates it was **1.564621925354004e-7**. The first and second remote v6 results matched exactly (maximum difference **0.0**). No pair's `overall_coherence` exceeded 0.80, so `objectives::methodology` was forced through `explain_batch.remote`; its remote baseline was **0.5073589086532593** and the explanation call took **1.188 s**. The maximum numeric difference from local `explain_batch` (baseline and evidence deltas) was **5.960464477539063e-8**; sentence text and list structure matched. Remote evidence had A: two supporting sentences (deltas 0.16925845, 0.04805008) and one undermining (-0.01104134); B: two supporting (0.10209078, 0.08398521) and one undermining (-0.01543599).

### Task 3 — authenticated API scan: FAIL (resource check incomplete)

A temporary confirmed Supabase user signed in successfully. The temporary `ML_BACKEND=modal` server on 127.0.0.1:8010 accepted the in-memory eight-heading DOCX: `UPLOAD={"http_status":202,"body":{"status":"processing"}}`. After nine polls and **44.305 s** from upload start, the result was `status="completed"`, `db_save_status="success"`, `error_message=null`. `score_breakdown.v6_dimensions` held all seven keys listed in Task 1; every dimension was non-null for every pair. The server log showed a Gemini **429** quota response and no Modal error marker or scan-job-failed marker. The Gemini quota response was separate from the successful Modal scan.

The script reported `server_rss_mb=4.2`, but on Windows that PID was the Python launcher. It did not inspect the serving child process's RSS or loaded modules, so this reading **does not establish** that the server avoided loading torch or sentence-transformers. That missing resource check makes the full task incomplete. The first cleanup attempt also returned Supabase `403 User not allowed`: signing in on the admin client had replaced its service-role session. Cleanup succeeded with a fresh service-role client, as recorded under Task 5. No API scan was repeated.

### Task 4 — Modal-side CLI evidence: NOT-RUN

Verification stopped after the Task 3 resource-check failure. `python -m modal app list` and recent `modal app logs` were not run; Modal-side runtime errors and container seconds were therefore not observed. The direct remote calls and API scan establish that the deployed class responded, but no Modal billing or compute-duration figure was available from the captured output.

### Task 5 — cleanup: PASS after retry

The temporary server launcher was terminated. The immediate port check reported `PORT_8010_FREE=False`, but a later socket connection to 127.0.0.1:8010 returned Windows code **10061** (connection refused), and `Get-NetTCPConnection -LocalPort 8010 -State Listen` returned no listener. With a fresh service-role client, the temporary user deletion request succeeded; a follow-up admin lookup returned **`User not found`**. No temporary user or listener was left by this test.

## Render-Like Scan Test

Run on 2026-10-24 from the `development` working tree. The repository already had uncommitted changes before this run; this test changed only this report. No commit, push, Render action, or Modal deployment was made.

### 1. Render-like copy: PASS

Copied the current working tree to `C:\Users\NHMIER\.claude\jobs\4a24cd9e\tmp\render-sim\app` with the `.dockerignore` exclusions (`.venv/`, `__pycache__/`, `*.pyc`, `.git/`, `.env`, `*.log`, `tests/`, `models/`, `.pytest_cache/`). Copied `.env` into that directory only for this local run. Captured top-level listing and checks:

```text
.claude  graphify-out  migrations  services  .dockerignore  .env  .gitignore
check_models.py  check_requirements.py  Dockerfile  main.py  modal_app.py
render.yaml  requirements-ml.txt  requirements.txt  V6_BACKEND_INTEGRATION_REPORT.md
MODELS_EXISTS=False
TESTS_EXISTS=False
ENV_COPY_EXISTS=True
```

### 2. Requirements-only venv: PASS

Reused `C:\Users\NHMIER\.claude\jobs\4a24cd9e\tmp\render-venv`. Its installed versions satisfied every current `requirements.txt` specifier, so it was not rebuilt. Captured checks:

```text
fastapi==0.141.1  modal==1.6.2  spacy==3.8.16
supabase==2.31.0  uvicorn==0.52.4
REQUIREMENTS_MATCH=True
FORBIDDEN={'torch': False, 'sentence_transformers': False, 'transformers': False, 'en_core_web_sm': False}
No broken requirements found.
```

The four forbidden packages were absent from `pip list` and their import specs were absent. The venv also had no `psutil`; memory measurement used the separate development venv outside the serving process.

### 3. Server and authenticated scan: PASS

Started the copied app with `ML_BACKEND=modal` and `python -m uvicorn main:app --host 127.0.0.1 --port 8010`. Local Modal authentication used the existing configuration; no `MODAL_TOKEN_*` variables were set. Startup output:

```text
INFO:     Started server process [18880]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8010 (Press CTRL+C to quit)
```

Created a temporary confirmed Supabase user through a service-role client, signed in through a separate anon-key client, and uploaded an in-memory DOCX with eight headed sections and several sentences in each. Captured API summary:

```text
TEMP_USER_CREATED=True
SEPARATE_CLIENT_SIGN_IN=True
UPLOAD_HTTP=202
RUN_ID=ae1f34a2-6c32-4cbb-b3fd-6117e2b7710c
SCAN_RESULT={"status":"completed","seconds":64.922,"polls":10,"db_save_status":"success","error_message":null,"v6_pairs":["results::discussion","methodology::results","abstract::__document__","discussion::conclusion","objectives::conclusion","objectives::methodology","introduction::objectives"],"v6_all_non_null":true}
```

All five v6 dimension values were non-null for each of the seven returned pairs. The captured server output had no explicit Modal diagnostic line and no `ImportError` or `ModuleNotFoundError`. It showed the upload and polls returning 202/200, plus this separate Gemini quota line (credential masked):

```text
INFO:     127.0.0.1:64966 - "POST /api/scans/upload HTTP/1.1" 202 Accepted
Strong-pair verification failed, degrading to empty: Client error '429 Too Many Requests' for url 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key=[MASKED]'
INFO:     127.0.0.1:65121 - "GET /api/scans/ae1f34a2-6c32-4cbb-b3fd-6117e2b7710c HTTP/1.1" 200 OK
```

### 4. Serving process memory: PASS

Measured the uvicorn PID with `psutil.Process(18880)` and summed its RSS with all recursive descendants. The process tree contained the serving `python.exe` itself, not a launcher PID. Windows `peak_wset` is the process peak working set since start.

```text
STARTUP_MEMORY={"tree":[[18880,"python.exe",74.0]],"rss_mb":74.0,"peak_wset_mb":74.1}
FINAL_MEMORY={"tree":[[18880,"python.exe",145.4]],"rss_mb":145.4,"peak_wset_mb":145.9}
```

Both observed RSS readings and the recorded peak working set were below 512 MB.

### 5. Cleanup and repository state: PASS

Stopped the server before deleting the user. A fresh service-role client performed the deletion and follow-up lookup. Removed the entire temporary `render-sim` directory, including its `.env` copy and probe scripts; kept `render-venv`.

```text
DELETE_REQUEST=success
FOLLOWUP_LOOKUP=AuthApiError:User not found
SERVER_PID_ALIVE=False
PORT_8010_LISTENER=False
TEMP_COPY_EXISTS=False
VENV_EXISTS=True
```

The pre-run and post-run `git status --short` output matched exactly; only this report's contents were appended. No repository source file was edited by this test.

```text
 M .dockerignore
 M Dockerfile
 M V6_BACKEND_INTEGRATION_REPORT.md
 M main.py
 M render.yaml
 M requirements.txt
?? debug.log
?? modal_app.py
?? requirements-ml.txt
?? services/ml_client.py
?? services/ml_worker.py
?? tests/test_ml_modal_parity.py
```
