"""Bounded, read-only capacity experiments against an existing routing run."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from .config import load_rules
from .models import AssignmentStatus, PipelineResult, Priority, Seniority
from .routing import route_all


class CapacityError(ValueError):
    """A scenario cannot safely be compared with the active run."""


@dataclass(frozen=True)
class CapacityComparison:
    baseline_assigned: int
    baseline_review: int
    baseline_critical_waiting: int
    scenarios: list[dict[str, Any]]
    matching_mode: str


def _placement(assignments):
    return {a.lead_id: (a.status, a.rep_id, a.match_level, a.overflow_reason) for a in assignments}


def compare_capacity(result: PipelineResult, rep_ids: list[str], extra_slots: int) -> CapacityComparison:
    """Each selected rep gets +extra_slots in an INDEPENDENT scenario, never cumulatively.

    Replay saved lead order/scores, strategy and matching minimum with fresh zero-load copies.
    Reject a baseline that current routing rules cannot reproduce. No STATE writes, new hires,
    scoring changes or automatic policy exceptions. Return at most 20 decision examples per case.
    """
    if not isinstance(rep_ids, list) or not 1 <= len(rep_ids) <= 3 or not all(isinstance(r, str) for r in rep_ids) or len(set(rep_ids)) != len(rep_ids):
        raise CapacityError("Choose between one and three different representatives.")
    if type(extra_slots) is not int or not 1 <= extra_slots <= 50:
        raise CapacityError("Extra slots must be a whole number from 1 to 50.")
    if any(a.manual for a in result.assignments):
        raise CapacityError("This run includes manual assignments. Re-run automatic routing before comparing capacity.")
    known = {r.id: r for r in result.reps}
    if any(rep_id not in known for rep_id in rep_ids):
        raise CapacityError("A selected representative is no longer in this run. Choose again.")
    leads = {lead.id: lead for lead in result.leads}
    expected = _placement(result.assignments)
    if len(leads) != len(result.leads) or len(expected) != len(result.assignments) or set(leads) != set(expected):
        raise CapacityError("This run has inconsistent lead identities. Run routing again first.")
    settings_before = json.dumps(load_rules(), sort_keys=True)
    strategy = result.metrics.get("strategy")
    minimum = result.metrics.get("min_traits", 1)
    original = {a.lead_id: a for a in result.assignments}
    # Every replay starts from the original, already-scored input, never the previous scenario.
    def replay(target=None):
        copied_leads = deepcopy([leads[a.lead_id] for a in result.assignments])
        # live=True sorts by row_number. On copies only, encode the saved dispatch order
        # so both normal Critical-first runs and arrival-order runs replay exactly.
        for ordinal, lead in enumerate(copied_leads):
            lead.row_number = ordinal
        copied_reps = deepcopy(result.reps)
        for rep in copied_reps:
            rep.current_capacity = 0
            if rep.id == target:
                rep.max_capacity += extra_slots
        routed = route_all(copied_leads, copied_reps, strategy=strategy, live=True, min_traits=minimum)
        if set(_placement(routed.assignments)) != set(expected) or len(routed.assignments) != len(expected):
            raise CapacityError("A scenario did not retain one decision per lead.")
        if any(rep.current_capacity > rep.max_capacity for rep in routed.reps) or any(
            a.priority is Priority.CRITICAL and a.rep_seniority is Seniority.JUNIOR for a in routed.assignments
        ):
            raise CapacityError("A scenario failed the capacity or Critical-lead safety check.")
        return routed

    baseline = replay()
    if _placement(baseline.assignments) != expected:
        raise CapacityError("Current rules no longer reproduce this run. Re-run routing before comparing capacity.")
    assigned = sum(a.status is AssignmentStatus.ASSIGNED for a in result.assignments)
    critical_waiting = sum(a.priority is Priority.CRITICAL and not a.rep_id for a in result.assignments)
    scenarios = []
    for rep_id in rep_ids:
        routed = replay(rep_id)
        now_assigned = sum(a.status is AssignmentStatus.ASSIGNED for a in routed.assignments)
        now_critical = sum(a.priority is Priority.CRITICAL and not a.rep_id for a in routed.assignments)
        examples = []; newly_assigned = newly_waiting = moved = changed = 0
        for after in routed.assignments:
            before = original[after.lead_id]
            newly_assigned += int(not before.rep_id and bool(after.rep_id))
            newly_waiting += int(bool(before.rep_id) and not after.rep_id)
            moved += int(bool(before.rep_id) and bool(after.rep_id) and before.rep_id != after.rep_id)
            if (before.status, before.rep_id, before.match_level) != (after.status, after.rep_id, after.match_level):
                changed += 1
                if len(examples) < 20:
                    examples.append({"lead_id": after.lead_id, "priority": after.priority.value,
                                     "before": before.rep_name or "Manager Review",
                                     "after": after.rep_name or "Manager Review",
                                     "match": after.match_level.value if after.match_level else "—"})
        rep = known[rep_id]
        scenarios.append({"rep_id": rep_id, "rep_name": rep.name, "seniority": rep.seniority.value,
                          "region": rep.region, "language": rep.language, "industry": rep.industry,
                          "old_capacity": rep.max_capacity, "new_capacity": rep.max_capacity + extra_slots,
                          "extra_slots": extra_slots, "assigned": now_assigned,
                          "review": len(result.assignments) - now_assigned,
                          "critical_waiting": now_critical, "critical_resolved": critical_waiting - now_critical,
                          "net_assigned": now_assigned - assigned, "newly_assigned": newly_assigned,
                          "newly_waiting": newly_waiting, "moved_owners": moved,
                          "changed_decisions": changed, "examples": examples,
                          "net_assigned_per_slot": round((now_assigned - assigned) / extra_slots, 3)})
    if json.dumps(load_rules(), sort_keys=True) != settings_before:
        raise CapacityError("Routing rules changed during the comparison. Try again with a fresh run.")
    scenarios.sort(key=lambda s: (-s["critical_resolved"], -s["net_assigned"], s["moved_owners"], s["rep_id"]))
    return CapacityComparison(assigned, len(result.assignments) - assigned, critical_waiting,
                              scenarios, result.metrics.get("matching_mode", str(minimum)))
