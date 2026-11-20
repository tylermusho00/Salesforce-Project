"""Tests for the distribution method (lead_router/distribution.py): how ties between equally
qualified reps are broken. Each test isolates one layer of the fair_share key."""
from collections import Counter

import pytest

from conftest import leads, make_lead, make_rep
from lead_router.distribution import STRATEGIES, distribution_fairness, get_strategy, jain_index
from lead_router.models import Priority, Seniority
from lead_router.routing import route_all

SR, JR = Seniority.SENIOR, Seniority.JUNIOR


def counts(result):
    return Counter(a.rep_id for a in result.assignments if a.rep_id)


# --------------------------------------------------------------------------- Jain's index
def test_jain_index_extremes():
    assert jain_index([5, 5, 5, 5]) == 1.0                 # perfectly even
    assert jain_index([10, 0, 0, 0]) == pytest.approx(0.25)  # one rep has everything -> 1/n
    assert jain_index([]) == 1.0 and jain_index([0, 0]) == 1.0


def test_distribution_fairness_only_compares_interchangeable_reps():
    # Different seniorities are NOT compared with each other (Managers are held back on purpose).
    reps = [make_rep("S1", SR, 10, current=5), make_rep("S2", SR, 10, current=5),
            make_rep("M", Seniority.MANAGER, 10, current=0)]
    assert distribution_fairness(reps) == 1.0


# --------------------------------------------------------------------------- layer 1: proportional share
def test_fair_share_is_proportional_to_capacity():
    reps = [make_rep("BIG", JR, max_capacity=50), make_rep("SMALL", JR, max_capacity=20)]
    result = route_all(leads(35), reps, strategy="fair_share")
    assert counts(result) == {"BIG": 25, "SMALL": 10}       # both exactly 50% full


def test_fair_share_interleaves_instead_of_bursting():
    reps = [make_rep("BIG", JR, max_capacity=50), make_rep("SMALL", JR, max_capacity=20)]
    order = [a.rep_id for a in route_all(leads(7), reps, strategy="fair_share").assignments]
    # 5:2 ratio, spread out, never more than 3 in a row for the big rep
    assert Counter(order) == {"BIG": 5, "SMALL": 2}
    assert "BIG,BIG,BIG,BIG" not in ",".join(order)


def test_round_robin_ignores_capacity():
    reps = [make_rep("BIG", JR, max_capacity=50), make_rep("SMALL", JR, max_capacity=20)]
    result = route_all(leads(20), reps, strategy="round_robin")
    assert counts(result) == {"BIG": 10, "SMALL": 10}       # SMALL is 50% full, BIG only 20%


def test_first_match_piles_onto_one_rep():
    reps = [make_rep("A", SR, 10), make_rep("B", SR, 10)]
    assert counts(route_all(leads(10), reps, strategy="first_match")) == {"A": 10}


# --------------------------------------------------------------------------- layer 2: value fairness
def test_value_tiebreak_shares_good_leads():
    """A and B both hold one lead, but A's was worth 90 points and B's only 10.
    The next lead goes to B (less value received), even though plain round-robin would pick A."""
    reps = [make_rep("A", SR, max_capacity=4), make_rep("B", SR, max_capacity=4)]
    arriving = [make_lead("big", score=90, row=1), make_lead("small", score=10, row=2),
                make_lead("next", score=50, row=3)]
    fair = route_all(arriving, reps, strategy="fair_share", live=True)
    assert [a.rep_id for a in fair.assignments] == ["A", "B", "B"]


# --------------------------------------------------------------------------- layer 3: round-robin rotation
def test_equal_reps_rotate_in_turn():
    reps = [make_rep(f"R{i}", SR, 10) for i in range(4)]
    order = [a.rep_id for a in route_all(leads(8), reps, strategy="fair_share").assignments]
    assert order == ["R0", "R1", "R2", "R3", "R0", "R1", "R2", "R3"]


# --------------------------------------------------------------------------- layer 4: reproducible
def test_same_input_same_output():
    def run():
        reps = [make_rep(f"R{i}", SR, 3 + i) for i in range(5)]
        return [a.rep_id for a in route_all(leads(15), reps).assignments]
    assert run() == run()


# --------------------------------------------------------------------------- rules still hold for every strategy
@pytest.mark.parametrize("strategy", list(STRATEGIES))
def test_every_strategy_respects_capacity_and_seniority(strategy):
    reps = [make_rep("J", JR, 5), make_rep("S", SR, 3), make_rep("M", Seniority.MANAGER, 1)]
    ls = leads(4, priority=Priority.CRITICAL, score=90) + [make_lead(f"x{i}", row=10 + i) for i in range(10)]
    result = route_all(ls, reps, strategy=strategy)
    assert all(r.current_capacity <= r.max_capacity for r in reps)
    assert not any(a.rep_id == "J" and a.priority is Priority.CRITICAL for a in result.assignments)
    assert len(result.assignments) == len(ls)


def test_fairness_is_measured_during_the_run():
    reps = [make_rep("A", SR, 10), make_rep("B", SR, 10)]
    result = route_all(leads(10), reps)
    assert len(result.fairness_timeline) == 10
    assert all(0 < f <= 1 for f in result.fairness_timeline)


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="Unknown distribution strategy"):
        get_strategy("random_guess")
