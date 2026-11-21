"""The evidence a judge can ask for: matching mode, honest labels, VIP checks, the value audit,
conservation (no lead lost or doubled) and a fingerprint that proves runs are repeatable."""
import pytest

from conftest import LEAD_HEADER, REP_HEADER, csv, leads, make_lead, make_rep
from lead_router.cleaning import clean_leads, clean_reps
from lead_router.cli import main
from lead_router.config import load_rules
from lead_router.export import results_frame
from lead_router.models import AssignmentStatus, MatchLevel, Priority, Seniority
from lead_router.pipeline import STATE, PipelineError, reset_state, run_dataset, run_pipeline_frames
from lead_router.routing import route_all

SR, JR = Seniority.SENIOR, Seniority.JUNIOR
REP_ROW = "R-1,Ann,North America,English,Technology,Senior,0,22\n"


def lead_rows(*rows: str):
    return csv(LEAD_HEADER + "".join(row + "\n" for row in rows))


def sample_or_skip(**kw):
    try:
        return run_dataset("sample", **kw)
    except PipelineError:
        pytest.skip("sample dataset not present in data/")


# ---------------------------------------------------------------- matching mode per run
def test_strict_mode_per_run_vs_default():
    def run(min_traits=None):
        reps = [make_rep("exact", SR, max_capacity=1), make_rep("partial", SR, industry="Retail")]
        return route_all(leads(3), reps, min_traits=min_traits)

    default, strict = run(), run(min_traits=3)
    assert [a.rep_id for a in default.assignments] == ["exact", "partial", "partial"]
    assert [a.rep_id for a in strict.assignments] == ["exact", None, None]
    assert load_rules()["routing"]["min_traits_to_match"] == 1      # cached rules untouched


def test_matching_mode_is_recorded_in_metrics():
    frames = (lead_rows("L-1,Acme,NA,EN,Retail,500,Contact Sales,False"), csv(REP_HEADER + REP_ROW))
    default = run_pipeline_frames(*frames).metrics
    strict = run_pipeline_frames(*frames, min_traits=3).metrics
    assert (default["min_traits"], default["matching_mode"]) == (1, "Best available (3→2→1 of 3)")
    assert (strict["min_traits"], strict["matching_mode"]) == (3, "Strict (3 of 3)")
    assert default["assigned"] == 1 and strict["assigned"] == 0


def test_bad_min_traits_is_refused():
    with pytest.raises(ValueError):
        route_all(leads(1), [make_rep()], min_traits=4)


def test_strict_mode_on_sample_assigns_fewer_leads():
    default, strict = sample_or_skip(), sample_or_skip(min_traits=3)
    assert strict.metrics["assigned"] < default.metrics["assigned"]
    assert strict.metrics["junior_critical_violations"] == 0 and strict.metrics["capacity_violations"] == 0
    # Strict mode: every leftover lead gets a suggestion, and Critical ones never name a Junior.
    rep_by_id = {rep.id: rep for rep in strict.reps}
    for a in strict.manager_review:
        assert a.suggested_action
        if a.priority is Priority.CRITICAL and a.suggested_rep_id:
            assert rep_by_id[a.suggested_rep_id].seniority is not JR


def test_cli_strict_flag(capsys):
    sample_or_skip()
    assert main(["run", "--strict"]) == 0
    assert "Strict (3 of 3)" in capsys.readouterr().out
    assert main(["run", "--min-traits", "2"]) == 0
    out = capsys.readouterr().out
    assert "3→2 of 3" in out or "3->2 of 3" in out      # "->" on terminals that can't print "→"


# ---------------------------------------------------------------- honest labels
def test_unknown_industry_is_never_called_exact():
    res = route_all([make_lead(industry=None)], [make_rep("gen", SR, industry=None)])
    a = res.assignments[0]
    assert a.match_level is MatchLevel.INDUSTRY_UNKNOWN and a.match_level.traits_matched == 2
    assert a.reason.startswith("Industry unknown, so only region and language could match")
    known = route_all([make_lead()], [make_rep("tech", SR)]).assignments[0]
    assert known.match_level is MatchLevel.EXACT and known.reason.startswith("Perfect match")


def test_unknown_industry_fallback_still_allowed_in_strict_mode():
    a = route_all([make_lead(industry=None)], [make_rep("tech", SR)], min_traits=3).assignments[0]
    assert a.rep_id == "tech" and a.match_level is MatchLevel.INDUSTRY_UNKNOWN


def test_strict_review_sentence_does_not_claim_no_shared_trait():
    a = route_all([make_lead()], [make_rep("S", SR, industry="Retail")], min_traits=3).assignments[0]
    assert "at least 3 of the 3 traits" in a.reason and "even one" not in a.reason


# ---------------------------------------------------------------- VIP
def test_vip_unknown_is_flagged_but_not_scored_as_vip():
    r = run_pipeline_frames(lead_rows("L-1,Acme,NA,EN,Retail,50,,Unknown",
                                      "L-2,Beta,NA,EN,Retail,50,,",
                                      "L-3,Gamma,NA,EN,Retail,50,,False"), csv(REP_HEADER + REP_ROW))
    by_id = {lead.id: lead for lead in r.leads}
    assert [i for i in by_id if "vip_unknown" in by_id[i].flags] == ["L-1", "L-2"]
    assert all(not lead.vip_referral and lead.priority is Priority.LOW for lead in r.leads)
    assert r.metrics["vip_unknown"] == 2
    notes = dict(zip(results_frame(r)["lead_id"], results_frame(r)["notes"]))
    assert "VIP status unknown – confirm" in notes["L-1"] and "VIP status" not in notes["L-3"]


def test_vip_promoted_counts_vips_that_are_critical_only_because_of_vip():
    r = run_pipeline_frames(lead_rows("L-1,Small,NA,EN,Retail,10,,True",                        # 0 points
                                      "L-2,Big,NA,EN,Technology,5000,Requested Live Demo,True"),  # 100 points
                            csv(REP_HEADER + REP_ROW))
    assert all(lead.priority is Priority.CRITICAL and lead.score == 100 for lead in r.leads)  # scoring unchanged
    assert (r.metrics["vip_total"], r.metrics["vip_promoted"]) == (2, 1)


# ---------------------------------------------------------------- value audit
def test_value_map_for_messy_lead_values():
    _, report = clean_leads(lead_rows("L-1,A,ASIA   PACIFICEurope,EN,Tech,5k,,False",
                                      "L-2,B,ASIA   PACIFICEurope,EN,Tech,500-1000,,False",
                                      "L-3,C,NA,EN,Tech,N/A,,Unknown",
                                      "L-4,D,NA,EN,Tech,-20,,True",
                                      "L-5,E,NA,EN,Tech,\"1,000\",,False",
                                      "L-6,F,NA,EN,Tech,250,,False"))
    region = {row["raw"]: row for row in report.value_map["region"]}
    assert region["ASIA   PACIFICEurope"] == {"raw": "ASIA   PACIFICEurope", "cleaned": "Asia-Pacific",
                                              "status": "fixed", "count": 2, "alternate": "Europe"}
    assert region["NA"]["cleaned"] == "North America" and region["NA"]["count"] == 4

    sizes = {row["format"]: row for row in report.value_map["company_size"]}
    assert sizes["k suffix"] == {"format": "k suffix", "count": 1, "example": "5k", "becomes": "5,000"}
    assert sizes["range"]["example"] == "500-1000" and sizes["range"]["becomes"] == "750"
    assert sizes["missing token"]["example"] == "N/A" and sizes["missing token"]["becomes"].startswith("unknown")
    assert sizes["non-positive"]["example"] == "-20" and sizes["non-positive"]["becomes"].startswith("unknown")
    assert sizes["thousands comma"]["becomes"] == "1,000" and sizes["plain number"]["becomes"] == "250"
    assert sum(row["count"] for row in report.value_map["company_size"]) == 6

    vip = {row["raw"]: (row["cleaned"], row["status"]) for row in report.value_map["vip_referral"]}
    assert vip == {"False": ("No", "ok"), "True": ("Yes", "ok"), "Unknown": ("unknown", "missing")}
    activity = report.value_map["recent_activity"]
    assert activity == [{"raw": "", "cleaned": "unknown", "status": "missing", "count": 6, "alternate": ""}]


def test_value_map_for_rep_values():
    _, report = clean_reps(csv(REP_HEADER + "R-1,Ann,NA,EN,Tech,sr,0,-20\nR-2,Bob,EUR,de,,MGR,0,N/A\n"
                                            "R-3,Cy,EUR,de,,jr,0,30\n"))
    capacity = {row["format"]: row for row in report.value_map["max_capacity"]}
    assert capacity["non-positive"]["becomes"] == "20 (sign typo fixed)"
    assert capacity["missing token"]["becomes"].startswith("seniority default")
    assert capacity["plain number"]["becomes"] == "30"
    seniority = {row["raw"]: row["cleaned"] for row in report.value_map["seniority"]}
    assert seniority == {"sr": "Senior", "MGR": "Manager", "jr": "Junior"}
    assert {row["raw"]: row["status"] for row in report.value_map["industry"]} == {"": "missing", "Tech": "fixed"}


# ---------------------------------------------------------------- conservation
def test_every_source_row_exactly_once_with_id_collisions():
    r = run_pipeline_frames(lead_rows("A,First,NA,EN,Tech,1,,False",
                                      "A,Second,NA,EN,Tech,1,,False",    # clash -> A-2
                                      "A-2,Third,NA,EN,Tech,1,,False",   # real A-2, clashes with the rename
                                      "A,First,NA,EN,Tech,1,,False"),    # exact re-upload of row 1
                            csv(REP_HEADER + REP_ROW + "R-1,Bob,NA,EN,Tech,Senior,0,5\n"
                                "R-1-2,Cy,NA,EN,Tech,Senior,0,5\nR-1,Di,NA,EN,Tech,Senior,0,5\n"))
    assert sorted(lead.id for lead in r.leads) == ["A", "A-2", "A-2-2"]
    rep_ids = [rep.id for rep in r.reps]
    assert len(set(rep_ids)) == len(rep_ids) == 4
    m = r.metrics
    assert (m["rows_in"], m["rows_out"], m["rows_merged_as_duplicates"]) == (4, 3, 1)
    assert m["every_row_once"] is True
    assert sorted(a.lead_id for a in r.assignments) == ["A", "A-2", "A-2-2"]


def test_same_content_under_its_own_id_is_kept_not_merged():
    # (A-2, Y) has the same content as the renamed second A, but its own ID in the file:
    # it is a possible duplicate (kept, flagged), not a re-upload.
    leads_, report = clean_leads(lead_rows("A,X,NA,EN,Tech,1,,False",
                                           "A,Y,NA,EN,Tech,1,,False",
                                           "A-2,Y,NA,EN,Tech,1,,False",
                                           "A,Y,NA,EN,Tech,1,,False"))   # true re-upload of row 2
    assert [lead.id for lead in leads_] == ["A", "A-2", "A-2-2"]
    assert report.duplicates_removed == 1 and report.possible_duplicates == 1
    assert "possible_duplicate_of:A-2" in leads_[2].flags


# ---------------------------------------------------------------- proof metrics
def test_tier_share_and_critical_capacity():
    r = run_pipeline_frames(lead_rows("L-1,A,NA,EN,Tech,10,,True", "L-2,B,NA,EN,Retail,10,,False"),
                            csv(REP_HEADER + "R-1,Ann,NA,EN,Tech,Senior,0,2\nR-2,Bo,NA,EN,Tech,Junior,0,40\n"
                                "R-3,Cy,NA,EN,Tech,Manager,0,3\n"))
    m = r.metrics
    assert m["tier_share"] == {"Critical": 50.0, "High": 0.0, "Medium": 0.0, "Low": 50.0}
    assert m["expected_tier_share"] == {"Critical": 10, "High": 20, "Medium": 40, "Low": 30}
    assert m["critical_slots"] == 5 and m["critical_waiting"] == 0


def test_critical_waiting_counts_critical_leads_in_review():
    r = run_pipeline_frames(lead_rows("L-1,A,NA,EN,Tech,10,,True"),
                            csv(REP_HEADER + "R-1,Bo,NA,EN,Tech,Junior,0,40\n"))
    assert r.metrics["critical_waiting"] == 1 and r.metrics["critical_slots"] == 0


def test_fingerprint_is_repeatable_and_sensitive():
    first = sample_or_skip()
    again = sample_or_skip()                       # same input twice, no reset in between
    assert first.metrics["fingerprint"] == again.metrics["fingerprint"]
    STATE.set(again)
    reset_state()                                  # the dashboard's Reset button
    after_reset = sample_or_skip()
    assert after_reset.metrics["fingerprint"] == first.metrics["fingerprint"]
    assert len(first.metrics["fingerprint"]) == 12
    assert sample_or_skip(min_traits=3).metrics["fingerprint"] != first.metrics["fingerprint"]


def test_results_frame_has_suggestion_columns():
    r = run_pipeline_frames(lead_rows("L-1,A,NA,EN,Tech,10,,True"),
                            csv(REP_HEADER + "R-1,Bo,NA,EN,Tech,Junior,0,40\n"))
    row = results_frame(r).iloc[0]
    assert row["status"] == AssignmentStatus.MANAGER_REVIEW.value
    assert row["suggested_action"].startswith("Needs a Senior") and row["suggested_rep"] is None
