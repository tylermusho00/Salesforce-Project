# UI verification — 2026-11-21

Applied Impeccable's distill/refine guidance and Arc's copy, layout and accessibility principles
to the existing Dash/Salesforce interface. Kept Impeccable v4.0.2 at the user's request.

## Changes

- Removed repeated instructions, duplicate report actions and decorative labels. Native buttons
  now navigate from report cards; data-quality actions scroll to their target.
- Moved routing methodology, scoring rules, source values, match breakdown and test explanations
  into disclosures. Labels and status information remain visible where needed to act.
- Simplified upload controls and simulator comparisons. Simulator input changes invalidate the
  preview; each rep is still compared independently without changing the active run.
- Improved the 390-pixel layout, constrained chart overflow during resizing and removed KPI
  count-up animation so results display exact values immediately.
- Fixed an infinite-grid loading stall reproduced during rapid filtering/sorting. Both queue
  and review grids debounce loading and allow replacement requests while earlier requests settle.
- Finished raw-row identity/conservation checks and retained source rep IDs in exports. The
  Salesforce export is explicitly named a mapping template and still requires real ID mapping.

## Browser coverage

Real Chromium interactions via Playwright CLI, using sample, 50,000-lead scale and synthetic CSVs:

| Area | Exercised |
|---|---|
| Navigation | All three pages; all Dashboard/About report actions; data-quality anchor; keyboard Enter navigation |
| Runs | Sample and scale; Best available and Strict; Reset; empty-state disabled actions |
| Queue | Global/local search and clear; every top-level filter option; grouping options; 13 column sorts/floating filters |
| Review | All four reason chips; 10 column sorts/floating filters; row drawer |
| Tables | Queue/review filter-menu buttons; rep column sorts; all three tables' First/Previous/Next/Last and 20/50/100 page sizes; disabled first-page boundary |
| Capacity | Region/seniority/sort options; rep search, Select All and Deselect All; three independent cases; changed-decision disclosures |
| Simulator guards | Empty or >3 reps; blank/zero/fractional/>50 slots; stale preview after input change; Reset and run replacement |
| Simulator benefit | Synthetic one-slot Senior team with two leads waiting: +2 slots clears both; active assignments unchanged |
| Uploads | Unsupported extension rejected; missing headers/empty CSV alerts and dismiss; leads-only/default team; custom reps; oversized numeric input |
| Exports | All four downloads; CSV contents and source IDs checked; renamed 50,000-row Salesforce mapping template checked |
| Details | Eight built-in checks, Run again and both close actions; blueprint, reference comparison and source-value disclosures; row scoring/original data; four trace-step links |
| Responsive | Dashboard, About and simulator at 1440×1000 and 390×844; mobile trace drawer; no horizontal page overflow on Dashboard/About |

The rapid queue filter/sort sequence that previously stalled completed after the loading fix.
The final scale checks returned 49,913 assigned in Best available and 47,021 in Strict.
No console errors were observed during the exercised flows. Screenshots were inspected in two
bounded batches. This covers application actions and table controls, not every third-party
filter-operator permutation, browser, device or assistive-technology combination.

## Automated and comparison evidence

- Full suite: **293 passed** on Python 3.11, including an editable install of `.[dev,dashboard]`
  in a fresh environment. One existing Dash pytest-plugin deprecation warning remains.
- Sample (100) and scale (50,000) saved decision CSVs match exactly. Cleaned leads/reps also
  match; the only quality/metric difference is the new `source_rows_preserved: true` flag.
- Impeccable detector on changed UI files: no findings. Independent read-only source review:
  no remaining substantive findings.
- Pipeline median of five warm runs: **1.583 s** for scale. One browser scale click-to-updated
  metric and tier-chart check: **3.561 s**. These are local observations, not an SLA or percentile.

Hosted Linux/Python 3.12–3.13 CI has not been executed in this session. The existing shared
in-memory run model remains; the simulator does not add per-user isolation. Changes are local
and have not been committed or pushed.
