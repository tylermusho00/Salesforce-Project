"""Plain-language explanations: every decision and fix should read like a sentence, not a code."""
from conftest import LEAD_HEADER, REP_HEADER, csv, leads, make_lead, make_rep
from lead_router.cleaning import clean_leads, clean_reps
from lead_router.explain import lead_fixes, lead_tags, rep_fixes
from lead_router.models import MatchLevel, Priority, Seniority
from lead_router.routing import route_all

SR, JR, MGR = Seniority.SENIOR, Seniority.JUNIOR, Seniority.MANAGER


def test_perfect_match_sentence_is_one_short_line():
    reason = route_all(leads(1), [make_rep("R", SR)]).assignments[0].reason
    assert reason == "Perfect match: same region, language and industry."


def test_close_match_sentence_names_what_differs():
    reason = route_all(leads(1), [make_rep("R", SR, industry="Retail")]).assignments[0].reason
    assert reason == ("Close match (2 of 3): same region and language; "
                      "different industry (rep: Retail, lead: Technology).")


def test_one_of_three_sentence_names_the_shared_trait():
    reps = [make_rep("R", SR, region="Europe", industry="Retail")]
    assert route_all(leads(1), reps).assignments[0].reason == "Best available (1 of 3): only the language matches."


def test_unknown_on_both_sides_is_not_called_a_match():
    reps = [make_rep("R", SR, region="Europe", industry=None)]
    reason = route_all(leads(1, industry=None), reps).assignments[0].reason
    assert reason == "Best available (1 of 3): only the language matches."


def test_urgent_lead_to_manager_is_explained():
    reason = route_all(leads(1, priority=Priority.CRITICAL), [make_rep("M", MGR)]).assignments[0].reason
    assert reason.endswith("Seniors were full, so a Manager took this urgent lead.")


def test_manager_review_sentences_say_what_to_do():
    full = route_all(leads(2), [make_rep("R", SR, max_capacity=1)]).assignments[1].reason
    assert full == "Every rep who could take it is full. A manager needs to assign it."
    junior_only = route_all(leads(1, priority=Priority.CRITICAL), [make_rep("J", JR)]).assignments[0].reason
    assert "Juniors can't take urgent leads" in junior_only


def test_lead_fixes_show_before_and_after():
    lead = clean_leads(csv(LEAD_HEADER + "L-1,ACME CORP LLC,Nort America,EN,,5k,,Unknown\n"))[0][0]
    fixes = {field: (before, after) for field, before, after in lead_fixes(lead)}
    assert fixes["Region"] == ('"Nort America"', "North America")
    assert fixes["Language"] == ('"EN"', "English")
    assert fixes["Company size"] == ('"5k"', "5,000 employees")
    assert fixes["Company name"][1] == "Acme Corp LLC"
    assert fixes["Industry"][1].startswith("Unknown (not given)")
    assert fixes["VIP referral"][1].startswith("No")


def test_clean_lead_has_no_fixes():
    lead = clean_leads(csv(LEAD_HEADER + "L-1,Acme,North America,English,Technology,500,Attended Webinar,True\n"))[0][0]
    assert lead_fixes(lead) == []


def test_rep_fixes_are_sentences():
    rep = clean_reps(csv(REP_HEADER + "R-1,Ann,NA,EN,Unknown,sr,0,-20\n"))[0][0]
    assert set(rep_fixes(rep)) == {'Capacity was "-20"; read as 20 (a typo)', "No industry specialty listed"}


def test_lead_tags_are_short_words():
    lead = make_lead(vip_referral=True, imputed_fields=["industry", "company_size"])
    assert lead_tags(lead, MatchLevel.RELAXED_INDUSTRY) == ["VIP", "Closest match (2 of 3)",
                                                           "Missing industry and company size"]


def test_data_quality_report_keeps_examples():
    _, report = clean_leads(csv(LEAD_HEADER + "L-1,Acme,EUR,es,Tech,1,,False\nL-2,Beta,EUR,de,Tech,1,,False\n"))
    assert report.examples["region"] == ['"EUR" -> Europe']
    assert report.examples["language"] == ['"es" -> Spanish', '"de" -> German']
