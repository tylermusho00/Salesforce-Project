# Submission verification — 2026-11-21

Merged GitHub `b65c145` and the subsequent `de05e6d` local-launcher update into the local routing/evidence upgrades, audit fixes and polished capacity simulator. Both histories are preserved; no rebase or force push.

## Verified

- 353 tests passed in the separately installed Python 3.11 environment using `python -m pytest -q --benchmark-disable`. The dashboard's real pytest subprocess also passed all 353 cases and preserved the active run.
- Sample and scale: all 50,100 saved per-lead decisions match the pre-edit baseline exactly. Conservation, capacity and Critical-to-Junior checks pass.
- Dependencies installed from `.[dev,dashboard]`; `pip check` reports no broken requirements. Dash minimum is 3.1 for optional dynamic review controls, in both dependency manifests.
- Preserved local matching modes, raw-value evidence, reserved suggestions, infinite paging, exact KPI display, UI copy cleanup and capacity scenarios.
- Retained incoming Manager Review and Tests pages, manual assignment, deterministic advisory Isolation Forest flags and shared scale fixtures.
- Manual assignment refreshes metrics and remaining suggestions. An atomic expected-revision check rejects stale cards; manual edits are published on a copy. Regression tests cover replacement during card construction.
- Capacity comparisons explicitly reject manually overridden runs until automatic routing is rerun. Old previews are invalidated.
- Browser: all five pages, all three suggested-rep assignment buttons, searchable manual rep picker, assignment metrics, reruns, simulator manual-override guard and real full Tests-page execution. Earlier comprehensive control coverage is recorded in [UI_VERIFICATION.md](UI_VERIFICATION.md).
- Mobile Tests and Manager Review fit 390 pixels after long test names were allowed to wrap. Desktop review keeps the paged queue and assignment panel side by side.
- Independent read-only review: no remaining source blockers. Conflict-marker, whitespace and credential-pattern scans found no submission blockers.

## Limits

This is readiness for the repository and competition demonstration, not certification of an unseen official submission rubric. The available Q&A PDF is practice material. Hosted Linux/Python 3.12–3.13 CI must still be confirmed on GitHub after pushing. The existing shared in-memory run model is a prototype limitation; no multi-user production isolation or live Salesforce connection is claimed. Salesforce CSVs remain mapping templates that require org record IDs.

Local HANDOFF and historical council notes are preserved outside the submission commits. No generated CSVs, virtual environments or browser artifacts are included.
