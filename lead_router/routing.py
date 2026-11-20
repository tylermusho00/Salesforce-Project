"""
routing.py - STEP 3 (the core): give every lead to the right rep.

THE ALGORITHM IN PLAIN ENGLISH
==============================

Setup: put reps into "pools"
    A pool is every rep sharing the same traits. Each rep sits in 7 pools:
        ("Europe", "German", "Finance")   all 3 traits          -> 3-of-3 matches
        ("Europe", "German", ANY)         region + language     -> 2-of-3 matches
        (ANY,      "German", "Finance")   language + industry   -> 2-of-3 matches
        ("Europe", ANY,      "Finance")   region + industry     -> 2-of-3 matches
        (ANY,      "German", ANY)         language only         -> 1-of-3 matches
        ("Europe", ANY,      ANY)         region only           -> 1-of-3 matches
        (ANY,      ANY,      "Finance")   industry only         -> 1-of-3 matches

Order: urgent leads first
    Leads are routed Critical -> High -> Medium -> Low (then highest score first),
    so Critical leads get first pick while capacity is still free.

For each lead:
    1. Decide which pools it may use, MOST TRAITS IN COMMON FIRST (see `pools_to_try`):
           3 of 3   same region, language and industry (also tries the 2nd region of an
                    ambiguous value like "ASIA PACIFICEurope")
           2 of 3   only if nobody with 3 of 3 is free. Which trait gives way first is set in
                    rules.yaml (routing.relax_order); default: industry, then region, then language
                    (a rep who speaks the lead's language is the most useful partial match).
           1 of 3   last resort, only if nobody with 2 of 3 is free. Keeps the most useful trait
                    first: same language, then same region, then same industry.
       routing.min_traits_to_match in rules.yaml sets the lowest tier allowed (3 = strict).
       A lead with no usable traits at all goes straight to Manager Review.

    2. Decide which seniorities may take it, in groups (see `seniority_groups`):
           Critical  -> [Senior]  then  [Manager]          Juniors NEVER get Critical leads
           others    -> [Junior, Senior]  then  [Manager]  Managers last: their capacity is small

    3. Inside the first pool + group that has anyone free, pick the FAIREST rep using the
       distribution strategy from distribution.py (default: capacity-weighted fair round-robin):
           a. smallest "virtual finish time" = (leads held + 1) / max capacity, so a Junior with
              50 slots gets ~2.5x the leads of a Senior with 20 and both fill at the same pace.
           b. tie? the rep who has received the least lead VALUE (total score) per slot, so the
              best leads are shared too, not just the lead count.
           c. still tied? the rep who got a lead LONGEST AGO (round-robin rotation).
           d. still tied (nobody has had a lead yet)? the rep listed first in the file.

    4. Give the lead to that rep. If the rep is now full, remove them from their pools.

    5. If no pool/group had anyone free, send the lead to Manager Review with a reason:
           "No rep shares a trait"                nobody shares even 1 of the lead's 3 traits
           "Urgent lead, only Juniors match"      only Juniors match a Critical lead
           "Matching reps are all full"           the right reps exist but are all full
           "Not enough info to match"             region, language and industry all unknown
       Every decision also gets a plain-English sentence (see explain.py).

Why this is fast enough: a pool holds ~10-30 reps for 1,600 reps total, so step 3 looks at a
handful of reps per lead. 50,000 leads route in well under a second.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from .config import load_rules
from .distribution import distribution_fairness, get_strategy
from .explain import review_sentence, routing_sentence
from .models import (Assignment, AssignmentStatus, Lead, MatchLevel, OverflowReason, Priority,
                     Rep, RoutingResult, Seniority)
from .scoring import priority_order

ANY = "*"            # marker for "this trait doesn't have to match" in a pool key
ANY_INDUSTRY = ANY   # (old name, kept for readability in a few places)

# A pool key: (region, language, industry). Any position may be ANY; industry may also be None
# (None = rep's expertise is unknown: they get leads whose industry is also unknown).
PoolKey = tuple[str, str, str | None]


def pools_for_rep(rep: Rep) -> list[PoolKey]:
    """Every pool a rep belongs to: all 3 traits, each pair of known traits, each single trait."""
    region, language, industry = rep.region, rep.language, rep.industry
    pools = []
    if region and language:
        pools.append((region, language, industry))   # all 3 traits (industry None = generalist)
        pools.append((region, language, ANY))        # region + language
    if language and industry:
        pools.append((ANY, language, industry))      # language + industry
    if region and industry:
        pools.append((region, ANY, industry))        # region + industry
    if language:
        pools.append((ANY, language, ANY))           # language only
    if region:
        pools.append((region, ANY, ANY))             # region only
    if industry:
        pools.append((ANY, ANY, industry))           # industry only
    return pools


class RepPools:
    """Keeps reps grouped by skills and remembers who was served when (for round-robin)."""

    def __init__(self, reps: list[Rep], strategy: str | None = None):
        # how to choose between equally qualified reps (see distribution.py)
        self.sort_key = get_strategy(strategy)
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
        # rep id -> total score of the leads they hold (for value fairness)
        self.value_received: dict[str, float] = {}
        self.clock = 0  # goes up by one with every assignment

        for position, rep in enumerate(reps):
            self.file_position[rep.id] = position
            self.last_served[rep.id] = -1
            self.value_received[rep.id] = 0.0
            self.pools_of[rep.id] = pools_for_rep(rep)
            for pool in self.pools_of[rep.id]:
                self.headcount[pool][rep.seniority] += 1
                if not rep.is_full:          # a rep with max capacity 0 never enters a pool
                    self.open_reps[pool].append(rep)

    def fairness_key(self, rep: Rep) -> tuple:
        """Smaller = should get the next lead. The strategy decides what "fair" means."""
        return self.sort_key(rep, self.value_received[rep.id], self.last_served[rep.id],
                             self.file_position[rep.id])

    def pick_rep(self, pool: PoolKey, allowed: list[Seniority]) -> Rep | None:
        """The fairest free rep in this pool with one of the allowed seniorities, or None."""
        candidates = [rep for rep in self.open_reps.get(pool, []) if rep.seniority in allowed]
        if not candidates:
            return None
        return min(candidates, key=self.fairness_key)

    def assign(self, rep: Rep, lead_score: int = 0) -> None:
        """Record one more lead for this rep and take them out of the pools when full."""
        rep.current_capacity += 1
        self.value_received[rep.id] += lead_score
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
    """Pools this lead may use, most traits in common first, each labelled with the match level.

    Example: a Europe / German / Finance lead tries
        ("Europe", "German", "Finance")    3 of 3
        ("Europe", "German", ANY)          2 of 3: region + language
        (ANY,      "German", "Finance")    2 of 3: language + industry
        ("Europe", ANY,      "Finance")    2 of 3: region + industry
        (ANY,      "German", ANY)          1 of 3: language
        ("Europe", ANY,      ANY)          1 of 3: region
        (ANY,      ANY,      "Finance")    1 of 3: industry
    """
    min_traits = settings.get("min_traits_to_match", 3)
    relax_order = settings.get("relax_order", ["industry", "region", "language"])
    regions = [lead.region] if lead.region else []
    if lead.region and settings.get("try_alternate_region_when_ambiguous", True):
        regions += list(lead.alt_regions)
    language, industry = lead.language, lead.industry

    pools: list[tuple[PoolKey, MatchLevel]] = []

    # ---- 3 of 3 (industry None here = reps whose expertise is also unknown)
    if language:
        for i, region in enumerate(regions):
            pools.append(((region, language, industry), MatchLevel.EXACT if i == 0 else MatchLevel.ALTERNATE_REGION))

    # ---- 2 of 3: let one trait differ, in the configured order
    # (an unknown industry may always be relaxed, even in strict mode, as before)
    for trait in relax_order:
        if trait == "industry" and language and (min_traits <= 2 or industry is None):
            if industry is None and not settings.get("relax_industry_when_unknown", True):
                continue
            pools += [((region, language, ANY), MatchLevel.RELAXED_INDUSTRY) for region in regions]
        elif trait == "region" and min_traits <= 2 and language and industry:
            pools.append(((ANY, language, industry), MatchLevel.RELAXED_REGION))
        elif trait == "language" and min_traits <= 2 and industry:
            pools += [((region, ANY, industry), MatchLevel.RELAXED_LANGUAGE) for region in regions]

    # ---- 1 of 3: last resort, keep the most useful single trait (reverse of relax_order)
    if min_traits <= 1:
        for trait in reversed(relax_order):
            if trait == "language" and language:
                pools.append(((ANY, language, ANY), MatchLevel.ONLY_LANGUAGE))
            elif trait == "region" and regions:
                pools += [((region, ANY, ANY), MatchLevel.ONLY_REGION) for region in regions]
            elif trait == "industry" and industry:
                pools.append(((ANY, ANY, industry), MatchLevel.ONLY_INDUSTRY))

    # remove repeats, keep the first (best) label
    seen: set[PoolKey] = set()
    return [(pool, level) for pool, level in pools if not (pool in seen or seen.add(pool))]


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
    candidate_pools = pools_to_try(lead, settings)
    if not candidate_pools:
        return manager_review(lead, OverflowReason.MISSING_ROUTING_DATA)
    groups = seniority_groups(lead.priority, settings)

    # Best match first; within a match, preferred seniority first.
    for pool, match_level in candidate_pools:
        for allowed in groups:
            rep = pools.pick_rep(pool, allowed)
            if rep is None:
                continue
            pools.assign(rep, lead.score)
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
    return manager_review(lead, reason)


def route_all(leads: Iterable[Lead], reps: list[Rep], strategy: str | None = None,
              live: bool = False) -> RoutingResult:
    """Route every lead, most urgent first.

    `live=True` simulates leads arriving one at a time during the day: they are routed in file
    order instead of being sorted Critical-first. Used by `leadrouter compare` to show why
    batching Critical leads first matters and how each strategy copes with mixed arrivals.

    `strategy` picks the distribution method (see distribution.py); by default it comes from
    rules.yaml (routing.distribution_strategy), which is "fair_share".
    Every lead comes back exactly once: either Assigned or in Manager Review.
    Note: this updates rep.current_capacity on the rep objects you pass in.
    """
    settings = load_rules()["routing"]
    pools = RepPools(reps, strategy or settings.get("distribution_strategy"))
    ordered = sorted(leads, key=(lambda lead: lead.row_number) if live else priority_order)

    # Fairness snapshots at 10%, 20%, ... 100% of the way through, so we can show how even the
    # distribution is WHILE leads are arriving, not just at the end (see distribution.py).
    checkpoints = {max(1, round(len(ordered) * step / 10)) for step in range(1, 11)} if ordered else set()
    fairness_timeline = []

    assignments = []
    for count, lead in enumerate(ordered, start=1):
        assignments.append(route_lead(lead, pools, settings))
        if count in checkpoints:
            fairness_timeline.append(distribution_fairness(reps))
    value_fairness = distribution_fairness(reps, pools.value_received)

    overflow = [a for a in assignments if a.status is AssignmentStatus.MANAGER_REVIEW]
    return RoutingResult(assignments=assignments, overflow=overflow, reps=reps,
                         fairness_timeline=fairness_timeline, value_fairness=value_fairness)


# ======================================================================================
# Building the Assignment records
# ======================================================================================
def describe_pool(pool: PoolKey) -> str:
    region, language, industry = pool
    region_text = "any region" if region == ANY else region
    language_text = "any language" if language == ANY else language
    industry_text = "any industry" if industry == ANY else (industry or "unknown industry")
    return f"{region_text}/{language_text}/{industry_text}"


def assigned(lead: Lead, rep: Rep, pool: PoolKey, match_level: MatchLevel) -> Assignment:
    reason = routing_sentence(lead, rep, match_level)
    return Assignment(lead_id=lead.id, rep_id=rep.id, priority=lead.priority, score=lead.score,
                      reason=reason, status=AssignmentStatus.ASSIGNED, match_level=match_level,
                      rep_name=rep.name, rep_seniority=rep.seniority)


def manager_review(lead: Lead, reason: OverflowReason) -> Assignment:
    return Assignment(lead_id=lead.id, rep_id=None, priority=lead.priority, score=lead.score,
                      reason=review_sentence(lead, reason), status=AssignmentStatus.MANAGER_REVIEW,
                      overflow_reason=reason)
