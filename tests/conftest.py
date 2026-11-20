"""Tiny hand-built leads and reps so routing tests are deterministic and readable."""
from __future__ import annotations

import io

import pandas as pd
import pytest

from lead_router.config import DATA_DIR
from lead_router.models import Lead, Priority, Rep, Seniority
from lead_router.pipeline import STATE


def make_lead(id="L-1", region="North America", language="English", industry="Technology",
              priority=Priority.MEDIUM, score=50, row=0, **kw) -> Lead:
    lead = Lead(id=id, company_name=kw.pop("company_name", f"Co {id}"), region=region, language=language,
                industry=industry, company_size=kw.pop("company_size", 500),
                recent_activity=kw.pop("recent_activity", "Attended Webinar"),
                vip_referral=kw.pop("vip_referral", False), row_number=row, **kw)
    lead.priority, lead.score = priority, score
    return lead


def make_rep(id="R-1", seniority=Seniority.SENIOR, max_capacity=5, region="North America",
             language="English", industry="Technology", current=0) -> Rep:
    return Rep(id=id, name=f"Rep {id}", region=region, language=language, industry=industry,
               seniority=seniority, current_capacity=current, max_capacity=max_capacity)


def leads(n, **kw) -> list[Lead]:
    return [make_lead(id=f"L-{i}", row=i, **kw) for i in range(n)]


def csv(text: str) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)


LEAD_HEADER = "Lead_ID,Company_Name,Region,Language,Industry_Vertical,Company_Size,Recent_Activity,VIP_Referral\n"
REP_HEADER = "Rep_ID,Name,Region,Language,Industry_Vertical_Expertise,Seniority_Level,Current_Capacity,Max_Capacity\n"


def _with_min_traits(monkeypatch, minimum: int) -> None:
    """Make routing see rules.yaml with a different routing.min_traits_to_match."""
    import copy

    import lead_router.routing as routing
    from lead_router.config import load_rules

    rules = copy.deepcopy(load_rules())
    rules["routing"]["min_traits_to_match"] = minimum
    monkeypatch.setattr(routing, "load_rules", lambda: rules)


@pytest.fixture
def strict_matching(monkeypatch):
    """Run a test with strict 3-of-3 matching (the brief's literal rule)."""
    _with_min_traits(monkeypatch, 3)


@pytest.fixture
def two_trait_minimum(monkeypatch):
    """Run a test where at least 2 of the 3 traits must match."""
    _with_min_traits(monkeypatch, 2)


@pytest.fixture(autouse=True)
def _clean_state():
    STATE.reset()
    yield
    STATE.reset()


@pytest.fixture
def scale_paths():
    leads_path, reps_path = DATA_DIR / "leads_scale.csv", DATA_DIR / "reps_scale.csv"
    if not (leads_path.exists() and reps_path.exists()):
        pytest.skip("scale dataset not present in data/")
    return leads_path, reps_path
