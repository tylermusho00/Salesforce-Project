import pytest

from conftest import make_lead
from lead_router.models import Priority
from lead_router.scoring import score_all, score_lead, tier_for


@pytest.mark.parametrize("points,tier", [
    (100, Priority.CRITICAL), (85, Priority.CRITICAL), (84, Priority.HIGH), (60, Priority.HIGH),
    (59, Priority.MEDIUM), (40, Priority.MEDIUM), (39, Priority.LOW), (0, Priority.LOW),
])
def test_tier_boundaries(points, tier):
    assert tier_for(points) is tier


def test_cheat_sheet_example_enterprise_demo_is_critical():
    lead = make_lead(company_size=1000, recent_activity="Requested Live Demo", industry="Retail")
    res = score_lead(lead)
    assert (res.points, res.priority) == (85, Priority.CRITICAL)


@pytest.mark.parametrize("size,activity,industry,expected", [
    (99, "Visited Pricing Page", "Manufacturing", 5),        # SMB
    (100, "Attended Webinar", "Healthcare", 65),             # Mid-Market
    (999, "Downloaded Whitepaper", "Finance", 55),
    (5000, "Contact Sales", "Technology", 100),
    (None, None, None, 25),                                  # 20 size fallback + 0 activity + 5 neutral industry
    (500, "Opened Marketing Email", "Retail", 30),           # unscored activity -> 0
])
def test_matrix(size, activity, industry, expected):
    lead = make_lead(company_size=size, recent_activity=activity, industry=industry)
    assert score_lead(lead).points == expected


@pytest.mark.parametrize("kwargs", [
    {}, {"company_size": None, "recent_activity": None, "industry": None}, {"company_size": 1, "industry": "Retail"},
])
def test_vip_override_even_with_missing_or_low_value_fields(kwargs):
    res = score_lead(make_lead(vip_referral=True, **kwargs))
    assert (res.points, res.priority) == (100, Priority.CRITICAL)
    assert "VIP" in res.explanation


def test_missing_fields_do_not_crash_and_explain():
    res = score_lead(make_lead(region=None, language=None, industry=None, company_size=None, recent_activity=None))
    assert res.priority in Priority and "unknown" in res.explanation


def test_score_all_sorts_by_tier_then_score():
    ls = [make_lead(id="low", company_size=5, recent_activity=None, industry="Retail", row=0),
          make_lead(id="vip", vip_referral=True, row=1),
          make_lead(id="high", company_size=1000, recent_activity="Attended Webinar", industry="Retail", row=2),
          make_lead(id="crit", company_size=1000, recent_activity="Contact Sales", industry="Technology", row=3)]
    assert [l.id for l in score_all(ls)] == ["vip", "crit", "high", "low"]
