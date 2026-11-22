"""
review.py - STEP 3b: a suggested next step for every lead in Manager Review.

Routing (routing.py) sends a lead to Manager Review when no suitable rep was free. A manager
then has to decide what to do. This file runs ONCE, after every lead has been placed, and looks
at the capacity that is really left, so each suggestion can actually be carried out.

Leads are handled most urgent first (Critical, then highest score). For each one we try, in order:

    1. Nothing known        region, language and industry are all unknown
                            -> "Fill in region, language and industry for this lead, then re-run."

    2. Someone has room     a rep of an ALLOWED seniority (never a Junior for a Critical lead) who
                            shares the most traits and still has a free slot
                            -> "Reassign to Ann Lee (Senior, shares region + language, 3 open slots)."
                            If that is fewer traits than this run allows (e.g. Strict mode needs 3),
                            the sentence says it needs a policy exception.
                            The slot is RESERVED, so two review leads are never promised the
                            same last slot.

    3. Nobody covers it     no rep at all shares even one trait (e.g. nobody covers Asia-Pacific
                            or speaks Spanish) -> coverage is missing: hire or assign a rep for it.
                            If a Manager has room, they are named as a stop-gap (and reserved).

    4. Only Juniors match   a Critical lead whose only free match is a Junior
                            -> "Needs a Senior: raise <closest Senior/Manager>'s capacity or
                            approve an exception." The Junior is never suggested.

    5. Everyone is full     -> "Team at capacity: raise <closest full rep>'s max capacity or hold
                            for the next cycle."

`suggested_rep_id` is the rep the sentence names as the future owner (None when no rep can be
named). It is never a Junior for a Critical lead.

Speed: reps are grouped by (region, language, industry, seniority), so a lead compares itself
with ~150 groups instead of 1,600 reps, and leads with the same traits share that work.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .config import load_rules
from .models import Assignment, AssignmentStatus, Lead, MatchLevel, PipelineResult, Priority, Rep, Seniority
from .scoring import priority_order

TRAITS = ("region", "language", "industry")


def shared_traits(lead: Lead, rep: Rep, use_alt_regions: bool = True) -> tuple[str, ...]:
    """Which of the three traits this rep has in common with the lead (only KNOWN values count).
    The lead's second region (from a value like "ASIA PACIFICEurope") counts as its region too,
    unless rules.yaml turns that off (routing.try_alternate_region_when_ambiguous: false)."""
    shared = []
    lead_regions = (lead.region, *lead.alt_regions) if use_alt_regions else (lead.region,)
    if rep.region and lead.region and rep.region in lead_regions:
        shared.append("region")
    if rep.language and rep.language == lead.language:
        shared.append("language")
    if rep.industry and rep.industry == lead.industry:
        shared.append("industry")
    return tuple(shared)


def _traits_text(shared: tuple[str, ...]) -> str:
    return " + ".join(shared) if shared else "no trait"


@dataclass(slots=True)
class _Group:
    """Reps with identical traits and seniority. They are interchangeable for a suggestion."""
    seniority: Seniority
    sample: Rep                                  # any member: used to compare traits
    reps: list[Rep] = field(default_factory=list)
    free: int = 0                                # open slots left in the whole group (skip it at 0)


class _Capacity:
    """Remaining capacity after routing, minus the slots we have already promised."""

    def __init__(self, reps: list[Rep], groups: Iterable[_Group]):
        self.reserved: dict[int, int] = defaultdict(int)     # id(rep) -> slots promised
        self.position = {id(rep): i for i, rep in enumerate(reps)}
        self.group_of: dict[int, _Group] = {}
        for group in groups:
            group.free = sum(rep.available for rep in group.reps)
            for rep in group.reps:
                self.group_of[id(rep)] = group

    def free(self, rep: Rep) -> int:
        return max(rep.available - self.reserved[id(rep)], 0)

    def load(self, rep: Rep) -> float:
        """How full the rep would be counting promised slots (0.0 empty .. 1.0 full)."""
        if rep.max_capacity <= 0:
            return 1.0
        return (rep.current_capacity + self.reserved[id(rep)]) / rep.max_capacity

    def reserve(self, rep: Rep) -> None:
        self.reserved[id(rep)] += 1
        self.group_of[id(rep)].free -= 1


def suggest_actions(review: list[Assignment], leads: Iterable[Lead], reps: list[Rep],
                    settings: dict, allowed_for: Callable[[Priority], list[list[Seniority]]]) -> None:
    """Fill in suggested_action / suggested_rep_id on every Manager Review assignment.

    `allowed_for(priority)` gives the seniority groups that may take a lead of that priority,
    in preference order (routing.seniority_groups), so the rules stay in one place.
    """
    if not review:
        return
    lead_by_id = {lead.id: lead for lead in leads}
    min_traits = settings.get("min_traits_to_match", 3)
    relax_unknown_industry = settings.get("relax_industry_when_unknown", True)
    use_alt_regions = settings.get("try_alternate_region_when_ambiguous", True)

    # Group reps by traits + seniority (file order kept inside each group).
    groups: dict[tuple, _Group] = {}
    for rep in reps:
        key = (rep.region, rep.language, rep.industry, rep.seniority)
        if key not in groups:
            groups[key] = _Group(rep.seniority, rep)
        groups[key].reps.append(rep)
    capacity = _Capacity(reps, groups.values())

    # For each distinct set of lead traits: every group, best trait overlap first.
    ranked_cache: dict[tuple, list[tuple[tuple[str, ...], _Group]]] = {}

    def ranked(lead: Lead) -> list[tuple[tuple[str, ...], _Group]]:
        key = (lead.region, lead.alt_regions, lead.language, lead.industry)
        if key not in ranked_cache:
            pairs = [(shared_traits(lead, g.sample, use_alt_regions), g) for g in groups.values()]
            ranked_cache[key] = sorted(pairs, key=lambda pair: -len(pair[0]))   # stable: file order
        return ranked_cache[key]

    for assignment in sorted(review, key=lambda a: priority_order(lead_by_id[a.lead_id])):
        lead = lead_by_id[assignment.lead_id]
        action, rep = _suggest(lead, ranked(lead), allowed_for(lead.priority), capacity,
                               min_traits, relax_unknown_industry)
        assignment.suggested_action = action
        assignment.suggested_rep_id = rep.id if rep else None


def _suggest(lead: Lead, ranked: list[tuple[tuple[str, ...], _Group]], seniority_groups: list[list[Seniority]],
             capacity: _Capacity, min_traits: int, relax_unknown_industry: bool) -> tuple[str, Rep | None]:
    """The suggestion for one lead: (sentence, rep it names or None)."""
    missing = [trait for trait in TRAITS if getattr(lead, trait) is None]

    # 1. Nothing to match on.
    if len(missing) == len(TRAITS):
        return "Fill in region, language and industry for this lead, then re-run.", None
    hint = (f" Its {_join(missing)} {'is' if len(missing) == 1 else 'are'} unknown: "
            "filling that in may find a closer rep.") if missing else ""

    # Preference rank of each allowed seniority (e.g. Critical: Senior 0, Manager 1).
    rank_of = {s: i for i, group in enumerate(seniority_groups) for s in group}
    is_critical = lead.priority is Priority.CRITICAL
    # Fewest shared traits this run accepts for this lead (an unknown industry may be relaxed).
    required = 2 if (lead.industry is None and relax_unknown_industry) else min_traits

    def exception_note(shared) -> str:
        """Below the run's matching minimum, more capacity alone won't help: say so."""
        return (f" Needs a policy exception: this run only allows matches on {min_traits} of 3 traits."
                if len(shared) < required else "")

    # 2. An allowed rep with a free slot, most traits in common first.
    best: tuple | None = None
    for shared, group in ranked:
        if not shared or (best and len(shared) < len(best[1])):
            break                                  # sorted by overlap: nothing better follows
        if group.seniority not in rank_of or not group.free:
            continue
        for rep in group.reps:
            if capacity.free(rep):
                key = (rank_of[group.seniority], capacity.load(rep), capacity.position[id(rep)])
                if best is None or key < best[0]:
                    best = (key, shared, rep)
    if best:
        _, shared, rep = best
        open_slots = capacity.free(rep)
        capacity.reserve(rep)
        text = (f"Reassign to {rep.name} ({rep.seniority.value}, shares {_traits_text(shared)}, "
                f"{open_slots} open slot{'s' if open_slots != 1 else ''}).")
        return text + exception_note(shared) + hint, rep

    # 3. Nobody on the whole team shares a single trait: coverage is missing.
    if not ranked or not ranked[0][0]:
        known = [str(getattr(lead, trait)) for trait in TRAITS if getattr(lead, trait)]
        text = (f"Coverage missing: no rep covers {_join(known, 'or')}. "
                "Hire or assign a rep for this area.")
        manager = _roomiest(ranked, capacity, {Seniority.MANAGER} & set(rank_of))
        if manager:
            open_slots = capacity.free(manager)
            capacity.reserve(manager)
            text += (f" Meanwhile {manager.name} (Manager, {open_slots} open "
                     f"slot{'s' if open_slots != 1 else ''}) could take it.")
            return text + hint, manager
        return text + hint, None

    # 4. Critical lead: the only free match is a Junior (or only Juniors match at all).
    junior_has_room = any(shared and group.seniority is Seniority.JUNIOR and group.free
                          for shared, group in ranked)
    allowed_match = any(shared and group.seniority in rank_of for shared, group in ranked)
    if is_critical and (junior_has_room or not allowed_match):
        closest = _closest(ranked, capacity, set(rank_of))
        if closest is None:
            return ("Needs a Senior: only Juniors match and the team has no Senior or Manager. "
                    "Hire one or approve an exception." + hint), None
        rep, shared = closest
        open_slots = capacity.free(rep)
        if open_slots:   # they have room but match too little: only an exception helps
            capacity.reserve(rep)
            return (f"Needs a Senior: only Juniors match this urgent lead. Approve an exception to "
                    f"give it to {rep.name} ({rep.seniority.value}, shares {_traits_text(shared)}, "
                    f"{open_slots} open slot{'s' if open_slots != 1 else ''})." + hint), rep
        return (f"Needs a Senior: raise {rep.name}'s capacity ({rep.seniority.value}, shares "
                f"{_traits_text(shared)}, {rep.current_capacity} of {rep.max_capacity} leads) "
                "or approve an exception. Urgent leads never go to Juniors." + hint), rep

    # 5. Everyone who could take it is full.
    closest = _closest(ranked, capacity, set(rank_of))
    if closest is None:   # cannot happen (step 3/4 cover it), but never crash on odd rules
        return "Team at capacity: hold this lead for the next cycle." + hint, None
    rep, shared = closest
    return (f"Team at capacity: raise {rep.name}'s max capacity ({rep.seniority.value}, shares "
            f"{_traits_text(shared)}, {rep.current_capacity} of {rep.max_capacity} leads) "
            "or hold for the next cycle." + exception_note(shared) + hint), rep


def _closest(ranked, capacity: _Capacity, allowed: set[Seniority]) -> tuple[Rep, tuple[str, ...]] | None:
    """The allowed rep sharing the most traits (full or not); ties: most free slots, then most
    capacity, then file order."""
    best = None
    for shared, group in ranked:
        if best and len(shared) < len(best[2]):
            break
        if group.seniority not in allowed:
            continue
        for rep in group.reps:
            key = (-capacity.free(rep), -rep.max_capacity, capacity.position[id(rep)])
            if best is None or key < best[0]:
                best = (key, rep, shared)
    return (best[1], best[2]) if best else None


def _roomiest(ranked, capacity: _Capacity, allowed: set[Seniority]) -> Rep | None:
    """Among allowed reps, the one with the most free slots (ties: file order), or None."""
    best = None
    for _, group in ranked:
        if group.seniority not in allowed or not group.free:
            continue
        for rep in group.reps:
            free = capacity.free(rep)
            if free and (best is None or (-free, capacity.position[id(rep)]) < best[0]):
                best = ((-free, capacity.position[id(rep)]), rep)
    return best[1] if best else None


def _join(items: list[str], word: str = "and") -> str:
    """['a'] -> 'a';  ['a','b'] -> 'a and b';  ['a','b','c'] -> 'a, b and c'"""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]


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


def candidate_traits(lead: Lead, rep: Rep) -> tuple[tuple[str, ...], tuple[str, ...]]:
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
    from .routing import seniority_groups
    groups = seniority_groups(priority, load_rules()["routing"])
    return [s for group in groups for s in group]


def rep_choices(lead: Lead, reps: list[Rep]) -> list[Candidate]:
    """Every rep the manager MAY pick for this lead (has room, allowed seniority), closest first."""
    allowed = allowed_seniorities(lead.priority)
    preference = {s: i for i, s in enumerate(allowed)}
    # Seniorities in the same routing group (e.g. Junior and Senior for a High lead) are equally preferred.
    from .routing import seniority_groups
    groups = seniority_groups(lead.priority, load_rules()["routing"])
    group_of = {s: i for i, group in enumerate(groups) for s in group}
    position = {rep.id: i for i, rep in enumerate(reps)}
    candidates = []
    for rep in reps:
        if rep.available <= 0 or rep.seniority not in preference:
            continue
        matched, missed = candidate_traits(lead, rep)
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

    candidate = Candidate(rep, *candidate_traits(lead, rep))
    level = candidate.match_level
    if level is MatchLevel.EXACT and lead.region != rep.region:
        level = MatchLevel.ALTERNATE_REGION
    elif lead.industry is None and candidate.matched == ("region", "language"):
        level = MatchLevel.INDUSTRY_UNKNOWN
    rep.current_capacity += 1
    shared = " and ".join(candidate.matched) if candidate.matched else "none of the lead's traits"
    decision = Assignment(
        lead_id=lead.id, rep_id=rep.id, priority=lead.priority, score=lead.score,
        reason=(f"Assigned by a manager to {rep.name} ({rep.seniority.value}), who shares {shared} "
                f"({candidate.traits} of 3). It had been waiting because: {waiting.overflow_reason.value.lower()}."
                if waiting.overflow_reason else
                f"Assigned by a manager to {rep.name} ({rep.seniority.value}), who shares {shared}."),
        status=AssignmentStatus.ASSIGNED, match_level=level,
        rep_name=rep.name, rep_seniority=rep.seniority, manual=True,
    )
    result.assignments[index] = decision
    result.manager_review = [a for a in result.manager_review if a.lead_id != lead_id]
    from .routing import matching_settings, seniority_groups
    settings = matching_settings(result.metrics.get("min_traits", 1))
    suggest_actions(result.manager_review, result.leads, result.reps, settings,
                    lambda priority: seniority_groups(priority, settings))
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
