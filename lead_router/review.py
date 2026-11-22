"""
review.py - the Manager Review workflow: suggest reps for a waiting lead, and assign it by hand.

A lead lands in Manager Review when no allowed rep had room (see routing.py). A manager then:
    1. looks at the closest reps who still have room      -> closest_reps()
    2. assigns the lead to one of them (or any other rep)  -> manual_assign()

The same safety rules as automatic routing still apply to a manager's choice:
    - a Critical lead never goes to a Junior
    - no rep goes over their Max_Capacity
Matching is NOT enforced: a manager may knowingly pick a rep who shares fewer traits.

How "closest" is ranked (best first):
    1. most traits in common with the lead (region, language, industry: 3, 2, 1 or 0)
    2. preferred seniority for the lead's tier (Critical: Senior before Manager;
       others: Junior/Senior before Manager, to save scarce Manager capacity)
    3. least full relative to their own capacity (the same fair-share idea as routing)
    4. position in the reps file (so the list is always the same for the same data)
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import load_rules
from .models import (Assignment, AssignmentStatus, Lead, MatchLevel, PipelineResult, Priority, Rep,
                     Seniority)
from .routing import seniority_groups

TRAITS = ("region", "language", "industry")

# which traits matched -> the match level shown everywhere else on the dashboard
_LEVEL = {
    frozenset(TRAITS): MatchLevel.EXACT,
    frozenset({"region", "language"}): MatchLevel.RELAXED_INDUSTRY,
    frozenset({"language", "industry"}): MatchLevel.RELAXED_REGION,
    frozenset({"region", "industry"}): MatchLevel.RELAXED_LANGUAGE,
    frozenset({"language"}): MatchLevel.ONLY_LANGUAGE,
    frozenset({"region"}): MatchLevel.ONLY_REGION,
    frozenset({"industry"}): MatchLevel.ONLY_INDUSTRY,
}


class ReviewError(Exception):
    """A manual assignment that breaks a rule. The message is shown to the manager as is."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One suggested rep for a waiting lead."""
    rep: Rep
    matched: tuple[str, ...]     # traits the rep shares with the lead, e.g. ("region", "language")
    missed: tuple[str, ...]      # traits that differ (an unknown lead trait is in neither list)

    @property
    def traits(self) -> int:
        return len(self.matched)

    @property
    def match_level(self) -> MatchLevel | None:
        return _LEVEL.get(frozenset(self.matched))


def shared_traits(lead: Lead, rep: Rep) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(traits in common, traits that differ). A trait the lead doesn't have (unknown) is in neither."""
    lead_regions = [r for r in (lead.region, *lead.alt_regions) if r]
    checks = {
        "region": (bool(lead_regions), bool(rep.region and rep.region in lead_regions)),
        "language": (lead.language is not None, bool(rep.language and rep.language == lead.language)),
        "industry": (lead.industry is not None, bool(rep.industry and rep.industry == lead.industry)),
    }
    matched = tuple(t for t in TRAITS if checks[t][1])
    missed = tuple(t for t in TRAITS if checks[t][0] and not checks[t][1])
    return matched, missed


def allowed_seniorities(priority: Priority) -> list[Seniority]:
    """Seniorities that may take this lead, most preferred first (Juniors never for Critical)."""
    groups = seniority_groups(priority, load_rules()["routing"])
    return [s for group in groups for s in group]


def rep_choices(lead: Lead, reps: list[Rep]) -> list[Candidate]:
    """Every rep the manager MAY pick for this lead (has room, allowed seniority), closest first."""
    allowed = allowed_seniorities(lead.priority)
    preference = {s: i for i, s in enumerate(allowed)}
    # Seniorities in the same routing group (e.g. Junior and Senior for a High lead) are equally preferred.
    groups = seniority_groups(lead.priority, load_rules()["routing"])
    group_of = {s: i for i, group in enumerate(groups) for s in group}
    position = {rep.id: i for i, rep in enumerate(reps)}
    candidates = []
    for rep in reps:
        if rep.available <= 0 or rep.seniority not in preference:
            continue
        matched, missed = shared_traits(lead, rep)
        candidates.append(Candidate(rep, matched, missed))
    candidates.sort(key=lambda c: (-c.traits, group_of[c.rep.seniority], c.rep.load_ratio, position[c.rep.id]))
    return candidates


def closest_reps(lead: Lead, reps: list[Rep], n: int = 3) -> list[Candidate]:
    """The top `n` reps a manager should consider for this lead."""
    return rep_choices(lead, reps)[:n]


def manual_assign(result: PipelineResult, lead_id: str, rep_id: str) -> Assignment:
    """Give a lead waiting in Manager Review to a rep chosen by a manager.

    Updates the result in place: the lead's decision, the rep's load, the review list and the metrics.
    Raises ReviewError (with a message for the manager) if the choice breaks a rule.
    """
    index = next((i for i, a in enumerate(result.assignments) if a.lead_id == lead_id), None)
    if index is None:
        raise ReviewError(f"Lead {lead_id} isn't in this run.")
    waiting = result.assignments[index]
    if waiting.status is not AssignmentStatus.MANAGER_REVIEW:
        raise ReviewError(f"Lead {lead_id} isn't waiting for review: it's already with {waiting.rep_name}.")
    rep = next((r for r in result.reps if r.id == rep_id), None)
    if rep is None:
        raise ReviewError(f"Rep {rep_id} isn't on the team.")
    lead = next(l for l in result.leads if l.id == lead_id)
    if lead.priority is Priority.CRITICAL and rep.seniority is Seniority.JUNIOR:
        raise ReviewError(f"{rep.name} is a Junior rep, and Juniors can't take Critical leads. "
                          "Pick a Senior or a Manager.")
    if rep.is_full:
        raise ReviewError(f"{rep.name} is full ({rep.current_capacity} of {rep.max_capacity} leads). "
                          "Pick a rep with open slots.")

    candidate = Candidate(rep, *shared_traits(lead, rep))
    rep.current_capacity += 1
    shared = " and ".join(candidate.matched) if candidate.matched else "none of the lead's traits"
    decision = Assignment(
        lead_id=lead.id, rep_id=rep.id, priority=lead.priority, score=lead.score,
        reason=(f"Assigned by a manager to {rep.name} ({rep.seniority.value}), who shares {shared} "
                f"({candidate.traits} of 3). It had been waiting because: {waiting.overflow_reason.value.lower()}."
                if waiting.overflow_reason else
                f"Assigned by a manager to {rep.name} ({rep.seniority.value}), who shares {shared}."),
        status=AssignmentStatus.ASSIGNED, match_level=candidate.match_level,
        rep_name=rep.name, rep_seniority=rep.seniority, manual=True,
    )
    result.assignments[index] = decision
    result.manager_review = [a for a in result.manager_review if a.lead_id != lead_id]
    _refresh_metrics(result)
    return decision


def _refresh_metrics(result: PipelineResult) -> None:
    """Recount every dashboard number after a manual change (keeping run-only numbers like speed)."""
    from .pipeline import compute_metrics  # here, not at the top: pipeline imports this module's neighbours
    seconds = result.timings.get("total", 0) - result.timings.get("metrics", 0)
    fresh = compute_metrics(result.leads, result.reps, result.assignments, seconds)
    fresh["throughput_lps"] = result.metrics.get("throughput_lps", fresh["throughput_lps"])
    result.metrics.update(fresh)
    result.metrics["manual_assignments"] = sum(1 for a in result.assignments if a.manual)
