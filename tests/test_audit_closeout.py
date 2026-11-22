"""Regressions for source retention and identity mapping, independent of parser decisions."""
from copy import deepcopy
import pytest
from conftest import csv, LEAD_HEADER, REP_HEADER
from lead_router.cleaning import clean_leads
from lead_router.pipeline import run_pipeline_frames, conservation_check
from lead_router.export import results_frame, reps_frame, salesforce_frame


@pytest.mark.parametrize("field,left,right", [("size", "lots", "many"), ("vip", "Unknown", "maybe"),
                                               ("region", "NA", "North America")])
@pytest.mark.parametrize("reverse", [False, True])
def test_distinct_raw_values_survive_even_when_cleaned_values_and_flags_match(field,left,right,reverse):
    rows = []
    for value in ([right,left] if reverse else [left,right]):
        region = value if field == "region" else "NA"
        size = value if field == "size" else "50"
        vip = value if field == "vip" else "False"
        rows.append(f"A,Acme,{region},EN,Retail,{size},,{vip}\n")
    leads, report = clean_leads(csv(LEAD_HEADER + "".join(rows)))
    assert len(leads) == 2 and report.duplicates_removed == 0
    assert report.source_rows_preserved is True
    assert len({tuple(lead.raw.values()) for lead in leads}) == 2


def test_source_audit_detects_a_loss_even_when_duplicate_accounting_looks_consistent():
    r = run_pipeline_frames(csv(LEAD_HEADER + "A,Acme,NA,EN,Retail,lots,,False\nA,Acme,NA,EN,Retail,many,,False\n"),
        csv(REP_HEADER + "R,Ann,NA,EN,Retail,Senior,0,5\n"))
    quality = deepcopy(r.lead_quality)
    quality.duplicates_removed = 1
    quality.source_rows_preserved = False
    assert conservation_check(quality, r.leads[:1], r.assignments[:1])["every_row_once"] is False


def test_exports_preserve_source_rep_ids_when_internal_ids_are_renamed():
    r = run_pipeline_frames(csv(LEAD_HEADER + "L,Acme,EUR,DE,Finance,1000,Requested Live Demo,True\n"),
        csv(REP_HEADER + "R,One,NA,EN,Retail,Senior,0,1\nR,Two,EUR,DE,Finance,Senior,0,5\n"))
    results = results_frame(r); reps = reps_frame(r); template = salesforce_frame(results)
    assert results.iloc[0]["rep_id"] != "R"
    assert results.iloc[0]["source_rep_id"] == "R"
    assert reps["source_rep_id"].tolist() == ["R", "R"]
    assert template.iloc[0]["Source_Rep_ID__c"] == "R"



def test_independent_source_audit_catches_a_wrong_duplicate_classification(monkeypatch):
    from lead_router.cleaning import DuplicateChecker
    real = DuplicateChecker.should_keep
    def faulty(self, lead):
        if self.kept_by_source_id:
            self.report.duplicates_removed += 1
            return False
        return real(self, lead)
    monkeypatch.setattr(DuplicateChecker, "should_keep", faulty)
    r = run_pipeline_frames(csv(LEAD_HEADER + "A,Acme,NA,EN,Retail,lots,,False\nA,Acme,NA,EN,Retail,many,,False\n"),
        csv(REP_HEADER + "R,Ann,NA,EN,Retail,Senior,0,5\n"))
    assert r.metrics["rows_in"] == r.metrics["rows_out"] + r.metrics["rows_merged_as_duplicates"]
    assert r.metrics["source_rows_preserved"] is False
    assert r.metrics["every_row_once"] is False
