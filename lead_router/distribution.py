"""
distribution.py - HOW we choose between reps who are equally qualified for a lead.

By the time this code runs, routing.py has already narrowed the field to reps who match the
lead's region, language and industry, have the right seniority, and have free capacity.
Often several reps qualify. The brief says: don't just take the first one. This file decides.

OUR METHOD: "CAPACITY-WEIGHTED FAIR ROUND-ROBIN"  (strategy name: fair_share)
======================================================================================
It's the classic Weighted Fair Queuing scheduler (used in network routers to share a link
between flows of different sizes) applied to reps instead of network flows.

    For every eligible rep, compute their "virtual finish time":

            finish = (leads they hold + 1) / their max capacity

    i.e. "how full would this rep be if they took this lead?"  The rep with the SMALLEST
    finish time gets the lead.

    Tie-breakers, in order (ties are very common: everyone starts at 0):
      1. VALUE fairness: the rep who has received the least lead value (sum of lead scores)
         per slot goes first. Count fairness alone could hand one rep all the 95-point leads
         and their teammate all the 10-point ones; this spreads the good leads too.
      2. ROUND-ROBIN: the rep who was served LONGEST AGO goes first (rotation).
      3. FILE ORDER: a fixed last resort, so every run is 100% reproducible.

Why this is better than the obvious options:
    first_match   Always the first rep listed. Rep #1 drowns, everyone else idles. (Forbidden.)
    round_robin   Takes turns, but ignores capacity: a Senior with 20 slots and a Junior with
                  50 get the same number of leads, so the Senior is full at 40% team usage.
    least_loaded  Picks the lowest current load %. Fair in the long run, but with no rotation it
                  hands consecutive leads to the same rep whenever loads tie (bursty).
    fair_share    Shares leads in proportion to capacity (a 50-slot rep gets ~2.5x the leads of a
                  20-slot rep) AND interleaves them smoothly, AND rotates on ties. Every rep in a
                  pool reaches the same % full at the same time.

Measuring fairness: Jain's fairness index (Jain, Chiu & Hawe, 1984)
    J = (sum of x)^2 / (n * sum of x^2)       x = each rep's % of capacity used
    J = 1.0 means perfectly even; J = 1/n means one rep has everything.
    We compute it inside every group of interchangeable reps (same region, language,
    industry and seniority) and average, weighted by group size.

Every strategy is a "sort key": a tuple where SMALLER = gets the next lead.
Run `leadrouter compare --dataset scale` to see all four side by side on the real data.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable

from .models import Rep

# key(rep, value_received, last_served, file_position) -> tuple; the smallest tuple wins.
#   value_received = sum of the scores of the leads this rep already holds
SortKey = Callable[[Rep, float, int, int], tuple]

STRATEGIES: dict[str, SortKey] = {
    # Our method: weighted fair queuing (proportional to capacity), then value fairness,
    # then round-robin rotation, then file order.
    "fair_share": lambda rep, value, last, pos: (
        (rep.current_capacity + 1) / rep.max_capacity,   # virtual finish time
        value / rep.max_capacity,                        # lead value already received per slot
        last,                                            # served longest ago goes first
        pos,                                             # reproducible last resort
    ),
    # Baselines, kept so we can prove fair_share is better (see `leadrouter compare`).
    "least_loaded": lambda rep, value, last, pos: (rep.load_ratio, pos),
    "round_robin": lambda rep, value, last, pos: (last, pos),
    "first_match": lambda rep, value, last, pos: (pos,),
}

DEFAULT_STRATEGY = "fair_share"

DESCRIPTIONS = {
    "fair_share": "Capacity-weighted fair round-robin (ours)",
    "least_loaded": "Lowest % full, no rotation",
    "round_robin": "Take turns, ignore capacity",
    "first_match": "First rep in the list",
}


def get_strategy(name: str | None) -> SortKey:
    name = name or DEFAULT_STRATEGY
    if name not in STRATEGIES:
        raise ValueError(f"Unknown distribution strategy '{name}'. Choose one of: {', '.join(STRATEGIES)}")
    return STRATEGIES[name]


# ======================================================================================
# Fairness measurement
# ======================================================================================
def jain_index(values: Iterable[float]) -> float:
    """Jain's fairness index: 1.0 = perfectly equal, 1/n = one person has everything."""
    values = list(values)
    total = sum(values)
    squares = sum(v * v for v in values)
    if not values or squares == 0:
        return 1.0  # nobody has anything: trivially equal
    return (total * total) / (len(values) * squares)


def distribution_fairness(reps: list[Rep], value_by_rep: dict[str, float] | None = None) -> float:
    """Average Jain index across groups of interchangeable reps, weighted by group size.

    Without `value_by_rep`: fairness of WORKLOAD (% of capacity used).
    With `value_by_rep` ({rep id: total score of leads received}): fairness of OPPORTUNITY
    (lead value received per slot).
    Only groups that actually received leads and have 2+ reps count (a group of one is
    always "fair", and a group nobody needed tells us nothing).
    """
    groups: dict[tuple, list[float]] = defaultdict(list)
    for rep in reps:
        if rep.region and rep.language and rep.max_capacity > 0:
            key = (rep.region, rep.language, rep.industry, rep.seniority)
            amount = rep.current_capacity if value_by_rep is None else value_by_rep.get(rep.id, 0.0)
            groups[key].append(amount / rep.max_capacity)

    weighted_sum, weight = 0.0, 0
    for utilizations in groups.values():
        if len(utilizations) >= 2 and sum(utilizations) > 0:
            weighted_sum += jain_index(utilizations) * len(utilizations)
            weight += len(utilizations)
    return round(weighted_sum / weight, 4) if weight else 1.0
