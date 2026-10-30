# v7 four-part scoring contract

The API computes this score for scans and `/analyze_v7`; the Modal worker still returns its raw score. `result_version` remains `v7`.

| Field or behavior | Previous | Current | Reason |
|---|---|---|---|
| Manuscript score and band | Pair-weighted `overall_coherence` × 100 | Equal mean of applicable dimension averages × 100, rounded to two decimals; band thresholds remain 85/70 | Four approved parts have equal influence. |
| `score_breakdown.dimensions` | Mean of each model head over all scored pairs | Plain mean over scored pairs; Numeric uses only text-applicable pairs and is null if none | A nonnumeric comparison must not dilute Numeric. |
| `score_breakdown.dimension_weights` | Absent | `1/n` for each applicable dimension, `0` otherwise | Explain the dynamic score weighting. |
| `/analyze_v7.dimensions`, `/analyze_v7.dimension_weights` | Absent | Same averages and weights as the scan | Both entry points expose the same calculation. |
| `manuscript_flow[].pair_score`, `numeric_applicable` | Absent | Plain mean of applicable pair dimensions (0–1), and text-based Boolean | Ranking and findings reflect the approved parts. |
| Pair selection, finding `coherence_score`, pointer `score`, recommendation priority | Raw `overall_coherence` | `pair_score` (× 100 where the existing field is percent) | Pair assessments agree with the new score. |
| Raw `scores.overall_coherence`, `pair_weight`, pointer `overall_coherence` | Model values | Preserved | Existing clients can still read raw outputs. |

Numeric applies when both adjacent section texts contain figures (`NUMERIC_RULE = "both"` in `services/v7_scan.py`). The alternative `"either"` uses at least one section. Citation years, heading labels, page/list numbers, and DOI/URL digits are excluded. Skipped pairs contribute no score. With no scored pairs the manuscript score is `0.0`. Older saved rows without the added fields remain valid.

## P17 paywall and manuscript amendment

| Field or behavior | Previous | Current | Reason |
|---|---|---|---|
| Scan credit | One credit deducted on creation and refunded on failure | Scans cost zero credits; the existing rate limit remains | Users can inspect a preview before paying. |
| `manuscript_sections` | Absent from saved results | Ordered merged `{name, text}` entries for every new scan | Rebuild the Word or Google manuscript from the saved result. |
| `recommendations[]` | A Gemini subset with pair order but no stable display ID | Every verified culprit and finding quote in each explained worst-12 pair has a recommendation; `id = r-<pair_order>-<n>`, `section`, and `pair_order` identify it | Each source-backed highlight links to one recommendation. Unverified originals are dropped. |
| `access` | Absent | New results start `{unlocked:false, unlock_cost:1, locked_counts:{sections,recommendations,citations}}`; unlock sets true and counts to zero | The client can render the preview and paywall. Absence on older results means full access. |
| Locked result routes | Full saved result on GET and synchronous responses; Google preview fetched full source | One projection returns first section text, first two recommendations in manuscript order, all section names, scores, weights, band and counts; it removes later text, citations, pair culprits, findings, pair explanations and summary. The manuscript route uses the same projection. | Prevent paid content from leaking through another result route. |
| `POST /api/scans/{id}/unlock` | Absent | Authenticated owner can unlock once for one credit. A single database transaction locks the row, debits the wallet, appends the ledger entry, and updates `result_json`; repeat requests return `charged:false`. Insufficient credit returns 402. The credit kill switch unlocks without a charge. | Atomic and idempotent unlock, including concurrent requests. |

Install `migrations/009_scan_unlock.sql` in the Supabase SQL editor before enabling the paywall. Existing results without `access` remain fully visible.

## P21 locked manuscript blur

Locked `manuscript_sections` still expose every `name`, the first section's `text`, and `text: null` for later sections. Each withheld section also has `placeholder: {heading, paragraphs}` made only from a fixed neutral vocabulary. Its heading matches the real heading's character length; paragraph lengths are rounded to 40 characters, capped at about 4,000 characters per section and 40,000 across the response. The filler is deterministic from `analysis_run_id`. The browser blurs it as manuscript text and hides it from selection and assistive technology, with a centered unlock card above it. Unlocked and older results have no placeholder.

## Recommendation category

Each new `recommendations[]` item has `category`: `objectives`, `methodology`, `claims`, or `numeric`. It identifies which four-part score the recommended revision fixes, so the client can use the matching card colour for its highlight. Gemini supplies it in the existing per-pair call. Case-insensitive values and the aliases `objectives_alignment`, `methodology_fit`, `claim`/`claim_support`, and `numbers`/`numeric_consistency` are normalized. Missing or invalid values, including review recommendations added when Gemini omits a required passage, use the pair's weakest applicable dimension; numeric participates only when `numeric_applicable` is true. The locked projection preserves `category` on its two visible recommendations. Older stored results are not backfilled and may omit the field.

## P36 Gemini rate limit and scan status

Every Gemini request, per-pair, summary and retries included, takes a slot in a process-wide rolling 60 s window of `GEMINI_RPM` requests (default 15). Concurrency stays capped at 4 per scan. On HTTP 429, or 503 whose body says `overloaded`, the call waits for the server's `RetryInfo.retryDelay` or `Retry-After`, or for a window slot, then retries. One call waits at most about 180 s in total; past that it falls back exactly as before. Scores, prompts and normalization are unchanged.

`GET /api/scans/{id}` while `status` is `processing` adds two nullable fields:

| Field | Value |
|---|---|
| `status_message_level` | `"info"` when the window is at least 80% full (12 of 15 by default) or the scan needs more Gemini calls than the window allows; `"wait"` while a call waits for a reset or after a 429/503 |
| `status_message` | `"The AI is busy right now, so your scan may take a little longer than usual."` for `info`; `"Waiting for the AI to be ready again (about N seconds)."` for `wait` |

Both are `null` once the AI is back to normal and on `completed` or `failed` responses. The state is held in process memory keyed by `analysis_run_id`, so no migration is needed; a server restart clears it, and it is only visible to pollers on the instance running the scan.
