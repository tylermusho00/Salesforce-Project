# How we know Lead Router works

Lead Router has **216 automated test cases** (127 tests) written with **pytest**, the standard testing tool for Python.
Most of them check small technical details. This page covers the **business scenario tests**
(`tests/test_business_scenarios.py`): each one checks a promise a sales manager would care about,
in words anyone can follow.

**See them on the dashboard:** open the **Tests** tab (or click **✓ Run Tests** in the header) and press
**▶ Run all tests**. It runs the real pytest suite in the background (about 25 seconds) and groups the
results by what they prove: ties, rules that must never break, edge cases and unknown values, Manager
Review, and scale. Below that, these business scenarios appear as cards with a small chart and three short
blurbs: why this test, the result, and why it shows the program works. The checks live in
`lead_router/scenarios.py`, so the website and pytest always run the same code.

Each scenario test runs the **whole program**, the same way the dashboard does: read the CSV, clean
it, score it, route it. It then checks the outcome. Four of them use the real 50,000-lead /
1,600-rep challenge file; the others use a tiny, hand-written situation so the expected answer is
obvious.

**Run them yourself** (from the `backend` folder):

```bash
pytest tests/test_business_scenarios.py -v -s
```

`-s` prints each test's `RESULT:` line, which is where the numbers below come from.
**Last run: 11 passed** (the 10 below plus one that runs all of them together, exactly as the dashboard button does).

---

## Summary

| # | The promise | Result | Passed |
|---|---|---|---|
| 1 | No lead is ever lost | 50,000 in → 49,913 assigned + 87 in Manager Review. Missing: 0 | ✅ |
| 2 | Urgent leads never go to beginners | 6,967 Critical leads: 6,872 to Seniors, 95 to Managers, **0 to Juniors** | ✅ |
| 2b | …even when a Junior is the only match | The lead waits for a manager instead | ✅ |
| 3 | Nobody gets overloaded | 1,600 reps, **0 over their limit** | ✅ |
| 3b | 10 leads for 3 open slots | 3 assigned, 7 to Manager Review, rep holds exactly 3 | ✅ |
| 4 | VIP referrals always jump the line | A 5-person shop with no activity: VIP → Critical (100 pts); without VIP → Low (0 pts) | ✅ |
| 5 | Messy spellings still reach the right rep | “Nort America / EN / Tech / 5k” → North America / English / Technology / 5,000 → the right rep | ✅ |
| 6 | Equal reps get an equal share | 30 leads, 3 equal reps → 10 / 10 / 10 (first-on-the-list would give 30 / 0 / 0) | ✅ |
| 7 | Big uploads don’t freeze it | 50,000 leads in **3.3 seconds** (target: under 10) | ✅ |
| 8 | Missing information never crashes it or drops a lead | Partial lead still routed; an all-junk lead waits with a clear reason | ✅ |

---

## 1. No lead is ever lost

**Why this test:** the worst thing a routing system can do is silently lose a lead: that’s a
customer nobody ever calls. The brief says overflow must go to Manager Review, not be dropped.

**What it does:** runs the full 50,000-lead file and counts every outcome. Every row from the file
must end up exactly once, either with a rep or in Manager Review. (The only rows allowed to merge are
exact copies of the same row.)

**Result:** `50,000 rows in -> 49,913 assigned + 87 in Manager Review = 50,000. Missing: 0.`

**Why this shows it works:** the totals match exactly, and every lead ID appears exactly once. A
lead going missing, or being assigned twice, would make this test fail.

## 2. Urgent leads never go to beginners

**Why this test:** the cheat sheet’s hard rule: Critical leads go to a **Senior** first, then a
**Manager**, and **never to a Junior**. Breaking it would put the most valuable deals in the least
experienced hands.

**What it does:** (2) on the full file, looks at who received every Critical lead; (2b) builds a tiny
case where the *only* matching rep is a Junior and checks that the urgent lead is held back.

**Result:**
- `6,967 Critical leads -> Senior 6,872, Manager 95, Manager Review 0, Junior 0.`
- `Critical lead + only a Junior available -> Manager Review: "Urgent lead, but only Junior reps match (Juniors can't take urgent leads). Needs a manager."`

**Why this shows it works:** zero Juniors across nearly 7,000 urgent leads, 98.6% to Seniors, and
Managers used only as the backup. The tiny case proves the rule holds even when breaking it would be
the “easy” option.

## 3. Nobody gets overloaded

**Why this test:** every rep has a maximum number of leads. Going over it means leads sit untouched
on an overloaded desk, which is worse than a manager seeing them in review.

**What it does:** (3) checks all 1,600 reps after the full run; (3b) gives one rep room for 3 leads and
sends 10.

**Result:**
- `1,600 reps checked, 0 over their limit. Fullest rep: 23 of 23.`
- `10 leads, 3 slots -> 3 assigned, 7 in Manager Review, rep holds 3 of 3.`

**Why this shows it works:** reps are filled *right up to* their limit (23 of 23) but never past
it, and the leads that don’t fit go to Manager Review instead of piling on.

## 4. VIP referrals always jump the line

**Why this test:** the cheat sheet says a VIP referral is **Critical no matter what**. A referral from
a key partner shouldn’t wait just because the company is small.

**What it does:** two identical leads (a 5-person retail shop with no recent activity), one marked
VIP and one not.

**Result:** `5-person shop, no activity: VIP -> Critical (100 pts), same shop without VIP -> Low (0 pts).`
The VIP lead is also routed first.

**Why this shows it works:** the only difference between the two leads is the VIP flag, and it
alone moves the lead from the bottom tier to the top.

## 5. Messy spellings still reach the right rep

**Why this test:** the challenge data is full of typos and shorthand. If the program only
understood perfect spelling, most leads would never match a rep.

**What it does:** one lead written as `ACME ROBOTICS, Nort America, EN, Tech, 5k, "requested live demo "`
and two reps: one in Europe, one in North America. Both reps speak English and cover Technology.

**Result:** `"Nort America / EN / Tech / 5k" read as North America / English / Technology / 5,000 employees -> 100 pts (Critical) -> Right Rep, perfect match.`

**Why this shows it works:** every messy value was understood correctly, the lead was scored
correctly (big company + demo request + tech = 100), and it reached the rep in the right region,
not just any rep.

## 6. Equal reps get an equal share

**Why this test:** the brief says not to give every lead to the first matching rep. Unfair
distribution burns out one rep while others sit idle.

**What it does:** 30 leads, 3 reps with identical skills and capacity. Runs our method and, for
comparison, the naive “first on the list” method.

**Result:** `ours: Ana 10, Ben 10, Cal 10; first-on-the-list: Ana 30.`

**Why this shows it works:** the split is perfectly even, and the side-by-side shows the problem we
avoid. (`docs/DISTRIBUTION.md` covers reps with *different* capacities.)

## 7. Big uploads don’t freeze it

**Why this test:** the brief’s scale challenge: a sudden upload of 50,000 leads must be handled
quickly, without freezing.

**What it does:** times the whole program on the real 50,000-lead file.

**Result:** `50,000 leads cleaned, scored and routed in 3.3 s (15,312 leads per second). Target: under 10 s.`

**Why this shows it works:** a manager gets results in a few seconds, about 3× faster than our
target. (Exact time varies a little by computer.)

## 8. Missing information never crashes it or drops a lead

**Why this test:** real files have blanks, “N/A” and junk. A program that crashes on one bad row
would stop the whole team’s work.

**What it does:** two leads: one with only a region and language (everything else blank), and one
where every field is junk (`N/A`, `???`, `Unknown`, `lots`, `maybe`).

**Result:**
- Lead with blanks → assigned to Kai: *“Close match (2 of 3): same region and language; different industry (rep: Finance, lead: unknown).”*
- Lead with only junk → Manager Review: *“Region, language and industry are all unknown, so there’s nothing to match on.”*

**Why this shows it works:** nothing crashed, neither lead was dropped, partial information was
still enough to route, and the hopeless case went to a manager with a reason a person can act on.

---

## Ties: who goes first when scores are equal

`tests/test_ties_and_edge_cases.py`. Leads are ranked by **tier, then score, then their row in the file**.
Nothing is random, so the same file always gives the same answer.

| Test | What it proves |
|---|---|
| Same score → earlier row first | Three High leads with 70 points come out as row 2, 5, 9 |
| Higher score beats file order | Both High: 80 points goes before 65, even though the 65 came first |
| Tier always comes first | A Critical lead at row 99 beats a High lead at row 0 |
| VIP vs. a perfect 100 | Both 100 and Critical, so file order decides: no hidden preference |
| Last open slot → higher score | Two Critical leads, one Senior slot: 95 points gets it, 90 waits |
| Exact tie for the last slot | Same tier and score: the earlier row gets the rep |
| Identical reps take turns | A, B, A, B, starting with the rep listed first |
| Equal match → emptier rep | The rep with more room gets the next lead |
| Fairness never beats a better match | A busier 3-of-3 rep beats an idle 2-of-3 rep |

## Edge cases and unknown values

Same file. The rule: never crash, never drop a lead, always explain.

| Test | What it proves |
|---|---|
| Company size formats | `1,000`, `5k`, `500-1000` (→ 750), ` 250 ` are read; blank, N/A, text, 0 and negatives become unknown |
| Only a clear yes is VIP | True/yes/Y/1 are VIP; Unknown, blank and "maybe" are not |
| A completely blank lead | Kept, scored 25 (Low) from fallback points, sent to review: "Not enough info to match" |
| "NA" = North America | pandas would erase it as missing; we keep it |
| Two regions in one cell | First kept, second saved as a backup, and the backup is used if the first has no rep |
| Unreadable rep level | Treated as Junior, so that rep can never receive a Critical lead |
| Bad rep capacities | −20 → 20, blank → the level's default, a starting load → reset to 0 |
| Messy column headers | ` lead id `, `COMPANY NAME`… still work |
| A lead no rep covers | Waits in Manager Review with "No rep shares a trait" |

## Manager Review: assigning by hand

`tests/test_review.py`. The Manager Review page suggests the 3 closest reps with room and lets a manager assign.

| Test | What it proves |
|---|---|
| Most traits first | 3-of-3 before 2-of-3, equal reps in file order, full reps never suggested |
| No Juniors for Critical | Juniors are never suggested for, or assignable to, a Critical lead |
| Assigning updates everything | The lead leaves review, the rep's load goes up, every number recounts, the export says "Manager" |
| No overfilling | A full rep can't be chosen |
| Only waiting leads | A lead already with a rep can't be reassigned from here |

## Machine learning: unusual records

`tests/test_anomaly.py`. The Isolation Forest model only adds notes; these tests prove it.

| Test | What it proves |
|---|---|
| Never changes a route | The 50k file is run with the model on and off: all 50,000 leads get the same rep, score and priority |
| Catches impossible sizes | All 73 leads claiming 10,000,000,000 employees are flagged; under 1% of leads are flagged overall |
| Gives a reason | Each flag says what stands out, e.g. "Company size of 10,000,000,000 employees is far above normal" |
| Odd rep capacities | A Senior with 400 slots among Seniors with about 20 is flagged |
| Repeatable | Same data → same flags (fixed seed) |
| Too little data | Under 20 rows: it doesn't guess |

## The other tests

| File | What it checks |
|---|---|
| `test_cleaning.py` | Every alias and typo, blank and junk values, number formats, duplicates, rep capacity fixes |
| `test_scoring.py` | Every tier boundary (84 vs. 85 points…), every row of the cheat sheet, the VIP override |
| `test_routing.py` | Seniority rules, capacity limits, 3 → 2 → 1 trait matching, overflow reasons |
| `test_distribution.py` | Fair sharing in proportion to capacity, rotation, repeatable results |
| `test_explain.py` | The plain-English explanations say the right thing |
| `test_pipeline.py` | Empty files, wrong columns, zero reps, Reset |
| `test_properties.py` | 150 batches of random garbage: never crashes, never breaks a rule |
| `test_benchmark.py` | Times the 50,000-lead run |

All of them run automatically on GitHub on every push.
