"""
routing.py - STEP 3 (the core): give every lead to the right rep.

THE ALGORITHM IN PLAIN ENGLISH
==============================

Setup: put reps into "pools"
    A pool is every rep with the same (region, language, industry).
        ("Europe", "German", "Finance")  ->  [rep R-12, rep R-40, rep R-77, ...]
    Each rep is also put in an "any industry" pool for its region + language:
        ("Europe", "German", ANY)        ->  every Europe/German rep
    That second pool is ONLY used for leads whose industry is unknown.

Order: urgent leads first
    Leads are routed Critical -> High -> Medium -> Low (then highest score first),
    so Critical leads get first pick while capacity is still free.

For each lead:
    1. Decide which pools it may use, best match first (see `pools_to_try`):
           normal lead            -> its exact (region, language, industry) pool only
           industry unknown       -> reps with unknown expertise, then any industry
           region ambiguous       -> its first region, then its second region
           region/language missing-> nothing: straight to Manager Review
       Language is never relaxed: a rep must speak the lead's language.

    2. Decide which seniorities may take it, in groups (see `seniority_groups`):
           Critical  -> [Senior]  then  [Manager]          Juniors NEVER get Critical leads
           others    -> [Junior, Senior]  then  [Manager]  Managers last: their capacity is small

    3. Inside the first pool + group that has anyone free, pick the FAIREST rep:
           a. lowest load ratio (current / max). Ratio, not raw count, so a Junior with 50 slots
              and a Senior with 22 slots fill up at the same pace.
           b. tie? the rep who got a lead LONGEST AGO (round-robin rotation).
           c. still tied (nobody has had a lead yet)? the rep listed first in the file.

    4. Give the lead to that rep. If the rep is now full, remove them from their pools.

    5. If no pool/group had anyone free, send the lead to Manager Review with a reason:
           "No matching rep"                      nobody covers this combination at all
           "No Senior/Manager for Critical lead"  only Juniors match a Critical lead
           "All eligible reps at capacity"        the right reps exist but are all full
           "Missing region or language"           we couldn't even start

Why this is fast enough: a pool holds ~10-30 reps for 1,600 reps total, so step 3 looks at a
handful of reps per lead. 50,000 leads route in well under a second.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from .config import load_rules
from .models import (Assignment, AssignmentStatus, Lead, MatchLevel, OverflowReason, Priority,
                     Rep, RoutingResult, Seniority)
from .scoring import priority_order

ANY_INDUSTRY = "*"  # marker for the "any industry" pools

# A pool key: (region, language, industry). industry may also be None or ANY_INDUSTRY.
PoolKey = tuple[str, str, str | None]


class RepPools:
    """Keeps reps grouped by skills and remembers who was served when (for round-robin)."""

    def __init__(self, reps: list[Rep]):
        # pool key -> reps in that pool who still have free capacity
        self.open_reps: dict[PoolKey, list[Rep]] = defaultdict(list)
        # pool key -> how many reps of each seniority are in it at all (never changes).
        # Used only to explain WHY a lead overflowed.
        self.headcount: dict[PoolKey, Counter] = defaultdict(Counter)
        # rep id -> the pools that rep belongs to
        self.pools_of: dict[str, list[PoolKey]] = {}
        # rep id -> when they last got a lead (-1 = never). Bigger = more recent.
        self.last_served: dict[str, int] = {}
        # rep id -> position in the original file (final tiebreaker)
        self.file_position: dict[str, int] = {}
        self.clock = 0  # goes up by one with every assignment

        for position, rep in enumerate(reps):
            self.file_position[rep.id] = position
            self.last_served[rep.id] = -1
            if rep.region is None or rep.language is None:
                self.pools_of[rep.id] = []
                continue  # can't be matched to anyone (flagged "unroutable" during cleaning)

            exact_pool = (rep.region, rep.language, rep.industry)
            any_industry_pool = (rep.region, rep.language, ANY_INDUSTRY)
            self.pools_of[rep.id] = [exact_pool, any_industry_pool]
            for pool in (exact_pool, any_industry_pool):
                self.headcount[pool][rep.seniority] += 1
                if not rep.is_full:          # a rep with max capacity 0 never enters a pool
                    self.open_reps[pool].append(rep)

    def fairness_key(self, rep: Rep) -> tuple[float, int, int]:
        """Smaller = should get the next lead. (load ratio, last served, file position)"""
        return (rep.load_ratio, self.last_served[rep.id], self.file_position[rep.id])

    def pick_rep(self, pool: PoolKey, allowed: list[Seniority]) -> Rep | None:
        """The fairest free rep in this pool with one of the allowed seniorities, or None."""
        candidates = [rep for rep in self.open_reps.get(pool, []) if rep.seniority in allowed]
        if not candidates:
            return None
        return min(candidates, key=self.fairness_key)

    def assign(self, rep: Rep) -> None:
        """Record one more lead for this rep and take them out of the pools when full."""
        rep.current_capacity += 1
        self.clock += 1
        self.last_served[rep.id] = self.clock
        if rep.is_full:
            for pool in self.pools_of[rep.id]:
                self.open_reps[pool].remove(rep)

    def anyone_in(self, pools: list[PoolKey], seniorities: Iterable[Seniority]) -> bool:
        """Does at least one rep (full or not) with these seniorities exist in these pools?"""
        return any(self.headcount[pool][seniority] > 0 for pool in pools for seniority in seniorities)


# ======================================================================================
# Rules: which pools and which seniorities a lead may use
# ======================================================================================
def pools_to_try(lead: Lead, settings: dict) -> list[tuple[PoolKey, MatchLevel]]:
    """Pools this lead may use, best match first, each labelled with how good the match is."""
    regions = [(lead.region, MatchLevel.EXACT)]
    if settings.get("try_alternate_region_when_ambiguous", True):
        regions += [(other, MatchLevel.ALTERNATE_REGION) for other in lead.alt_regions]

    pools = []
    for region, level in regions:
        if lead.industry is not None:
            # Known industry: strict match only (the brief requires strict matching).
            pools.append(((region, lead.language, lead.industry), level))
        else:
            # Unknown industry: first reps whose expertise is also unknown...
            pools.append(((region, lead.language, None), level))
            # ...then any rep in the region + language.
            if settings.get("relax_industry_when_unknown", True):
                pools.append(((region, lead.language, ANY_INDUSTRY), MatchLevel.RELAXED_INDUSTRY))
    return pools


def seniority_groups(priority: Priority, settings: dict) -> list[list[Seniority]]:
    """Which seniorities may take a lead of this priority, as groups tried in order."""
    order = settings["seniority_order"]
    groups = order.get(priority.value, order["default"])
    groups = [[Seniority(name) for name in group] for group in groups]

    if priority is Priority.CRITICAL:
        # Hard safety rule. Even if someone edits rules.yaml, Juniors never get Critical leads.
        groups = [[s for s in group if s is not Seniority.JUNIOR] for group in groups]
    return [group for group in groups if group]


# ======================================================================================
# Routing
# ======================================================================================
def route_lead(lead: Lead, pools: RepPools, settings: dict) -> Assignment:
    """Find a rep for one lead, or send it to Manager Review with a reason."""
    if lead.region is None or lead.language is None:
        missing = "region" if lead.region is None else "language"
        return manager_review(lead, OverflowReason.MISSING_ROUTING_DATA, f"Cannot route: missing {missing}")

    candidate_pools = pools_to_try(lead, settings)
    groups = seniority_groups(lead.priority, settings)

    # Best match first; within a match, preferred seniority first.
    for pool, match_level in candidate_pools:
        for allowed in groups:
            rep = pools.pick_rep(pool, allowed)
            if rep is None:
                continue
            pools.assign(rep)
            return assigned(lead, rep, pool, match_level)

    # Nobody was free. Work out the most useful reason for the manager.
    pool_keys = [pool for pool, _ in candidate_pools]
    allowed_seniorities = {s for group in groups for s in group}
    if not pools.anyone_in(pool_keys, Seniority):
        reason = OverflowReason.NO_MATCH
    elif not pools.anyone_in(pool_keys, allowed_seniorities):
        reason = OverflowReason.NO_SENIOR_AVAILABLE
    else:
        reason = OverflowReason.ALL_AT_CAPACITY
    tried = ", ".join(describe_pool(pool) for pool in pool_keys)
    return manager_review(lead, reason, f"{reason.value} (tried {tried})")


def route_all(leads: Iterable[Lead], reps: list[Rep]) -> RoutingResult:
    """Route every lead, most urgent first.

    Every lead comes back exactly once: either Assigned or in Manager Review.
    Note: this updates rep.current_capacity on the rep objects you pass in.
    """
    settings = load_rules()["routing"]
    pools = RepPools(reps)
    assignments = [route_lead(lead, pools, settings) for lead in sorted(leads, key=priority_order)]
    overflow = [a for a in assignments if a.status is AssignmentStatus.MANAGER_REVIEW]
    return RoutingResult(assignments=assignments, overflow=overflow, reps=reps)


# ======================================================================================
# Building the Assignment records
# ======================================================================================
def describe_pool(pool: PoolKey) -> str:
    region, language, industry = pool
    if industry == ANY_INDUSTRY:
        industry_text = "any industry"
    else:
        industry_text = industry or "unknown industry"
    return f"{region}/{language}/{industry_text}"


def assigned(lead: Lead, rep: Rep, pool: PoolKey, match_level: MatchLevel) -> Assignment:
    note = "" if match_level is MatchLevel.EXACT else f" [{match_level.value}]"
    reason = (f"{describe_pool(pool)} -> {rep.seniority.value} {rep.name} "
              f"(load {rep.current_capacity}/{rep.max_capacity}){note}")
    return Assignment(lead_id=lead.id, rep_id=rep.id, priority=lead.priority, score=lead.score,
                      reason=reason, status=AssignmentStatus.ASSIGNED, match_level=match_level,
                      rep_name=rep.name, rep_seniority=rep.seniority)


def manager_review(lead: Lead, reason: OverflowReason, text: str) -> Assignment:
    return Assignment(lead_id=lead.id, rep_id=None, priority=lead.priority, score=lead.score,
                      reason=text, status=AssignmentStatus.MANAGER_REVIEW, overflow_reason=reason)
