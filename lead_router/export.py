"""
export.py - turn a PipelineResult into tables (pandas DataFrames).

    results_frame()     one row per lead, in routing order  -> dashboard grid / results CSV
    reps_frame()        one row per rep with load and utilization
    salesforce_frame()  a Salesforce Data Loader "update Leads" file (Id, OwnerId, custom fields)
"""
from __future__ import annotations

import pandas as pd

from .explain import lead_fixes, lead_tags, rep_fixes
from .models import PipelineResult

RESULT_COLUMNS = ["queue_rank", "lead_id", "source_lead_id", "company_name", "priority", "score", "status", "rep_id",
                  "source_rep_id", "rep_name", "rep_seniority", "region", "language", "industry", "company_size",
                  "recent_activity", "vip_referral", "match_level", "overflow_reason", "reason",
                  "score_explanation", "imputed_fields", "notes", "data_fixes",
                  "suggested_action", "suggested_rep"]


def results_frame(result: PipelineResult | None) -> pd.DataFrame:
    """One row per lead, in routing (priority) order."""
    if result is None:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    by_id = {l.id: l for l in result.leads}
    rep_by_id = {rep.id: rep for rep in result.reps}
    rows = []
    for rank, a in enumerate(result.assignments, start=1):
        l = by_id[a.lead_id]
        rep = rep_by_id.get(a.rep_id)
        rows.append({
            "queue_rank": rank,
            "lead_id": a.lead_id,
            "source_lead_id": l.raw.get("Lead_ID") if l.raw else None,   # as written in the file
            "company_name": l.company_name,
            "priority": a.priority.value,
            "score": a.score,
            "status": a.status.value,
            "rep_id": a.rep_id,
            "source_rep_id": rep.raw.get("Rep_ID") if rep and rep.raw else None,
            "rep_name": a.rep_name,
            "rep_seniority": a.rep_seniority.value if a.rep_seniority else None,
            "region": l.region or "Unknown",
            "language": l.language or "Unknown",
            "industry": l.industry or "Unknown",
            "company_size": l.company_size,
            "recent_activity": l.recent_activity or "",
            "vip_referral": l.vip_referral,
            "match_level": a.match_level.value if a.match_level else None,
            "overflow_reason": a.overflow_reason.value if a.overflow_reason else None,
            "reason": a.reason,
            "score_explanation": l.explanation,
            "imputed_fields": ", ".join(l.imputed_fields),
            "notes": " | ".join(lead_tags(l, a.match_level)),
            "data_fixes": "; ".join(f"{field}: {before} -> {after}" for field, before, after in lead_fixes(l)),
            # Manager Review only: what to do next and the rep it names (see review.py)
            "suggested_action": a.suggested_action,
            "suggested_rep": a.suggested_rep_id,
        })
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def reps_frame(result: PipelineResult | None) -> pd.DataFrame:
    cols = ["rep_id", "source_rep_id", "name", "seniority", "region", "language", "industry", "load",
            "max_capacity", "utilization", "available", "data_fixes"]
    if result is None:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([{
        "rep_id": r.id, "source_rep_id": r.raw.get("Rep_ID") if r.raw else None, "name": r.name, "seniority": r.seniority.value,
        "region": r.region or "Unknown", "language": r.language or "Unknown",
        "industry": r.industry or "Unknown", "load": r.current_capacity,
        "max_capacity": r.max_capacity,
        "utilization": round(100 * r.current_capacity / r.max_capacity, 1) if r.max_capacity else 0.0,
        "available": r.available, "data_fixes": "; ".join(rep_fixes(r)),
    } for r in result.reps], columns=cols)


def salesforce_frame(results: pd.DataFrame) -> pd.DataFrame:
    """A Data Loader / Bulk API *template*, not a ready-to-import file: it uses the challenge's lead
    and rep IDs (renamed "-2" IDs included) as Id / OwnerId, and MANAGER_REVIEW_QUEUE as a stand-in
    owner. Before importing, map these to your org's 18-character record IDs and queue ID, and
    rename the __c fields to your org's custom field API names."""
    return pd.DataFrame({
        "Id": results["lead_id"],
        "Source_Lead_ID__c": results["source_lead_id"],   # the ID before any "-2" rename
        "Source_Rep_ID__c": results["source_rep_id"],
        "OwnerId": results["rep_id"].fillna("MANAGER_REVIEW_QUEUE"),
        "Lead_Priority__c": results["priority"],
        "Lead_Score__c": results["score"],
        "Routing_Status__c": results["status"],
        "Routing_Reason__c": results["reason"],
    })
