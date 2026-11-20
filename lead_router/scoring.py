"""
scoring.py - STEP 2: give every lead a score and a priority tier.

    VIP referral?  -> 100 points, Critical. Nothing else is checked.

    Otherwise:
        score = company size points      Enterprise 1,000+ = 50 | Mid-Market 100-999 = 30 | SMB = 0
                                          unknown size = 20 (cheat-sheet fallback)
              + recent activity points    Live Demo / Contact Sales = 35 | Webinar = 25
                                          Whitepaper = 15 | Pricing Page = 5 | anything else = 0
              + industry points           Technology = 15 | Healthcare, Finance = 10
                                          Manufacturing, Retail = 0 | unknown = 5 (neutral)

    Tier:  Critical 85+  |  High 60-84  |  Medium 40-59  |  Low 0-39

All numbers come from config/rules.yaml. Each point added is saved with a label, e.g.
("Enterprise (1,000+): 3,258 employees", 50), so a manager can see exactly why a lead scored
the way it did.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import load_rules
from .models import Lead, Priority


@dataclass(frozen=True, slots=True)
class ScoreResult:
    points: int
    priority: Priority
    reasons: list[tuple[str, int]]   # [(label, points), ...]
    explanation: str                 # one-line summary for the dashboard


def tier_for(points: int, tiers: dict[str, int] | None = None) -> Priority:
    """Turn a score into a tier. Thresholds are checked from highest to lowest."""
    tiers = tiers or load_rules()["scoring"]["tiers"]
    for tier_name, minimum_points in sorted(tiers.items(), key=lambda item: item[1], reverse=True):
        if points >= minimum_points:
            return Priority(tier_name)
    return Priority.LOW


def score_lead(lead: Lead, rules: dict[str, Any] | None = None) -> ScoreResult:
    """Score one lead. Missing data gets fallback points, so it never crashes or gets dropped."""
    rules = rules or load_rules()["scoring"]

    # --- VIP override: skip the matrix completely.
    if lead.vip_referral:
        points = int(rules["vip_score"])
        return ScoreResult(points, Priority.CRITICAL, [("VIP referral: automatically Critical", points)],
                           f"VIP referral, so it's automatically Critical ({points} points)")

    reasons = [
        _company_size_points(lead, rules["company_size"]),
        _activity_points(lead, rules["recent_activity"]),
        _industry_points(lead, rules["industry"]),
    ]
    total = sum(points for _, points in reasons)
    priority = tier_for(total, rules["tiers"])
    breakdown = "; ".join(f"{label} {points:+d}" for label, points in reasons)
    return ScoreResult(total, priority, reasons, f"{total} points = {priority.value} priority. {breakdown}")


def _company_size_points(lead: Lead, rules: dict) -> tuple[str, int]:
    if lead.company_size is None:
        return "Company size unknown (standard points)", int(rules["missing_points"])
    # Bands are listed biggest first, so the first band the size reaches is the right one.
    for band in rules["bands"]:
        if lead.company_size >= band["min"]:
            return f"{band['name']}: {lead.company_size:,} employees", int(band["points"])
    return f"{lead.company_size:,} employees", 0


def _activity_points(lead: Lead, rules: dict) -> tuple[str, int]:
    points = rules["points"].get(lead.recent_activity) if lead.recent_activity else None
    if points is None:
        label = lead.recent_activity or "No recent activity"
        return f"{label} (earns no points)", int(rules["default_points"])
    return lead.recent_activity, int(points)


def _industry_points(lead: Lead, rules: dict) -> tuple[str, int]:
    if lead.industry is None:
        return "Industry unknown (neutral points)", int(rules["missing_points"])
    return f"{lead.industry} industry", int(rules["points"].get(lead.industry, 0))


def priority_order(lead: Lead) -> tuple[int, int, int]:
    """Sort key used everywhere: Critical first, then highest score, then original file order."""
    return (lead.priority.rank, -lead.score, lead.row_number)


def score_all(leads: list[Lead]) -> list[Lead]:
    """Score every lead (stored on the lead itself) and return them in priority order."""
    rules = load_rules()["scoring"]
    for lead in leads:
        result = score_lead(lead, rules)
        lead.score = result.points
        lead.priority = result.priority
        lead.score_reasons = result.reasons
        lead.explanation = result.explanation
    return sorted(leads, key=priority_order)
