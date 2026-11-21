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


def _first_name(rep: Rep) -> str:
    return rep.name.split()[0] if rep.name else rep.id


# ======================================================================================
# Routing
# ======================================================================================
MATCH_HEADLINES = {
    3: "Perfect match: same region, language and industry.",
    2: "Close match (2 of 3 traits): no rep matching all 3 was free.",
    1: "Partial match (1 of 3 traits): no closer rep was free.",
}


def _trait_phrases(lead: Lead, rep: Rep) -> tuple[list[str], list[str]]:
    """Split the rep's traits into ones that match the lead and ones that don't."""
    same, different = [], []
    lead_regions = [r for r in (lead.region, *lead.alt_regions) if r]

    if rep.region and rep.region in lead_regions:
        same.append(f"covers {rep.region}")
    else:
        different.append(f"covers {rep.region or 'no listed region'}"
                         + (f" rather than {lead.region}" if lead.region else " (the lead's region is unknown)"))

    if rep.language and rep.language == lead.language:
        same.append(f"speaks {rep.language}")
    else:
        different.append(f"speaks {rep.language or 'no listed language'}"
                         + (f" rather than {lead.language}" if lead.language else " (the lead's language is unknown)"))

    if lead.industry is None and rep.industry is None:
        same.append("handles leads with no listed industry")
    elif rep.industry and rep.industry == lead.industry:
        same.append(f"specializes in {rep.industry}")
    elif lead.industry is None:
        different.append(f"specializes in {rep.industry} (the lead's industry is unknown)")
    elif rep.industry is None:
        different.append(f"has no listed industry specialty (the lead is in {lead.industry})")
    else:
        different.append(f"specializes in {rep.industry} rather than {lead.industry}")
    return same, different


def _seniority_note(lead: Lead, rep: Rep) -> str:
    if lead.priority is Priority.CRITICAL:
        if rep.seniority is Seniority.SENIOR:
            return "Urgent (Critical) leads go to Senior reps first."
        return "Urgent (Critical) lead: every matching Senior rep was full, so a Manager took it."
    if rep.seniority is Seniority.MANAGER:
        return "Matching Junior and Senior reps were full, so a Manager took it."
    return ""


def routing_sentence(lead: Lead, rep: Rep, match_level: MatchLevel) -> str:
    """e.g. 'Close match (2 of 3 traits): no rep matching all 3 was free. Sent to Lucas Ali,
    a Senior rep who covers Europe and speaks German, but specializes in Retail rather than Finance.
    Lucas now has 5 of 22 leads.'"""
    headline = MATCH_HEADLINES[match_level.traits_matched]
    if match_level is MatchLevel.ALTERNATE_REGION:
        headline = "Perfect match, using the second region listed for this lead."
    elif match_level is MatchLevel.INDUSTRY_UNKNOWN:
        headline = ("Industry unknown, so only region and language could match"
                    + (" (this rep has no listed industry either)." if rep.industry is None else "."))
    same, different = _trait_phrases(lead, rep)
    who = _join(same) if same else "was the closest available"
    if different:
        who += ", but " + _join(different)
    parts = [headline,
             f"Sent to {rep.name}, a {rep.seniority.value} rep who {who}.",
             _seniority_note(lead, rep),
             f"{_first_name(rep)} now has {rep.current_capacity} of {rep.max_capacity} leads."]
    return " ".join(p for p in parts if p)


def review_sentence(lead: Lead, reason: OverflowReason, min_traits: int = 1) -> str:
    """Why nobody got this lead, and what the manager should do.
    `min_traits` is the run's matching minimum: in Strict mode (3) "no match" means nobody shares
    all 3 traits, not that nobody shares any."""
    traits = " / ".join(v for v in (lead.region, lead.language, lead.industry) if v) or "nothing known"
    if reason is OverflowReason.NO_MATCH and min_traits > 1:
        return (f"No rep shares at least {min_traits} of the 3 traits with this lead ({traits}), "
                "the minimum for this run. A manager needs to pick someone or allow a closer match.")
    if reason is OverflowReason.NO_MATCH:
        return (f"No rep shares even one trait with this lead ({traits}). "
                "A manager needs to pick someone, or the team needs a rep for this area.")
    if reason is OverflowReason.ALL_AT_CAPACITY:
        return (f"Reps who could take this lead ({traits}) are all full. "
                "A manager needs to assign it or free up capacity.")
    if reason is OverflowReason.NO_SENIOR_AVAILABLE:
        return ("This is an urgent (Critical) lead, and urgent leads never go to Junior reps. "
                "The only reps who match are Juniors, so a manager needs to step in.")
    return "We don't know this lead's region, language or industry, so there was nothing to match on."


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


VIP_UNKNOWN_TAG = "VIP status unknown – confirm"


def lead_tags(lead: Lead, match_level: MatchLevel | None) -> list[str]:
    """Short tags for the lead table's Notes column."""
    tags = []
    if lead.vip_referral:
        tags.append("VIP")
    vip_unknown = "vip_unknown" in lead.flags
    if vip_unknown:
        tags.append(VIP_UNKNOWN_TAG)
    # (an unknown-industry match is not a "closest match": "Missing industry" below says why)
    if match_level not in (None, MatchLevel.INDUSTRY_UNKNOWN) and match_level.traits_matched < 3:
        tags.append(f"Closest match ({match_level.traits_matched} of 3)")
    if lead.alt_regions:
        tags.append("Region unclear")
    if any(f.startswith("possible_duplicate_of:") or f == "duplicate_id_renamed" for f in lead.flags):
        tags.append("Possible duplicate")
    skip = {"lead_id", "vip_referral"} if vip_unknown else {"lead_id"}   # VIP already has its own tag
    missing = [FIELD_LABELS.get(f, f).lower() for f in lead.imputed_fields if f not in skip]
    if missing:
        tags.append("Missing " + _join(missing))
    return tags
