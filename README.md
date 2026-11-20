# Lead Router: backend

**2026 Ignite Design Challenge (Salesforce): Lead Prioritization.**
The engine that cleans messy lead and rep data, scores every lead, and routes it to the right
sales rep, or to a Manager Review queue when nobody can take it. **No lead is ever dropped.**

| Scale dataset: 50,000 leads × 1,600 reps | Result |
|---|---|
| Time, end to end | about 2.5 seconds (≈ 20,000 leads/second) |
| Assigned / Manager Review | 94% / 6% |
| Critical leads handled by a Senior | 98.6% (the rest by Managers or Manager Review) |
| Critical leads given to a Junior | 0 |
| Reps over their max capacity | 0 |

## Quick start

Requires **Python 3.11+**.

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -e ".[dev]"

leadrouter run --dataset sample          # 100 leads, 10 reps
leadrouter run --dataset scale           # 50,000 leads, 1,600 reps
leadrouter run --dataset scale --out results.csv
leadrouter run --leads my_leads.csv --reps my_reps.csv

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

## Folder layout

```
backend/
├── config/rules.yaml      ← ALL business numbers: points, tiers, aliases, capacities, routing order
├── data/                  ← the challenge CSVs
├── lead_router/
│   ├── models.py          data shapes: Lead, Rep, Assignment, PipelineResult
│   ├── config.py          loads rules.yaml
│   ├── cleaning.py        STEP 1  messy rows → clean objects
│   ├── scoring.py         STEP 2  points → Critical / High / Medium / Low
│   ├── routing.py         STEP 3  leads → reps (the core algorithm)
│   ├── pipeline.py        STEP 4  runs everything, times it, computes KPIs
│   ├── export.py          results → tables / CSV (incl. Salesforce Data Loader file)
│   └── cli.py             the `leadrouter` command
├── tests/                 pytest: unit, property-based (hypothesis), benchmark
└── docs/ARCHITECTURE.md   deeper walkthrough + diagram
```

Start reading at `pipeline.py` (the big picture), then `routing.py` (the interesting part).

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

1. **Pools.** Reps are grouped by `(region, language, industry)`.
2. **Urgent first.** Leads are routed Critical → Low, then by score, so Critical leads get first
   pick of capacity.
3. **Matching.** A lead only uses its exact pool. Fallbacks only apply when the lead's *own* data is
   uncertain: unknown industry → any industry in the same region + language; ambiguous region →
   its second region. Language is never relaxed.
4. **Seniority.** Critical → Senior, then Manager, **never Junior**. Others → Junior or Senior, then
   Manager (Managers have small capacity, so they're saved for overflow).
5. **Fair distribution: least-loaded first, round-robin on ties.** Among the eligible reps, pick:
   - the lowest **load ratio** (current ÷ max), so a 50-slot Junior and a 22-slot Senior fill evenly;
   - if tied, whoever got a lead **longest ago** (rotation);
   - if still tied, file order.
6. **Capacity.** Each assignment adds 1 to the rep's load; a full rep leaves the pools.
7. **Overflow.** No free rep → **Manager Review** with a reason: *No matching rep*,
   *All eligible reps at capacity*, *No Senior/Manager for Critical lead*, or
   *Missing region or language*.

**Why least-loaded + round-robin?** Pure round-robin ignores that reps have different capacities;
pure least-loaded keeps picking the same rep when loads tie. Combining them spreads work in
proportion to each rep's capacity and still rotates fairly. Each pool holds only ~10–30 reps,
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
| `test_cleaning.py` | aliases, casing, typos, blanks, junk, number parsing, duplicates, rep capacity fixes |
| `test_scoring.py` | every tier boundary, every matrix row, VIP override, missing fields |
| `test_routing.py` | Junior never gets Critical, Senior before Manager, capacity never exceeded, even split, overflow reasons, nothing dropped |
| `test_pipeline.py` | empty file, one lead, zero reps, reset, clear error messages |
| `test_properties.py` | 150 batches of random garbage never crash the pipeline or break the rules |
| `test_benchmark.py` | 50,000 leads finish under 10 s |

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
