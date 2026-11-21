"""The speed-ups must not change a single decision.

The optimised router (lead_router/routing.py) is compared with a deliberately simple, slow
reference written here: for every lead it scans plain lists, builds the fairness key from the
strategy function, and removes full reps with `list.remove`, exactly like the version before
the speed-up. Same leads, same reps, same rules -> same rep, match level and reason.
"""
from __future__ import annotations

import copy
import random

import pandas as pd
import pytest

from conftest import make_lead, make_rep
from lead_router.cleaning import rows_as_dicts
from lead_router.distribution import STRATEGIES
from lead_router.models import AssignmentStatus, Priority, Seniority
from lead_router.routing import (matching_settings, pools_for_rep, pools_to_try, route_all,
                                 seniority_groups)
from lead_router.scoring import priority_order

REGIONS = ["North America", "Europe", "Asia-Pacific", "Latin America", None]
LANGUAGES = ["English", "German", "Spanish", "French", None]
INDUSTRIES = ["Technology", "Finance", "Healthcare", None]


def generated_leads(n: int, seed: int):
    rng = random.Random(seed)
    leads = []
    for i in range(n):
        region = rng.choice(REGIONS)
        alt = (rng.choice(REGIONS[:4]),) if region and rng.random() < 0.1 else ()
        leads.append(make_lead(id=f"L-{i}", row=i, region=region, language=rng.choice(LANGUAGES),
                               industry=rng.choice(INDUSTRIES), alt_regions=alt,
                               priority=rng.choice(list(Priority)), score=rng.randint(0, 100)))
    return leads


def generated_reps(n: int, seed: int):
    rng = random.Random(seed)
    return [make_rep(id=f"R-{i}", seniority=rng.choice(list(Seniority)), max_capacity=rng.randint(0, 6),
                     region=rng.choice(REGIONS), language=rng.choice(LANGUAGES), industry=rng.choice(INDUSTRIES))
            for i in range(n)]


def reference_route(leads, reps, strategy="fair_share", min_traits=None):
    """The simple, slow router: returns {lead_id: (rep_id, match_level)}."""
    settings = matching_settings(min_traits)
    key = STRATEGIES[strategy]
    pools = {}
    value, last, position = {}, {}, {}
    for pos, rep in enumerate(reps):
        value[rep.id], last[rep.id], position[rep.id] = 0.0, -1, pos
        for pool in pools_for_rep(rep):
            if not rep.is_full:
                pools.setdefault(pool, []).append(rep)
    clock = 0
    decisions = {}
    for lead in sorted(leads, key=priority_order):
        chosen = None
        for pool, level in pools_to_try(lead, settings):
            for allowed in seniority_groups(lead.priority, settings):
                candidates = [r for r in pools.get(pool, []) if r.seniority in allowed]
                if candidates:
                    chosen = (min(candidates, key=lambda r: key(r, value[r.id], last[r.id], position[r.id])), level)
                    break
            if chosen:
                break
        if chosen is None:
            decisions[lead.id] = (None, None)
            continue
        rep, level = chosen
        rep.current_capacity += 1
        value[rep.id] += lead.score
        clock += 1
        last[rep.id] = clock
        if rep.is_full:
            for pool in pools_for_rep(rep):
                pools[pool] = [r for r in pools[pool] if r is not rep]
        decisions[lead.id] = (rep.id, level)
    return decisions


@pytest.mark.parametrize("strategy", list(STRATEGIES))
@pytest.mark.parametrize("min_traits", [3, 2, 1])
def test_optimised_router_matches_slow_reference(strategy, min_traits):
    leads = generated_leads(400, seed=7)
    reps = generated_reps(60, seed=11)    # small capacities: plenty of full reps and overflow
    expected = reference_route(copy.deepcopy(leads), copy.deepcopy(reps), strategy, min_traits)

    result = route_all(copy.deepcopy(leads), copy.deepcopy(reps), strategy, min_traits=min_traits)
    got = {a.lead_id: (a.rep_id, a.match_level) for a in result.assignments}

    assert got == expected
    assert any(a.status is AssignmentStatus.MANAGER_REVIEW for a in result.assignments)
    assert any(a.status is AssignmentStatus.ASSIGNED for a in result.assignments)


def test_rows_as_dicts_matches_dataframe_to_dict():
    df = pd.DataFrame({"Lead_ID": ["A", "B", ""], "Region": ["EU", " na ", "x"], "Extra": ["1", "2", "3"]})
    expected = df.copy()
    expected["Company_Name"] = None
    assert rows_as_dicts(df, ["Lead_ID", "Company_Name", "Region"]) == expected.to_dict("records")
    assert "Company_Name" not in df.columns   # the caller's frame is untouched
