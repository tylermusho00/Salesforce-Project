"""Machine learning (lead_router/anomaly.py): flags unusual records, never changes the routing."""
from __future__ import annotations

import pytest

from conftest import make_lead, make_rep
from lead_router import pipeline
from lead_router.anomaly import MIN_ROWS, flag_leads, flag_reps
from lead_router.models import Seniority
from lead_router.pipeline import run_dataset


def normal_leads(n=300):
    """Ordinary leads with company sizes spread between 20 and 5,000 employees."""
    return [make_lead(f"L-{i}", company_size=20 + (i * 37) % 4980, row=i) for i in range(n)]


def test_an_impossible_company_size_is_flagged_with_a_reason():
    leads = normal_leads() + [make_lead("L-HUGE", company_size=10_000_000_000, row=999)]
    flag_leads(leads)
    huge = leads[-1]
    assert huge.anomaly is not None and "10,000,000,000" in huge.anomaly
    assert sum(1 for l in leads if l.anomaly) <= 5        # only a handful of rows are flagged, not everything


def test_a_rep_with_a_strange_capacity_is_flagged():
    reps = [make_rep(f"R-{i}", Seniority.SENIOR, max_capacity=20 + i % 5) for i in range(60)]
    reps.append(make_rep("R-ODD", Seniority.SENIOR, max_capacity=400))
    flag_reps(reps)
    assert reps[-1].anomaly is not None and "Max capacity of 400" in reps[-1].anomaly


def test_too_few_rows_to_learn_from_flags_nothing():
    leads = normal_leads(MIN_ROWS - 1)
    assert flag_leads(leads) == 0 and all(l.anomaly is None for l in leads)


def test_same_data_same_flags():
    first, second = normal_leads(), normal_leads()
    first.append(make_lead("L-HUGE", company_size=10_000_000_000))
    second.append(make_lead("L-HUGE", company_size=10_000_000_000))
    flag_leads(first), flag_leads(second)
    assert [l.anomaly for l in first] == [l.anomaly for l in second]


@pytest.fixture
def scale(scale_run):
    return scale_run[0]   # the shared 50k run (conftest.scale_run)


def test_ml_never_changes_a_route(scale, monkeypatch):
    """Run the 50k file again with the model switched off: every lead gets the exact same rep and priority."""
    monkeypatch.setattr(pipeline, "flag_leads", lambda leads: 0)
    monkeypatch.setattr(pipeline, "flag_reps", lambda reps: 0)
    without_ml = run_dataset("scale")
    decisions = lambda r: [(a.lead_id, a.rep_id, a.priority, a.score) for a in r.assignments]
    assert decisions(without_ml) == decisions(scale)
    assert all(l.anomaly is None for l in without_ml.leads)


def test_every_ten_billion_employee_company_is_caught(scale):
    """The 50k file has 73 leads claiming 10,000,000,000 employees (more than the world's population)."""
    impossible = [l for l in scale.leads if l.company_size and l.company_size >= 1_000_000_000]
    assert len(impossible) == 73
    assert all(l.anomaly and "Company size" in l.anomaly for l in impossible)
    assert scale.metrics["unusual_leads"] < 0.01 * len(scale.leads)   # under 1% flagged: still a short list
