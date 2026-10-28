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
