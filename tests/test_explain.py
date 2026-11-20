"""Plain-language explanations: every decision and fix should read like a sentence, not a code."""
from conftest import LEAD_HEADER, REP_HEADER, csv, leads, make_lead, make_rep
from lead_router.cleaning import clean_leads, clean_reps
from lead_router.explain import lead_fixes, lead_tags, rep_fixes
from lead_router.models import MatchLevel, Priority, Seniority
from lead_router.routing import route_all

SR, JR, MGR = Seniority.SENIOR, Seniority.JUNIOR, Seniority.MANAGER


def test_perfect_match_sentence():
    reason = route_all(leads(1), [make_rep("R", SR)]).assignments[0].reason
    assert reason.startswith("Perfect match")
    assert "covers North America" in reason and "speaks English" in reason and "specializes in Technology" in reason
    assert "now has 1 of 5 leads" in reason


def test_close_match_sentence_names_what_differs():
    reason = route_all(leads(1), [make_rep("R", SR, industry="Retail")]).assignments[0].reason
    assert reason.startswith("Close match (2 of 3")
    assert "specializes in Retail rather than Technology" in reason


def test_urgent_lead_to_manager_is_explained():
    reason = route_all(leads(1, priority=Priority.CRITICAL), [make_rep("M", MGR)]).assignments[0].reason
    assert "every matching Senior rep was full, so a Manager took it" in reason


def test_manager_review_sentences_say_what_to_do():
    full = route_all(leads(2), [make_rep("R", SR, max_capacity=1)]).assignments[1].reason
    assert "all full" in full and "A manager needs to" in full
    junior_only = route_all(leads(1, priority=Priority.CRITICAL), [make_rep("J", JR)]).assignments[0].reason
    assert "never go to Junior reps" in junior_only


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
