"""
explain.py - turns the engine's decisions into sentences anyone can read.

Nothing here changes a decision; it only describes them:
    routing_sentence()   why a lead went to a particular rep
    review_sentence()    why a lead went to Manager Review
    lead_fixes()         what we cleaned up in a lead's row, as "before -> after"
    rep_fixes()          what we cleaned up in a rep's row
    lead_tags()          a few short tags for the lead table (VIP, Closest match, ...)
"""
from __future__ import annotations

import math
from typing import Any

from .cleaning import is_missing_token, normalize_text
from .config import load_rules
from .models import Lead, MatchLevel, OverflowReason, Priority, Rep, Seniority

FIELD_LABELS = {
    "lead_id": "Lead ID", "company_name": "Company name", "region": "Region", "language": "Language",
    "industry": "Industry", "company_size": "Company size", "recent_activity": "Recent activity",
    "vip_referral": "VIP referral", "rep_id": "Rep ID", "name": "Name", "seniority": "Seniority",
    "max_capacity": "Max capacity", "current_capacity": "Starting lead count",
}


def _join(items: list[str]) -> str:
    """['a'] -> 'a';  ['a','b'] -> 'a and b';  ['a','b','c'] -> 'a, b and c'"""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _raw(value: Any) -> str:
    """How a raw CSV value looked, for "before" columns."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or str(value).strip() == "":
        return "(blank)"
    return f'"{value}"'


# ======================================================================================
# Routing
# ======================================================================================
def _trait_differences(lead: Lead, rep: Rep) -> tuple[list[str], list[str]]:
    """(traits that match, short notes for traits that don't), e.g. (['region', 'language'],
    ['different industry (rep: Retail, lead: Finance)'])."""
    same, different = [], []
    lead_regions = [r for r in (lead.region, *lead.alt_regions) if r]
    pairs = (
        ("region", rep.region in lead_regions if rep.region else False, rep.region, lead.region),
        ("language", bool(rep.language) and rep.language == lead.language, rep.language, lead.language),
        ("industry", bool(rep.industry) and rep.industry == lead.industry, rep.industry, lead.industry),
    )
    for trait, matches, rep_value, lead_value in pairs:
        if matches:
            same.append(trait)
        elif rep_value is None and lead_value is None:
            continue  # unknown on both sides: neither a match nor a difference worth listing
        else:
            different.append(f"different {trait} (rep: {rep_value or 'none listed'}, lead: {lead_value or 'unknown'})")
    return same, different


def _seniority_note(lead: Lead, rep: Rep) -> str:
    if rep.seniority is not Seniority.MANAGER:
        return ""
    if lead.priority is Priority.CRITICAL:
        return " Seniors were full, so a Manager took this urgent lead."
    return " Juniors and Seniors were full, so a Manager took it."


def routing_sentence(lead: Lead, rep: Rep, match_level: MatchLevel) -> str:
    """One short line, e.g.
         'Perfect match: same region, language and industry.'
         'Close match (2 of 3): same region and language; different industry (rep: Retail, lead: Finance).'
         'Best available (1 of 3): only the language matches.'
       Who the rep is and how busy they are is shown next to it, so it isn't repeated here."""
    traits = match_level.traits_matched
    if traits == 3 and lead.industry is None:
        line = "Best match: same region and language (the lead's industry isn't known)."
    elif traits == 3:
        line = ("Perfect match (using the lead's second listed region)." if match_level is MatchLevel.ALTERNATE_REGION
                else "Perfect match: same region, language and industry.")
    elif traits == 2:
        same, different = _trait_differences(lead, rep)
        line = f"Close match (2 of 3): same {_join(same)}; " + "; ".join(different) + "."
    else:
        same, _ = _trait_differences(lead, rep)
        line = (f"Best available (1 of 3): only the {same[0]} matches." if same
                else "Best available (1 of 3): closest rep with room.")
    return line + _seniority_note(lead, rep)


def review_sentence(lead: Lead, reason: OverflowReason) -> str:
    """Why nobody got this lead, in one line, ending with what the manager should do."""
    if reason is OverflowReason.ALL_AT_CAPACITY:
        return "Every rep who could take it is full. A manager needs to assign it."
    if reason is OverflowReason.NO_SENIOR_AVAILABLE:
        return "Urgent lead, but only Junior reps match (Juniors can't take urgent leads). Needs a manager."
    if reason is OverflowReason.NO_MATCH:
        return "No rep covers this region, language or industry. Needs a manager."
    return "Region, language and industry are all unknown, so there's nothing to match on."


# ======================================================================================
# Data fixes
# ======================================================================================
def lead_fixes(lead: Lead) -> list[tuple[str, str, str]]:
    """(field, before, after) for every value we changed or filled in. Empty list = row was clean."""
    raw = lead.raw or {}
    scoring = load_rules()["scoring"]
    fixes: list[tuple[str, str, str]] = []

    def changed(raw_value: Any, clean: str | None) -> bool:
        return raw_value is None or str(raw_value) != (clean or "")

    if "duplicate_id_renamed" in lead.flags:
        fixes.append(("Lead ID", _raw(raw.get("Lead_ID")),
                      f"{lead.id} (another lead already used this ID, so both are kept)"))
    elif "lead_id" in lead.imputed_fields:
        fixes.append(("Lead ID", "(blank)", f"{lead.id} (made up so the lead isn't lost)"))

    if changed(raw.get("Company_Name"), lead.company_name):
        fixes.append(("Company name", _raw(raw.get("Company_Name")), lead.company_name))

    for field, column, value in (("Region", "Region", lead.region), ("Language", "Language", lead.language),
                                 ("Industry", "Industry_Vertical", lead.industry)):
        before = raw.get(column)
        if value is None:
            not_given = _raw(before) == "(blank)" or is_missing_token(normalize_text(before))
            after = "Unknown (not given)" if not_given else "Unknown (couldn't read it)"
            if field == "Industry":
                after += f"; scored as a neutral +{scoring['industry']['missing_points']}"
            fixes.append((field, _raw(before), after))
        elif changed(before, value):
            after = value
            if field == "Region" and lead.alt_regions:
                after += f" (two regions were listed; {_join(list(lead.alt_regions))} kept as a backup)"
            fixes.append((field, _raw(before), after))

    size_before = raw.get("Company_Size")
    if lead.company_size is None:
        fixes.append(("Company size", _raw(size_before),
                      f"Unknown; given the standard +{scoring['company_size']['missing_points']} points"))
    elif changed(size_before, str(lead.company_size)):
        note = " (middle of the range)" if "-" in str(size_before) else ""
        fixes.append(("Company size", _raw(size_before), f"{lead.company_size:,} employees{note}"))

    activity_before = raw.get("Recent_Activity")
    if lead.recent_activity is None:
        fixes.append(("Recent activity", _raw(activity_before), "None recorded (0 points)"))
    elif changed(activity_before, lead.recent_activity):
        fixes.append(("Recent activity", _raw(activity_before), lead.recent_activity))

    if "vip_referral" in lead.imputed_fields:
        fixes.append(("VIP referral", _raw(raw.get("VIP_Referral")), "No (it wasn't clearly marked yes)"))

    for flag in lead.flags:
        if flag.startswith("possible_duplicate_of:"):
            other = flag.split(":", 1)[1]
            fixes.append(("Duplicate check", "-", f"Looks identical to lead {other}. Kept, but worth a look"))
    return fixes


def rep_fixes(rep: Rep) -> list[str]:
    """Plain sentences for every real problem fixed in a rep's row (spelling clean-ups left out)."""
    raw = rep.raw or {}
    messages = {
        "negative_max_capacity_fixed": f"Capacity was {_raw(raw.get('Max_Capacity'))}; read as {rep.max_capacity} (a typo)",
        "max_capacity_defaulted": (f"Capacity was {_raw(raw.get('Max_Capacity'))}; used {rep.max_capacity}, "
                                   f"the usual for a {rep.seniority.value}"),
        "seniority_defaulted_junior": f"Seniority {_raw(raw.get('Seniority_Level'))} not recognized; treated as Junior (safest)",
        "expertise_unknown": "No industry specialty listed",
        "current_capacity_reset": f"Starting lead count was {_raw(raw.get('Current_Capacity'))}; reset to 0",
        "missing_region_or_language": "Region or language missing; only partial matches possible",
        "ambiguous_region": f"Region {_raw(raw.get('Region'))} named two places; used {rep.region}",
        "duplicate_id_renamed": f"ID {_raw(raw.get('Rep_ID'))} was used twice; renamed to {rep.id}",
    }
    return [messages[f] for f in rep.flags if f in messages]


def lead_tags(lead: Lead, match_level: MatchLevel | None) -> list[str]:
    """Short tags for the lead table's Notes column."""
    tags = []
    if lead.vip_referral:
        tags.append("VIP")
    if match_level is not None and match_level.traits_matched < 3:
        tags.append(f"Closest match ({match_level.traits_matched} of 3)")
    if lead.alt_regions:
        tags.append("Region unclear")
    if any(f.startswith("possible_duplicate_of:") or f == "duplicate_id_renamed" for f in lead.flags):
        tags.append("Possible duplicate")
    missing = [FIELD_LABELS.get(f, f).lower() for f in lead.imputed_fields if f != "lead_id"]
    if missing:
        tags.append("Missing " + _join(missing))
    return tags
