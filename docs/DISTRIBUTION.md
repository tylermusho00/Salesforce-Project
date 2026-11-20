# Distribution: Capacity-Weighted Fair Round-Robin

> **The requirement.** "If multiple representatives share the exact same matching criteria and have
> available capacity, teams must design and implement their own distribution logic. Do not simply
> assign all leads to the first matching record."

Code: [`lead_router/distribution.py`](../lead_router/distribution.py) · used by
[`routing.py`](../lead_router/routing.py) · tests: [`tests/test_distribution.py`](../tests/test_distribution.py)
· try it: `leadrouter compare --dataset scale`

---

## The method in one sentence

**Give the lead to the rep who would be the least full after taking it. If that's a tie, give it
to whoever has received the least lead value so far. If still tied, whoever was served longest ago.**

That's four layers, each solving a specific problem:

| # | Layer | Rule | Problem it solves | Where the idea comes from |
|---|---|---|---|---|
| 1 | **Proportional share** | smallest *virtual finish time* `(leads held + 1) ÷ max capacity` | Reps have different capacities (Juniors 40-50, Seniors 20-25). Equal counts would fill small reps first. | **Weighted Fair Queuing** (Demers, Keshav & Shenker, 1989), how network routers share bandwidth between flows of different sizes |
| 2 | **Value fairness** | least lead value (sum of scores) received per slot | Equal counts can still be unfair: one rep could get all the 95-point leads, a teammate all the 10-point ones. Reps are paid on closed deals. | "Fair share of opportunity," not just of workload |
| 3 | **Round-robin** | served longest ago | When everything else ties (very common: everyone starts at 0), rotate instead of always picking the same rep. | Classic **round-robin scheduling** (time-sharing CPUs) |
| 4 | **Deterministic** | file order | Same input gives the same output, every time. Easy to audit, test and demo. | Stable sort |

In code it's just a sort key. The smallest tuple wins:

```python
"fair_share": lambda rep, value, last, pos: (
    (rep.current_capacity + 1) / rep.max_capacity,   # 1. virtual finish time
    value / rep.max_capacity,                        # 2. lead value already received per slot
    last,                                            # 3. served longest ago goes first
    pos,                                             # 4. reproducible last resort
),
```

This runs only *after* the hard rules have narrowed the candidates. A rep is only considered if
they match the lead's region, language and industry, have the right seniority (Critical leads go
to a Senior, then a Manager, never a Junior), and have a free slot.

---

## Worked example

Three Juniors in the same pool, all equally qualified: **Ana** (50 slots), **Ben** (20), **Cal** (20).
Nine leads arrive.

| Lead | Ana finish | Ben finish | Cal finish | Winner | Why |
|---|---|---|---|---|---|
| 1 | **1/50 = .02** | 1/20 = .05 | 1/20 = .05 | Ana | lowest finish time |
| 2 | **2/50 = .04** | .05 | .05 | Ana | still lowest |
| 3 | 3/50 = .06 | **.05** | **.05** | Ben | Ben and Cal tie → file order |
| 4 | .06 | 2/20 = .10 | **.05** | Cal | lowest |
| 5 | **.06** | .10 | .10 | Ana | lowest |
| 6 | **4/50 = .08** | .10 | .10 | Ana | lowest |
| 7 | .10 | **.10** | .10 | Ben | 3-way tie → Ben and Cal have less value per slot than Ana → Ben was served longer ago |
| 8 | .10 | .15 | **.10** | Cal | Ana and Cal tie → Cal has less value per slot |
| 9 | **.10** | .15 | .15 | Ana | lowest |

**Result: Ana 5 of 50, Ben 2 of 20, Cal 2 of 20. Everyone is exactly 10% full**, and the picks are
interleaved (Ana, Ana, Ben, Cal, Ana, Ana, Ben, Cal, Ana), never one long streak.

The same nine leads with the alternatives:

| Strategy | Order | Result |
|---|---|---|
| **fair_share (ours)** | Ana Ana Ben Cal Ana Ana Ben Cal Ana | 5/50, 2/20, 2/20: **all 10% full** |
| round_robin | Ana Ben Cal Ana Ben Cal Ana Ben Cal | 3/50, 3/20, 3/20: Ana 6%, Ben and Cal 15%, so the small reps fill **2.5× faster** |
| first_match | Ana × 9 | 9/50, 0, 0: Ben and Cal sit idle (the brief forbids this) |

---

## Measured on the real 50,000-lead dataset

Fairness is measured with **Jain's fairness index** (Jain, Chiu & Hawe, 1984):

```
J = (Σx)² / (n · Σx²)        x = each rep's % of capacity used
J = 1.0  → perfectly even          J = 1/n → one rep has everything
```

It's computed inside every group of *interchangeable* reps (same region, language, industry and
seniority) and averaged by group size. Juniors aren't compared with Managers, because Managers are
held in reserve on purpose.

`leadrouter compare --dataset scale` prints:

**Batch mode** (how the pipeline runs: Critical leads first)

| Strategy | Workload fairness (during run) | After first 10% | Value fairness | Assigned | Critical → Senior |
|---|---|---|---|---|---|
| **fair_share (ours)** | **0.9916** | **0.9912** | **0.9977** | 99.83% | 98.64% |
| least_loaded | 0.9916 | 0.9912 | 0.9976 | 99.83% | 98.64% |
| round_robin | 0.9877 | 0.9816 | 0.9960 | 99.83% | 98.64% |
| first_match | 0.6204 | **0.4178** | 0.8409 | 99.83% | 98.64% |

**Live mode** (leads routed in the order they arrive, priorities mixed, like a real day)

| Strategy | Workload fairness (during run) | After first 10% | Value fairness | Assigned | Critical → Senior |
|---|---|---|---|---|---|
| **fair_share (ours)** | **0.9973** | 0.9867 | 0.9925 | 99.92% | **95.68%** |
| least_loaded | 0.9966 | 0.9908 | 0.9864 | 99.92% | 94.24% |
| round_robin | 0.9892 | 0.9817 | 0.9943 | 98.39% | **62.25%** |
| first_match | 0.5733 | **0.1272** | 0.9642 | 99.91% | 88.98% |

"During run" averages 10 snapshots taken as leads are handed out. A manager looks at the queue
mid-day, not only at the end.

### How to read this (honestly)

* **first_match is clearly worst.** After the first 10% of leads, fairness is 0.13–0.42: a few reps
  are buried while their teammates sit idle.
* **round_robin looks fine on paper but breaks the business.** Because it ignores capacity, Seniors
  fill up with ordinary leads early. In live mode only **62%** of Critical leads reach a Senior,
  versus **96%** with fair_share.
* **least_loaded is our method's closest relative.** It's layer 1 without the look-ahead, value
  fairness and rotation, and on the batch run the two tie. fair_share pulls ahead when priorities
  arrive mixed (live mode): better value fairness (0.9925 vs 0.9864), better workload fairness
  (0.9973 vs 0.9966), and more Critical leads to Seniors (95.68% vs 94.24%). It also guarantees
  rotation on ties instead of relying on file order.
* **Batching Critical leads first is part of the design.** The same algorithm gets 98.64% of
  Critical leads to Seniors in batch mode versus 95.68% live, because urgent leads get first pick of
  Senior capacity.

---

## Why not something else? (Q&A prep)

**"Why not plain round-robin?"** It treats a 20-slot Senior and a 50-slot Junior the same, so the
Senior is full at 40% team utilization and Critical leads start spilling over (62% Critical → Senior
in live mode).

**"Why not random assignment?"** Fair on average, but not reproducible (you can't demo or test it
reliably) and unfair in the short run, which is exactly when a manager is watching.

**"Isn't this just least-loaded?"** Layer 1 is least-loaded done properly (it looks one lead ahead,
which is what Weighted Fair Queuing does). Layers 2–4 add value fairness, guaranteed rotation and
determinism. The live-mode numbers show the difference.

**"How fast is it?"** A pool holds about 10–30 reps, so each pick checks a handful of reps:
50,000 leads route in under a second. The fairness snapshots add almost nothing (10 passes over
1,600 reps).

**"What would you add next?"** (1) A **Senior reserve** for live mode: keep e.g. 15% of each Senior's
slots for Critical leads, which would push the 96% closer to batch-mode's 98.6%. (2) **Performance
weighting**: tilt the share toward reps with higher close rates, once that data exists. Both slot in
as extra terms in the same sort key.

**"Can you switch it?"** Yes, without touching code: `routing.distribution_strategy` in
`config/rules.yaml`, or `leadrouter run --strategy round_robin`.
