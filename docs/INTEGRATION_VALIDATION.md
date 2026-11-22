# Combined-version validation — 2026-11-21

Local starting commit: `6f36943`. Teammate commit merged: `4b4eb1f`.
The merge preserves both histories. Nothing has been pushed.

## What was combined

- Local: strict-mode toggle, unknown-industry labels, duplicate-ID protection, Manager Review
  suggestions, routing/cleaning optimizations, Rules Check, fairness, tier/reference comparison,
  Critical Leads Waiting, value audit, and messy-lead trace.
- Teammate: clearer chart styles and capacity groups, short explanations, assigned/review chart,
  lead-versus-rep comparison table, business scenarios, and Run Tests panel.
- Integration fixes: aligned layout IDs with callback outputs; retained both fairness and Perfect
  Matches; restored the comparison table in the drawer after explanations were shortened.
- Independent read-only review findings adopted: restore assigned-rep evidence in the drawer;
  replace unsupported “Seniors were full” wording with availability at the selected match level.
  No review findings rejected in this merge.

## Routing equivalence

The older `Desktop/lead-router-baseline` CSVs predate the unknown-industry label and lack the JSON
snapshots expected by the current baseline script. They are not an exact pre-merge baseline.
A temporary archive of the exact local starting commit was used to generate fresh CSV and JSON
snapshots, then compared with the merged version.

Across 100 sample and 50,000 scale leads, every decision field except explanatory wording is
identical: status, rep, match level, review reason, priority, score, rep identity/seniority, suggested
action and suggested rep. Cleaned leads/reps, data-quality reports and non-timing metrics are identical.
All explanation strings intentionally changed to the teammate's shorter style.

| Result | Best available, scale | Strict, scale |
|---|---:|---:|
| Assigned | 49,913 (99.83%) | 47,021 (94.04%) |
| Manager Review | 87 | 2,979 |
| Critical leads waiting | 0 | 6 |
| Fingerprint | `e23e7406c7b2` | `e4ba9e24cf2e` |

Best available scale: 50,000 rows in/out; 509 reused IDs renamed and retained; zero Critical leads
to Juniors; zero reps over capacity; Jain fairness index 0.9916. Sample assignment rates remain
99% Best available and 19% Strict. Strict retains the existing region/language fallback when the
lead's industry is unknown; it is explicitly labelled, and is not a true 3-trait match.

## Performance

Same-machine session comparison, median of five end-to-end scale runs after a warm-up:

| Version | Median |
|---|---:|
| Teammate remote version | 6.49 s |
| Combined version | 4.48 s |

This session observed about 31% lower elapsed time, with the same 49,913 assignments and 87 review
leads. Browser work and varying host load overlapped parts of the session, so this is indicative,
not a controlled speedup guarantee. A separate combined-version bench measured 3.04 s; the older
1.30 s result in PERFORMANCE.md is historical and was not reproduced today. Run
`python scripts/bench.py scale 5` to measure under current conditions.

## Verification

- Full suite: `python -m pytest -q --benchmark-disable` (234 tests).
- Added regressions for drawer rep/trait evidence, accurate Manager wording when a weaker-match
  Senior has room, and Run Tests preserving an active Strict-mode dashboard run.
- Browser checks cover scale runs, matching modes, Rules Check, trace drawer, Manager Review,
  Data Quality, Run Tests, Reset/repeat fingerprint, and console errors.
- Existing Dash pytest-plugin deprecation warning remains; it does not fail the tests.

The Run Tests panel displays eight scenario cards; pytest has eleven business-scenario tests,
including two extra small edge cases and the run-all integration check. These checks use fixed
Best available scenarios, regardless of the active dashboard mode.
