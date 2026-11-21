# Performance: how long does 50,000 leads take?

**Short answer:** about **1.3 seconds** for 50,000 leads and 1,600 reps (it was 1.9 s before
the speed-up work). The slowest stage is **routing** (~0.5 s), followed by **cleaning** (~0.45 s).
Every decision, every cleaned value and every data-quality count is **identical** to before.

## Method

- Dataset: the bundled `scale` set (50,000 leads x 1,600 reps), same machine, same Python, same
  process for before and after.
- Timer: the pipeline times every stage itself (`PipelineResult.timings`).
  `python scripts/bench.py scale 5` runs the pipeline once to warm up, then 5 timed runs, and
  prints the **median** per stage (the median ignores one unlucky run).
- Finding the slow parts: `cProfile` on one run (`cProfile.run('run_dataset("scale")')`, sorted
  by time spent inside each function). Profiling slows everything down, so profile numbers are
  only used to *rank* functions, never quoted as timings.
- Proving nothing changed: `python scripts/baseline.py save <dir>` was run **before** any change.
  It stores every decision (lead, status, rep, match level, overflow reason, priority, score,
  explanation sentence, rep name/seniority, suggested action) plus the cleaned leads and reps,
  both data-quality reports (counts, examples, value audit) and all metrics. After each step
  `python scripts/baseline.py check <dir>` re-runs sample and scale and compares everything.

## Before / after (median of 5 runs, seconds)

| Stage   | Before | After | What it does |
|---------|-------:|------:|--------------|
| load    | 0.028  | 0.029 | read the two CSV files |
| clean   | 0.616  | 0.452 | fix messy values, build Lead / Rep objects, data-quality report |
| score   | 0.204  | 0.230 | points and priority tier (not touched; the difference is run-to-run noise) |
| route   | 0.977  | 0.536 | give each lead to a rep |
| metrics | 0.064  | 0.067 | KPIs for the dashboard (not touched; noise) |
| **total** | **1.887** | **1.308** | about 31 % less time |

Routing is still the slowest stage, but it is now about 1.8x faster; cleaning about 1.4x.
The first column is not the 3.1 s from the original profile: that number was measured with
the profiler switched on.

## What each optimisation did (plain English)

**Routing**

1. *Work out the rules once, not 50,000 times.* The list of pools a lead may use depends only on
   its region, language and industry, and the seniority groups depend only on its priority. There
   are only a few dozen distinct combinations, so each is computed once and reused.
2. *Take a full rep out of the pools by name instead of by comparison.* Before, a rep who became
   full was removed with `list.remove`, which compares the rep against list entries one by one using
   the dataclass's field-by-field equality (684,000 comparisons). Now each pool keeps its open reps
   in a dictionary keyed by rep ID, split by seniority, so removal is one lookup and a lookup for
   "free Seniors in this pool" never walks past Juniors or Managers.
3. *Build the fairness key inline.* To pick the fairest rep we compare, per candidate, a tuple
   (virtual finish time, value received per slot, last served, file position). Before, each candidate
   went through a helper method, a lambda and several dictionary lookups (1.5 million calls). The
   default `fair_share` strategy now builds the same tuple directly in the loop and keeps the
   smallest. The other strategies (`least_loaded`, `round_robin`, `first_match`) still use their
   original function.

**Cleaning**

4. *No `DataFrame.to_dict`.* Each column is pulled out once as a plain Python list and the rows are
   built with `zip`. Same dictionaries, same keys, same values, less overhead.
5. *Remember pure results.* `normalize_text` and the text part of `parse_int` always give the same
   answer for the same input, and the files repeat the same spellings over and over, so
   the answers are remembered (`functools.lru_cache`).

**Why the results cannot differ.** The fairness key always ends with the rep's unique file position,
so there is exactly one smallest key. That means the answer does not depend on the order in which
candidates are scanned, which is what lets us reorganise the lists safely.
`tests/test_performance.py` also keeps a small, slow reference router (plain lists, `list.remove`,
the strategy's own key function) and checks that the optimised router makes the same choice for 400
generated messy leads and 60 small-capacity reps, for every strategy and every matching minimum
(3, 2 and 1 of 3), with plenty of Manager Review overflow.

## How the work grows (Big-O in plain words)

Routing one lead costs roughly **pools tried x reps per pool**. A lead tries at most 7 pools
(a few more for an ambiguous region) and stops at the first pool with someone free; each pool
holds only ~10-30 of the 1,600 reps. So routing 50,000 leads is about
`leads x pools tried x reps per pool`, a small, roughly constant amount of work per lead, and it grows in a straight
line with the number of leads. Doubling the leads about doubles the time. It does not grow with the
total number of reps, only with how many sit in the pools a lead actually looks at. Cleaning is one
pass over the rows (the number of distinct spellings only matters for the first time each is seen).

## Tried and not kept

- **Per-pool heaps with lazy invalidation** (to find the fairest rep without scanning the pool). Not
  done: each rep sits in up to 7 pools and every assignment changes their key, so the heaps would
  need constant re-pushing and stale-entry skipping. The risk of a subtle ordering difference was
  not worth saving a fraction of the remaining ~0.5 s.
- **Caching `clean_name`.** Company names are almost all different, so a cache would only add memory.
- **review.py (Manager Review suggestions) and the value audit** were checked in the profile. They
  already work per distinct trait combination or distinct raw value, not per row, and are a small
  part of the total, so they were left alone.
- No threading or multiprocessing: routing is sequential by design (every decision depends on who
  is free after the previous one).

## Reproduce

```
python scripts/bench.py scale 5                 # median stage timings
python scripts/baseline.py save  <dir>          # freeze decisions (run on the old code)
python scripts/baseline.py check <dir>          # prove nothing changed
pytest -q --benchmark-disable                   # includes tests/test_performance.py
```
