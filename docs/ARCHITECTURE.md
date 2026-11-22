# Architecture and walkthrough

## System blueprint

```mermaid
flowchart LR
    L[leads.csv] --> RC[read_csv<br/>check file + columns]
    R[reps.csv] --> RC
    Y[(config/rules.yaml)] -.-> CL & SC & RO
    RC --> CL[cleaning.py<br/>clean_leads / clean_reps]
    CL --> SC[scoring.py<br/>score_all]
    SC --> RO[routing.py<br/>route_all]
    RO --> M[pipeline.py<br/>compute_metrics]
    M --> OUT[PipelineResult]
    OUT --> CLI[cli.py]
    OUT --> EX[export.py<br/>CSV / Data Loader]
    OUT --> UI[dashboard<br/>(separate app)]
```

## Who calls what

```
pipeline.run_pipeline(leads_csv, reps_csv)
├── read_csv()                         ×2   errors → PipelineError
└── run_pipeline_frames()
    ├── cleaning.clean_leads(df)
    │   ├── clean_lead_row()                one row → Lead
    │   │   ├── Canonicalizer.resolve()     region, language, industry, activity
    │   │   ├── parse_company_size()
    │   │   └── parse_vip()
    │   └── DuplicateChecker.should_keep()
    ├── cleaning.clean_reps(df)             → Rep (capacity fixes)
    ├── scoring.score_all(leads)
    │   └── score_lead()                    → points, tier, reasons
    ├── routing.route_all(leads, reps)
    │   ├── RepPools(reps)                  build pools once
    │   └── route_lead()                    per lead, Critical first
    │       ├── pools_to_try()              which pools, best match first
    │       ├── seniority_groups()          which seniorities, in order
    │       ├── RepPools.pick_rep()         fairest free rep
    │       └── RepPools.assign()           +1 load, remove if full
    └── compute_metrics()
```

## A worked routing example

Reps in the pool `("Europe", "German", "Finance")`:

| Rep | Seniority | Max |
|---|---|---|
| A | Senior | 2 |
| B | Senior | 2 |
| C | Junior | 4 |
| M | Manager | 1 |

Leads arrive (already sorted Critical first): **4 Critical**, then **4 Medium**.

| # | Lead | Allowed (in order) | Free candidates → ratio | Picked | Why |
|---|---|---|---|---|---|
| 1 | Critical | [Senior] → [Manager] | A 0/2, B 0/2 | **A** | tie on ratio; tie on last-served; A is first in file |
| 2 | Critical | [Senior] → [Manager] | A 1/2, B 0/2 | **B** | B less loaded |
| 3 | Critical | [Senior] → [Manager] | A 1/2, B 1/2 | **A** | tie; A was served longer ago (round-robin). A now full |
| 4 | Critical | [Senior] → [Manager] | B 1/2 | **B** | B now full |
| 5 | Medium | [Junior, Senior] → [Manager] | C 0/4 | **C** | Seniors are full |
| 6 | Medium | [Junior, Senior] → [Manager] | C 1/4 | **C** | |
| 7 | Medium | [Junior, Senior] → [Manager] | C 2/4 | **C** | |
| 8 | Medium | [Junior, Senior] → [Manager] | C 3/4 | **C** | C now full |

A 5th Critical lead would go to **M** (Manager is the backup for Critical). A 9th Medium lead would
also try M last. Once M is full, the next lead goes to **Manager Review: "All eligible reps at
capacity"**. C is a Junior, so it is never offered a Critical lead, even when it has free slots.

## Design decisions (and the alternatives we rejected)

| Decision | Why | Rejected alternative |
|---|---|---|
| Least-loaded by **ratio**, round-robin on ties | Fair to reps with different capacities; still rotates | Pure round-robin (overloads small-capacity reps); first match (the brief forbids it) |
| Route **Critical first** | Urgent leads get scarce Senior capacity | File order (a Low lead could take the last Senior slot) |
| Managers **last** for non-critical leads | Managers have 5–10 slots; keep them for Critical overflow | Treat Managers like everyone else |
| Most traits in common first: **3 of 3 → 2 of 3 → 1 of 3** | Strict 3-of-3 left 81% of the sample in Manager Review; the closest available rep beats no rep. A better match always wins when that rep is free, and every assignment is labelled with its match level so managers can see weak matches | Strict only (`min_traits_to_match: 3`, still available as a switch) |
| Language kept in the **first two** match levels (3 of 3, then region + language or language + industry); region + industry with a different language is the last 2-of-3 option, and Best-available mode can fall back to a single shared trait | A rep can't easily work a lead they can't talk to, so language is relaxed only when no same-language rep sharing a second trait is free (a free region + industry rep who speaks another language is still preferred over a same-language rep who shares nothing else). Every such assignment is labelled with its match level and the language mismatch, and the Match quality view counts them | Never relax language (more leads left in Manager Review); treat language like any other trait |
| **Match quality before seniority** | Within each match level the preferred seniority goes first, but a better match always beats a preferred seniority: an exact-match Manager is used before a weaker-match Senior | Seniority first (more weak matches for non-critical leads, fewer Manager slots used) |
| Unknown seniority → **Junior** | Safest: Juniors can never take Critical leads | Guess Senior |
| Negative capacity → **absolute value** | -20, -45 match the dictionary's ranges exactly: a sign typo | Treat as 0 (wastes a real rep) |
| Plain lists, not a database | The brief asks for in-memory logic; ~2 s for 50k is plenty | SQL / pandas groupby routing (harder to explain) |

## Performance

| Stage | 50k leads | Notes |
|---|---|---|
| load | ~0.03 s | pandas reads everything as text |
| clean | ~0.45 s | each distinct spelling is resolved once and cached |
| score | ~0.2 s | |
| route | ~0.5 s | exact pools are small; relaxed pools can hold hundreds of reps (worst case O(leads x reps)) |
| **total** | **~1.3 s** | historical median of 5 runs (later runs under browser/host load were slower); before/after and method in [PERFORMANCE.md](PERFORMANCE.md) |

Run `pytest tests/test_benchmark.py --benchmark-only` to measure on your machine.

## Combined-version validation (2026-11-21)

The combined suite has **358 passing tests**, including the business scenarios and dashboard
integration regressions. Routing decisions, cleaned data, reports and metrics match the pre-merge
version for sample and scale; explanation wording is intentionally shorter. See
[INTEGRATION_VALIDATION.md](INTEGRATION_VALIDATION.md). The earlier ~1.3 s timing above is historical;
the merged version measured 4.48 s in this validation session, with machine load affecting timings.
