# Lead Router

**2026 Ignite Design Challenge (Salesforce): Lead Prioritization.**
An engine that cleans messy lead and rep data, scores every lead, and routes it to the right
sales rep, or to a Manager Review queue when nobody can take it, plus a Salesforce
Lightning-style dashboard to present the results. **No lead is ever dropped.**

| Scale dataset: 50,000 leads × 1,600 reps | Result |
|---|---|
| Time, end to end | about 2.5 seconds (≈ 20,000 leads/second) |
| Assigned / Manager Review | 99.8% / 0.2% (sample: 99% / 1%) |
| Critical leads handled by a Senior | 98.6% (the rest by Managers or Manager Review) |
| Critical leads given to a Junior | 0 |
| Reps over their max capacity | 0 |

## Quick start

Requires **Python 3.11+**.

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -e ".[dashboard,dev]"

python -m app.main                       # dashboard -> http://127.0.0.1:8050
leadrouter run --dataset sample          # 100 leads, 10 reps
leadrouter run --dataset scale           # 50,000 leads, 1,600 reps
leadrouter run --dataset scale --out results.csv
leadrouter run --leads my_leads.csv --reps my_reps.csv
leadrouter compare --dataset scale       # distribution strategies side by side

pytest -q                                # all tests (~10 s)
```

From Python:

```python
from lead_router import run_dataset

result = run_dataset("scale")
print(result.metrics["assigned_rate"], result.timings["total"])
for a in result.manager_review[:5]:
    print(a.lead_id, a.overflow_reason)
```

## The dashboard

`python -m app.main`, then open <http://127.0.0.1:8050> and click **Run Scale (50k)**.

Styled after a Salesforce Lightning dashboard (the "Sales Team Dream Dashboard" from the
Salesforce Admins blog): a global header with search, the app nav bar, a record-style page header with
the run buttons, and white report components on the blue-gray dashboard canvas.

Five pages, each with its own address:

| Page | What's on it | The question it answers |
|---|---|---|
| **Dashboard** `/` | Leads Assigned · Waiting in Manager Review · Critical Leads to Seniors · Team Capacity Used, then Leads by Priority · Leads by Region · How Busy Each Team Is · How Full Are Our Reps? | A manager's at-a-glance check: did every lead get an owner, and is the team balanced? |
| **Leads & Reps** `/leads` | Lead Queue · Rep Capacity tabs | Drill down: search, filter, group, click a lead for its score breakdown |
| **Manager Review** `/review` | Leads waiting for a manager · the selected lead's details · its **3 closest reps with room** (traits ✓/✗, load) · **Assign** buttons and a pick-any-rep list | Who needs a decision, and who's the best person left to take it? |
| **Tests** `/tests` | Runs the real **pytest** suite live and groups the results: ties, rules that must never break, edge cases & unknown values, Manager Review, scale | Is it really working, or are we just guessing? |
| **About** `/about` | How it works (Clean → Score → Route → ML double-check) · Perfect Matches · Leads Routed per Second · **Unusual records found by machine learning** · Data Quality | How good and how fast is it, and what did we clean up? |

Every "View Report" link opens the page and tab with the details; the header search box filters
the lead queue on the Leads & Reps page. **Demo script (≈90 s):** Run Sample → read the four big
numbers → Run Scale → click a Critical lead in the queue → Manager Review: assign a lead → Tests: Run all tests → Reset.

A manager's assignment follows the same safety rules as the router (no Critical lead to a Junior, nobody over
capacity), updates every number on every page, and shows up in the exports as `assigned_by = Manager`.

## Folder layout

```
./
├── app/                   the dashboard
│   ├── main.py            layout + callbacks
│   ├── charts.py          every chart (Plotly), Lightning colors
│   ├── grid_server.py     server-side paging for the 50k-row table
│   ├── test_runner.py     runs pytest for the Tests page and explains each test
│   └── assets/            CSS, count-up animation, table cell renderers
├── config/rules.yaml      ← ALL business numbers: points, tiers, aliases, capacities, routing order
├── data/                  ← the challenge CSVs
├── lead_router/
│   ├── models.py          data shapes: Lead, Rep, Assignment, PipelineResult
│   ├── config.py          loads rules.yaml
│   ├── cleaning.py        STEP 1  messy rows → clean objects
│   ├── scoring.py         STEP 2  points → Critical / High / Medium / Low
│   ├── routing.py         STEP 3  leads → reps (the core algorithm)
│   ├── distribution.py    how ties between equally qualified reps are broken + fairness math
│   ├── explain.py         turns every decision and data fix into a plain-English sentence
│   ├── pipeline.py        STEP 4  runs everything, times it, computes KPIs
│   ├── review.py          Manager Review: closest reps for a waiting lead, assign by hand
│   ├── anomaly.py         machine learning: flags unusual leads/reps (Isolation Forest), never changes a route
│   ├── export.py          results → tables / CSV (incl. Salesforce Data Loader file)
│   └── cli.py             the `leadrouter` command
├── tests/                 pytest: unit, property-based (hypothesis), benchmark
├── docs/ARCHITECTURE.md   deeper walkthrough + diagram
└── docs/DISTRIBUTION.md   the fair round-robin method, worked example, measurements
```

Start reading at `pipeline.py` (the big picture), then `routing.py` (the interesting part).

## Machine learning: unusual-record detection

`lead_router/anomaly.py` uses scikit-learn's **Isolation Forest**, an unsupervised model (it needs no
"right answers" to learn from). It builds random trees that keep splitting the data; ordinary rows take
many splits to isolate, odd rows only a few. The rows isolated fastest are flagged, each with a
plain-English reason (the feature furthest from normal):

| Looks at | Example flag |
|---|---|
| Lead company size (log scale), number of blank fields, number of values that needed cleaning | "Company size of 10,000,000,000 employees is far above normal (typical: 629). Check it's not a typo." |
| Rep max capacity vs. others of the same seniority, number of values fixed | "Max capacity of 10 is much higher than a typical Manager (7)." |

On the 50,000-lead file it flags 291 leads (0.6%) and 40 reps in about 0.9 s, including all 73 companies
claiming 10 billion employees. **It never changes a score, priority or route**: it only adds a note
(About page, lead detail panel, Manager Review panel, and the `unusual_record` export column).
`test_ml_never_changes_a_route` runs the 50k file with the model on and off and checks every assignment is identical.

Why not a model that predicts which leads will close? The data has no won/lost outcomes, so it could only
learn our own scoring rules back. With Salesforce closed-deal history, that is the natural next step,
behind the same rules.

## The algorithm in one page

### Step 1: Cleaning

Every text value goes through the same checks until one works:

| Check | Example | Result |
|---|---|---|
| Already correct | `Europe` | Europe |
| Casing / spacing / punctuation | ` EUROPE `, `Asia.  Pacific` | Europe, Asia-Pacific |
| Alias (rules.yaml) | `EUR`, `EN`, `sr`, `MGR` | Europe, English, Senior, Manager |
| Two regions glued together | `ASIA   PACIFICEurope` | Asia-Pacific (Europe saved as backup) |
| Typo (≥85% similar) | `Asa-Pacific` | Asia-Pacific |
| Blank / `N/A` / `Unknown` / junk | | unknown → fallback later |

Numbers: `1,000` → 1000, `5k` → 5000, `500-1000` → 750, `N/A` → unknown.
VIP: `true / yes / 1 / Y` → True, anything else → False.
Duplicates: an exact copy is merged; the same ID with different data is kept and renamed `-2`.
Reps: max capacity `-20` → 20 (sign typo); missing → seniority default; everyone starts at 0 leads.

### Step 2: Scoring

```
VIP referral → 100 points, Critical (nothing else checked)

score = company size   Enterprise 1,000+ = 50 · Mid-Market 100-999 = 30 · SMB = 0 · unknown = 20
      + activity       Live Demo / Contact Sales = 35 · Webinar = 25 · Whitepaper = 15 · Pricing = 5
      + industry       Technology = 15 · Healthcare / Finance = 10 · Mfg / Retail = 0 · unknown = 5

Critical 85+ · High 60–84 · Medium 40–59 · Low 0–39
```

### Step 3: Routing

1. **Pools.** Reps are grouped by the traits they share: all three `(region, language, industry)`,
   plus each pair of two, plus each single trait.
2. **Urgent first.** Leads are routed Critical → Low, then by score, so Critical leads get first
   pick of capacity.
3. **Matching: most traits in common first.** Try reps who share **all 3** traits (region, language,
   industry). Only if none of them is free, use reps who share **2 of 3**: industry gives way first,
   then region, then language. As a last resort, reps who share **1 of 3**: same language first,
   then same region, then same industry (a rep who speaks the lead's language is the most useful
   partial match). An ambiguous region like `ASIA PACIFICEurope` also tries its second region.
   Every assignment records its match level, e.g. *"2 of 3: region + language"*.

   | Matching (`routing.min_traits_to_match`) | Sample: assigned | Scale: assigned |
   |---|---|---|
   | `3`: strict 3 of 3 | 19% | 94.0% |
   | `2`: 3 of 3, then 2 of 3 | 61% | 98.5% |
   | **`1`: 3 → 2 → 1 of 3 (default)** | **99%** | **99.8%** |

   On the scale run: 45,477 leads matched on 3 of 3, 3,660 on 2 of 3 and 776 on 1 of 3. A better
   match always wins when that rep is free. Set `min_traits_to_match: 3` for the brief's literal
   strict rule.
4. **Seniority.** Critical → Senior, then Manager, **never Junior**. Others → Junior or Senior, then
   Manager (Managers have small capacity, so they're saved for overflow).
5. **Fair distribution: Capacity-Weighted Fair Round-Robin.** Among the eligible reps, pick:
   - the smallest **virtual finish time** `(leads held + 1) ÷ max capacity` (Weighted Fair Queuing),
     so a 50-slot Junior gets 2.5× the leads of a 20-slot one and both fill at the same pace;
   - if tied, whoever has received the least **lead value** (sum of scores) per slot, so the good
     leads are shared, not just the count;
   - if still tied, whoever got a lead **longest ago** (round-robin rotation);
   - if still tied, file order (100% reproducible).

   Full explanation, worked example and measurements: **[docs/DISTRIBUTION.md](docs/DISTRIBUTION.md)**.
6. **Capacity.** Each assignment adds 1 to the rep's load; a full rep leaves the pools.
7. **Overflow.** No free rep → **Manager Review** with a reason: *Matching reps are all full*,
   *Urgent lead, only Juniors match*, *No rep shares a trait*, or *Not enough info to match*.
8. **Plain-English explanations** (`lead_router/explain.py`). Every decision gets a sentence anyone
   can read, e.g. *"Close match (2 of 3 traits): no rep matching all 3 was free. Sent to Lucas Ali, a
   Senior rep who covers Europe and speaks German, but specializes in Retail rather than Finance.
   Lucas now has 5 of 22 leads."* Every data fix is shown as *in the file → what we used*,
   e.g. `"5k"` → 5,000 employees.

**Why this method?** Measured with Jain's fairness index (1.0 = perfectly even) on the 50,000 leads,
`leadrouter compare --dataset scale`:

| Strategy | Workload fairness | After first 10% | Critical → Senior (live arrivals) |
|---|---|---|---|
| **Fair round-robin (ours)** | **0.992** | **0.991** | **95.7%** |
| Plain round-robin | 0.988 | 0.982 | 62.3% |
| First match | 0.620 | 0.418 | 89.0% |

Plain round-robin ignores capacity, so Seniors fill up early and Critical leads spill over;
first-match buries a few reps while the rest sit idle. Each pool holds only ~10–30 reps,
so checking them all per lead is fast.

## Changing the rules

Edit **`config/rules.yaml`**, not the code:

| I want to… | In rules.yaml |
|---|---|
| Change points or tier cutoffs | `scoring:` |
| Add a spelling (e.g. `Deutsch` → German) | `aliases:` |
| Let Managers take normal leads earlier | `routing.seniority_order` |
| Turn off a fallback | `routing.relax_industry_when_unknown`, `routing.try_alternate_region_when_ambiguous` |

Then run `pytest -q`.

## Tests

| File | What it proves |
|---|---|
| `test_business_scenarios.py` | **10 plain-language promises** (no lead lost, no Junior gets Critical, nobody overloaded, VIP first, messy spellings understood, fair sharing, 50k in seconds, missing data handled). Explained in [docs/TESTING.md](docs/TESTING.md) |
| `test_ties_and_edge_cases.py` | **who goes first on equal scores** (tier → score → file row; last open slot), how equal reps take turns, and edge cases: size formats, unknown VIP, a completely blank lead, "NA" = North America, glued regions, unreadable seniority, bad capacities, messy headers |
| `test_anomaly.py` | Machine learning: catches all 73 "10 billion employee" companies, gives a reason, same flags every run, and **never changes a route** (50k file run with the model on and off) |
| `test_review.py` | Manager Review: closest reps ranked by traits, no Juniors for Critical, full reps skipped, assigning updates every number, rules can't be broken by hand |
| `test_cleaning.py` | aliases, casing, typos, blanks, junk, number parsing, duplicates, rep capacity fixes |
| `test_scoring.py` | every tier boundary, every matrix row, VIP override, missing fields |
| `test_routing.py` | Junior never gets Critical, Senior before Manager, capacity never exceeded, even split, overflow reasons, nothing dropped |
| `test_distribution.py` | proportional to capacity, interleaved not bursty, value tie-break, rotation, reproducible, every strategy obeys the rules |
| `test_pipeline.py` | empty file, one lead, zero reps, reset, clear error messages |
| `test_properties.py` | 150 batches of random garbage never crash the pipeline or break the rules |
| `test_benchmark.py` | 50,000 leads finish under 10 s |

All 216 test cases (127 tests) can be run from the dashboard's **Tests** page, which shows what each one proves.

## Pushing this folder to GitHub

`backend/` is a complete project on its own, with its own `.gitignore` and CI workflow:

```bash
cd backend
git init -b main
git add .
git commit -m "Lead Router backend"
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

GitHub Actions (`.github/workflows/tests.yml`) then runs the tests on every push.
If the challenge data must stay private, uncomment `data/*.csv` in `.gitignore`.
