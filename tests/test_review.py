"""Manager Review suggestions (review.py): every branch, using final capacity, never a Junior for
a Critical lead, and never the same last slot promised twice."""
from conftest import leads, make_lead, make_rep
from lead_router.models import AssignmentStatus, Priority, Seniority
from lead_router.routing import route_all

CRIT = Priority.CRITICAL
JR, SR, MGR = Seniority.JUNIOR, Seniority.SENIOR, Seniority.MANAGER


def review_of(result):
    return {a.lead_id: a for a in result.overflow}


def test_assigned_leads_get_no_suggestion():
    res = route_all(leads(1), [make_rep("S", SR)])
    a = res.assignments[0]
    assert a.status is AssignmentStatus.ASSIGNED
    assert a.suggested_action is None and a.suggested_rep_id is None


def test_missing_routing_data_asks_to_fill_it_in():
    res = route_all([make_lead(region=None, language=None, industry=None)], [make_rep("S", SR)])
    a = res.assignments[0]
    assert a.suggested_action == "Fill in region, language and industry for this lead, then re-run."
    assert a.suggested_rep_id is None


def test_strict_mode_reassign_needs_a_policy_exception():
    reps = [make_rep("S", SR, industry="Retail")]            # shares region + language only
    a = route_all([make_lead()], reps, min_traits=3).assignments[0]
    assert a.status is AssignmentStatus.MANAGER_REVIEW
    assert a.suggested_action.startswith("Reassign to Rep S (Senior, shares region + language, 5 open slots).")
    assert "Needs a policy exception" in a.suggested_action and "3 of 3" in a.suggested_action
    assert a.suggested_rep_id == "S"
    assert reps[0].current_capacity == 0                     # a suggestion never moves the lead itself


def test_reassign_picks_the_rep_sharing_the_most_traits():
    reps = [make_rep("one", SR, region="Europe", industry="Retail"),     # language only
            make_rep("two", SR, industry="Retail")]                      # region + language
    a = route_all([make_lead()], reps, min_traits=3).assignments[0]
    assert a.suggested_rep_id == "two"


def test_alternate_region_only_counts_when_rules_allow_it(monkeypatch):
    import copy

    import lead_router.routing as routing
    from lead_router.config import load_rules

    rules = copy.deepcopy(load_rules())
    rules["routing"]["try_alternate_region_when_ambiguous"] = False
    monkeypatch.setattr(routing, "load_rules", lambda: rules)
    lead = make_lead(region="Europe", alt_regions=("North America",))
    a = route_all([lead], [make_rep("S", SR)], min_traits=3).assignments[0]   # rep is in North America
    assert a.status is AssignmentStatus.MANAGER_REVIEW
    assert "shares language + industry" in a.suggested_action and "policy exception" in a.suggested_action


def test_partly_missing_lead_gets_a_fill_in_hint():
    a = route_all([make_lead(region=None)], [make_rep("S", SR, language="German")], min_traits=3).assignments[0]
    assert a.suggested_rep_id == "S"                                     # shares the industry
    assert "Its region is unknown" in a.suggested_action


def test_critical_only_juniors_match_never_suggests_the_junior():
    reps = [make_rep("J", JR, max_capacity=50),                                       # perfect, but Junior
            make_rep("S", SR, region="Europe", language="German", industry="Retail")]  # shares nothing, free
    a = route_all(leads(1, priority=CRIT), reps).assignments[0]
    assert a.status is AssignmentStatus.MANAGER_REVIEW
    assert a.suggested_action.startswith("Needs a Senior")
    assert a.suggested_rep_id == "S" and "Rep J" not in a.suggested_action


def test_critical_only_juniors_free_asks_to_raise_a_full_seniors_capacity():
    reps = [make_rep("S", SR, max_capacity=1), make_rep("J", JR, max_capacity=50)]
    res = route_all(leads(2, priority=CRIT), reps)
    a = res.assignments[1]
    assert a.status is AssignmentStatus.MANAGER_REVIEW
    assert a.suggested_action.startswith("Needs a Senior: raise Rep S's capacity")
    assert a.suggested_rep_id == "S"


def test_critical_with_no_senior_on_the_team():
    a = route_all(leads(1, priority=CRIT), [make_rep("J", JR)]).assignments[0]
    assert a.suggested_action.startswith("Needs a Senior") and a.suggested_rep_id is None


def test_coverage_missing_names_a_manager_with_room():
    lead = make_lead(region="Asia-Pacific", language="Spanish", industry="Retail")
    reps = [make_rep("S", SR), make_rep("M", MGR, max_capacity=3)]       # both North America / English / Tech
    a = route_all([lead], reps).assignments[0]
    assert a.suggested_action.startswith("Coverage missing: no rep covers Asia-Pacific, Spanish or Retail.")
    assert "Meanwhile Rep M (Manager, 3 open slots)" in a.suggested_action
    assert a.suggested_rep_id == "M"


def test_coverage_missing_without_a_free_manager():
    lead = make_lead(region="Asia-Pacific", language="Spanish", industry="Retail")
    a = route_all([lead], [make_rep("S", SR)]).assignments[0]
    assert a.suggested_action.startswith("Coverage missing") and a.suggested_rep_id is None


def test_team_at_capacity_names_the_closest_full_rep():
    reps = [make_rep("S", SR, max_capacity=1), make_rep("other", SR, max_capacity=1, industry="Retail")]
    res = route_all(leads(3), reps)
    a = res.assignments[2]
    assert a.suggested_action.startswith("Team at capacity: raise Rep S's max capacity")
    assert "hold for the next cycle" in a.suggested_action
    assert a.suggested_rep_id == "S"                                     # shares all 3 traits


def test_last_slot_is_never_promised_twice_and_critical_goes_first():
    reps = [make_rep("S", SR, max_capacity=1, industry="Retail")]       # 1 slot, only 2 of 3 traits
    low = make_lead("low", priority=Priority.LOW, score=10, row=0)
    crit = make_lead("crit", priority=CRIT, score=90, row=1)
    review = review_of(route_all([low, crit], reps, min_traits=3))
    assert review["crit"].suggested_action.startswith("Reassign to Rep S")
    assert review["low"].suggested_action.startswith("Team at capacity")


def test_promised_slots_never_exceed_free_capacity():
    reps = [make_rep(f"S{i}", SR, max_capacity=2, industry="Retail") for i in range(3)]   # 6 free slots
    res = route_all(leads(10), reps, min_traits=3)
    promised = [a.suggested_rep_id for a in res.overflow if a.suggested_action.startswith("Reassign")]
    assert len(promised) == 6
    assert all(promised.count(rep.id) <= rep.available for rep in reps)
