"""
anomaly.py - machine learning: flag unusual leads and reps for a human to double-check.

Model: scikit-learn's IsolationForest (unsupervised, so it needs no "right answers" to learn from).
It builds many random decision trees that keep splitting the data at random. A normal row hides among
thousands of similar rows and takes many splits to isolate; an odd row (a company with 10 billion
employees, a lead where almost every field was blank) gets separated in just a few splits.
Rows that are isolated fastest are flagged.

What it does NOT do: change any score, priority, route or capacity. The business rules stay exactly as
they are (Critical never to Juniors, capacity limits, Manager Review). The model only adds a note,
"Unusual record: ...", that a manager sees on the About page, in the lead's detail panel and in the export.

Why this, and not a model that predicts which leads will close? The data has no outcome column
(won / lost), so a predictive model could only learn our own scoring rules back. Spotting suspicious
records is useful today and needs no labels.

Features the model looks at (all numbers, built from the cleaned data):
    leads   company size (log scale), how many fields were blank, how many values needed cleaning
    reps    max capacity compared with others of the same seniority, how many values needed fixing
Each flag gets a reason: the feature that is furthest from normal, in plain words.
"""
from __future__ import annotations

import math
from statistics import median

import numpy as np
from sklearn.ensemble import IsolationForest

from .explain import lead_fixes, rep_fixes
from .models import Lead, Rep

MIN_ROWS = 20                # too few rows to learn what "normal" looks like
LEAD_SHARE, REP_SHARE = 0.005, 0.02   # flag about 0.5% of leads and 2% of reps (the most unusual ones)
SEED = 0                     # fixed, so the same file always gets the same flags
SAMPLE = 2048                # rows each tree sees. Bigger than the default 256 so a repeated bad value
                             # (73 companies "with 10 billion employees") is still seen as unusual.
STRONG = 2.0                 # keep a flag only if some feature is this far from normal in a BAD direction


def _robust_z(column: np.ndarray) -> np.ndarray:
    """How far each value is from the middle, in units of the typical spread (median / IQR based,
    so a few extreme values don't hide themselves)."""
    q1, mid, q3 = np.percentile(column, [25, 50, 75])
    spread = (q3 - q1) / 1.35 or np.std(column) or 1.0
    return (column - mid) / spread


def _flag(features: np.ndarray, share: float) -> tuple[np.ndarray, np.ndarray]:
    """Indexes of the `share` most unusual rows, and every row's unusualness (higher = stranger).
    The rows are scored once and the cut-off is taken directly (faster than fit_predict + score_samples)."""
    model = IsolationForest(n_estimators=100, max_samples=min(SAMPLE, len(features)), random_state=SEED)
    unusual = -model.fit(features).score_samples(features)
    cutoff = np.quantile(unusual, 1 - share)
    return np.flatnonzero(unusual >= cutoff), unusual


def flag_leads(leads: list[Lead]) -> int:
    """Set lead.anomaly to a plain-English reason for the most unusual leads. Returns how many."""
    for lead in leads:
        lead.anomaly = None
    if len(leads) < MIN_ROWS:
        return 0
    known_sizes = [l.company_size for l in leads if l.company_size]
    typical_size = median(known_sizes) if known_sizes else 100
    sizes = np.array([math.log10(l.company_size or typical_size) for l in leads])
    blanks = np.array([len(l.imputed_fields) for l in leads], dtype=float)
    cleaned = np.array([len(lead_fixes(l)) for l in leads], dtype=float)
    features = np.column_stack([sizes, blanks, cleaned])
    flagged, scores = _flag(features, max(LEAD_SHARE, 1 / len(leads)))
    z = np.column_stack([_robust_z(c) for c in features.T])
    # Only "bad" directions count: any odd size, but MORE blanks / MORE cleaning (a clean row isn't suspicious).
    badness = np.column_stack([np.abs(z[:, 0]), z[:, 1], z[:, 2]])

    count = 0
    for i in flagged:
        lead = leads[i]
        strongest = int(np.argmax(badness[i]))
        if badness[i, strongest] < STRONG:
            continue
        count += 1
        if strongest == 0 and lead.company_size:
            direction = "far above" if z[i, 0] > 0 else "far below"
            reason = (f"Company size of {lead.company_size:,} employees is {direction} normal "
                      f"(typical: {typical_size:,.0f}). Check it's not a typo.")
        elif strongest == 1 or (strongest == 0 and not lead.company_size):
            fields = ", ".join(f.replace("_", " ") for f in lead.imputed_fields)
            reason = f"{len(lead.imputed_fields)} fields were blank ({fields}), far more than most leads."
        else:
            reason = f"{int(cleaned[i])} values needed cleaning, far more than most leads. The source may be unreliable."
        lead.anomaly = reason
        lead.anomaly_score = round(float(scores[i]), 3)
    return count


def flag_reps(reps: list[Rep]) -> int:
    """Set rep.anomaly for the most unusual reps (capacity far from their level, many fixes)."""
    for rep in reps:
        rep.anomaly = None
    if len(reps) < MIN_ROWS:
        return 0
    by_level: dict[str, list[int]] = {}
    for rep in reps:
        by_level.setdefault(rep.seniority.value, []).append(rep.max_capacity)
    typical = {level: median(values) or 1 for level, values in by_level.items()}
    ratio = np.array([math.log2((rep.max_capacity + 1) / (typical[rep.seniority.value] + 1)) for rep in reps])
    fixes = np.array([len(rep_fixes(rep)) for rep in reps], dtype=float)
    features = np.column_stack([ratio, fixes])
    flagged, scores = _flag(features, max(REP_SHARE, 1 / len(reps)))
    z = np.column_stack([_robust_z(c) for c in features.T])
    badness = np.column_stack([np.abs(z[:, 0]), z[:, 1]])   # any odd capacity, but only MORE fixes

    count = 0
    for i in flagged:
        rep = reps[i]
        level, usual = rep.seniority.value, typical[rep.seniority.value]
        strongest = int(np.argmax(badness[i]))
        if badness[i, strongest] < STRONG:
            continue
        count += 1
        if strongest == 0:
            size = "much higher" if ratio[i] > 0 else "much lower"
            reason = (f"Max capacity of {rep.max_capacity} is {size} than a typical {level} "
                      f"({usual:.0f}). Check it's right.")
        else:
            reason = f"{int(fixes[i])} values in this rep's row needed fixing: " + "; ".join(rep_fixes(rep))
        rep.anomaly = reason
        rep.anomaly_score = round(float(scores[i]), 3)
    return count
