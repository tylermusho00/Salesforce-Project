# Capacity what-if simulator

Open **Leads & Reps → Rep capacity → Capacity what-if** after running a dataset or upload.
Choose one to three representatives, enter 1–50 extra lead slots (default 5), and click
**Compare capacity**. Search the selector by rep, region, language or expertise.

Each selected representative is a separate scenario with the same added-slot budget. Scenarios
are independent: choosing three reps does not add capacity to all three at once. Other reps,
matching minimum, strategy, saved scores and original dispatch order stay the same. These are
hypothetical capacity changes, not new hires, staffing costs, Salesforce writes or automatic policy
exceptions.

Results show Critical waiting and Manager Review before/after, net additional assignments,
newly assigned/newly waiting leads, existing owners that move, and up to 20 changed-decision
examples. Ranking is among the selected scenarios only: Critical waiting reduction first, then
net additional assignments, then fewer existing owners changed, with rep ID breaking ties.
A no-benefit result does not by itself establish whether coverage, matching policy or capacity
elsewhere is the bottleneck. More capacity can change existing owners without reducing review.

## Isolation and replay

The service deep-copies cleaned leads and representatives for every replay, starts from zero
routing load as the original pipeline does, and preserves saved dispatch order and scores.
No active assignments, representative capacities, STATE, or default matching configuration are
updated. A no-change baseline replay must match saved placements before scenarios are accepted.
Currently configured routing rules are used; changed baseline behavior or an in-flight rules
change requires a fresh main run. This is not a historical rules snapshot.

Changing reps or extra slots marks the preview stale until compared again.
New runs and Reset hide old comparison results and clear selected reps. A run replaced during
calculation rejects the old comparison. The UI uses the application's existing shared in-memory
run model; this feature does not introduce per-user server sessions.

## Validation

- 23 focused simulator tests: benefit, no-benefit policy gap, Critical/Junior protection, independent
  ranking, invalid input, changed owners and example limits, stale-run behavior, and no mutation.
- Mixed-priority regression verifies Critical-first replay even when a Low lead appeared first in
  the CSV; a separate regression preserves an arrival-order run.
- Full current suite: **293 passed**, with the existing Dash pytest-plugin deprecation warning.
- Chrome: three-rep sample comparison, no-benefit outcomes, synthetic upload with one Senior slot
  with one Critical and one Low lead waiting; +2 slots resolves both. Reset clears the preview. No console errors.
- Scale smoke: 50,000 leads, three independent +5-slot scenarios, 8.759 seconds for comparison in
  this session. Original decision fingerprint and all rep loads/max capacities stayed unchanged.
  This is a single smoke measurement, not a performance guarantee or benchmark median.
- Independent read-only review caught the dispatch-order bug; the fix and regressions were
  re-reviewed with no remaining substantive findings.

The simulator lives in `lead_router/capacity.py` and `app/capacity_simulator.py`, with integration
in `app/main.py`. See [UI verification](UI_VERIFICATION.md) for the subsequent interface polish,
full click-through coverage and verification limits. Work remains uncommitted.
