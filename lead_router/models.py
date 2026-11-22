"""
models.py - the data shapes every other file passes around.

Reading guide
-------------
    Lead          one prospective customer (one row of the leads CSV, after cleaning)
    Rep           one sales representative (one row of the reps CSV, after cleaning)
    Assignment    the routing decision for one lead: which rep, or Manager Review, and why
    PipelineResult  everything a single run produces (used by the CLI and the dashboard)

We use Python dataclasses: they are plain classes that auto-generate __init__ for us.
`slots=True` makes them smaller and faster, which matters with 50,000 leads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# --------------------------------------------------------------------------------------
# Fixed vocabularies (enums). Using enums instead of raw strings means a typo such as
# "Seinor" becomes an error immediately instead of a silent bug.
# --------------------------------------------------------------------------------------
class Seniority(StrEnum):
    JUNIOR = "Junior"
    SENIOR = "Senior"
    MANAGER = "Manager"


class Priority(StrEnum):
    CRITICAL = "Critical"
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"

    @property
    def rank(self) -> int:
        """0 = most urgent. Sorting by rank puts Critical leads first."""
        return _PRIORITY_RANK[self]


_PRIORITY_RANK = {priority: rank for rank, priority in enumerate(Priority)}  # Critical=0 ... Low=3


class AssignmentStatus(StrEnum):
    ASSIGNED = "Assigned"
    MANAGER_REVIEW = "Manager Review"


class OverflowReason(StrEnum):
    """Why a lead ended up in Manager Review instead of with a rep."""
    NO_MATCH = "No rep shares a trait"                           # nobody shares region, language or industry
    ALL_AT_CAPACITY = "Matching reps are all full"               # the right reps exist but are full
    NO_SENIOR_AVAILABLE = "Urgent lead, only Juniors match"      # Critical leads never go to Juniors
    MISSING_ROUTING_DATA = "Not enough info to match"            # region, language and industry all unknown


class MatchLevel(StrEnum):
    """How closely the chosen rep matches the lead on the three traits (region, language, industry)."""
    EXACT = "Exact"                                         # 3 of 3
    ALTERNATE_REGION = "Alternate region"                   # 3 of 3, using the lead's 2nd (ambiguous) region
    RELAXED_INDUSTRY = "2 of 3: region + language"          # industry differs (or is unknown)
    RELAXED_REGION = "2 of 3: language + industry"          # region differs (or is unknown)
    RELAXED_LANGUAGE = "2 of 3: region + industry"          # language differs (or is unknown)
    ONLY_LANGUAGE = "1 of 3: language"                      # last resort: only the language matches
    ONLY_REGION = "1 of 3: region"
    ONLY_INDUSTRY = "1 of 3: industry"

    @property
    def traits_matched(self) -> int:
        if self in (MatchLevel.EXACT, MatchLevel.ALTERNATE_REGION):
            return 3
        return 1 if self.value.startswith("1 of 3") else 2


# --------------------------------------------------------------------------------------
# Leads and reps
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class Lead:
    # Cleaned values. None means "unknown" (missing or unreadable in the CSV).
    id: str
    company_name: str
    region: str | None
    language: str | None
    industry: str | None
    company_size: int | None
    recent_activity: str | None
    vip_referral: bool

    # Extra information gathered while cleaning.
    alt_regions: tuple[str, ...] = ()                       # second region from values like "ASIA PACIFICEurope"
    imputed_fields: list[str] = field(default_factory=list) # fields we had to fill in ourselves
    flags: list[str] = field(default_factory=list)          # other notes, e.g. "duplicate_id_renamed"
    raw: dict[str, Any] = field(default_factory=dict)       # the original CSV row, for the detail view
    row_number: int = 0                                     # position in the file (used as last tiebreaker)

    # Filled in by scoring.score_all().
    score: int = 0
    priority: Priority = Priority.LOW
    score_reasons: list[tuple[str, int]] = field(default_factory=list)  # [(reason, points), ...]
    explanation: str = ""
    # Filled in by anomaly.flag_leads() (machine learning). None = nothing unusual.
    anomaly: str | None = None
    anomaly_score: float = 0.0


@dataclass(slots=True)
class Rep:
    id: str
    name: str
    region: str | None
    language: str | None
    industry: str | None        # None = expertise unknown (only gets leads whose industry is also unknown)
    seniority: Seniority
    current_capacity: int       # how many leads this rep holds right now
    max_capacity: int           # the most they may ever hold
    flags: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    anomaly: str | None = None   # set by anomaly.flag_reps() (machine learning)
    anomaly_score: float = 0.0

    @property
    def available(self) -> int:
        """Open slots left."""
        return max(self.max_capacity - self.current_capacity, 0)

    @property
    def load_ratio(self) -> float:
        """How full the rep is, from 0.0 (empty) to 1.0 (full)."""
        if self.max_capacity <= 0:
            return 1.0
        return self.current_capacity / self.max_capacity

    @property
    def is_full(self) -> bool:
        return self.current_capacity >= self.max_capacity


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------
@dataclass(slots=True)
class Assignment:
    lead_id: str
    rep_id: str | None                         # None when the lead went to Manager Review
    priority: Priority
    score: int
    reason: str                                # human-readable explanation of the decision
    status: AssignmentStatus
    overflow_reason: OverflowReason | None = None
    match_level: MatchLevel | None = None
    rep_name: str | None = None
    rep_seniority: Seniority | None = None
    manual: bool = False                       # True when a manager assigned it from Manager Review


@dataclass(slots=True)
class DataQualityReport:
    """Counts of what cleaning had to do. Shown on the dashboard's Data Quality tab."""
    rows_in: int = 0
    rows_out: int = 0
    fixed: int = 0                  # messy but recovered (alias, typo, "5k", negative capacity...)
    imputed: int = 0                # missing, filled with a fallback
    invalid: int = 0                # unreadable even after cleaning
    duplicates_removed: int = 0     # exact duplicate rows (same ID and same content)
    duplicate_ids_renamed: int = 0  # same ID but different content -> kept, renamed "-2"
    possible_duplicates: int = 0    # same content under a different ID -> kept, flagged
    by_field: dict[str, dict[str, int]] = field(default_factory=dict)  # {"region": {"fixed": 12}, ...}
    examples: dict[str, list[str]] = field(default_factory=dict)       # {"region": ['"EUR" -> Europe', ...]}
    warnings: list[str] = field(default_factory=list)

    def count(self, field_name: str, kind: str) -> None:
        """Add one to a total ("fixed", "imputed" or "invalid") and to that field's breakdown."""
        setattr(self, kind, getattr(self, kind) + 1)
        per_field = self.by_field.setdefault(field_name, {})
        per_field[kind] = per_field.get(kind, 0) + 1

    def example(self, field_name: str, before: Any, after: Any, limit: int = 3) -> None:
        """Remember up to `limit` different "before -> after" fixes per field, for the dashboard."""
        text = f'"{before}" -> {after}'
        seen = self.examples.setdefault(field_name, [])
        if len(seen) < limit and text not in seen:
            seen.append(text)


@dataclass(slots=True)
class RoutingResult:
    assignments: list[Assignment]   # one per lead, in the order they were routed
    overflow: list[Assignment]      # the subset that went to Manager Review
    reps: list[Rep]                 # same rep objects, with updated current_capacity
    fairness_timeline: list[float] = field(default_factory=list)  # Jain index at 10%, 20%, ... 100%
    value_fairness: float = 1.0     # Jain index of lead value (score) received per slot


@dataclass(slots=True)
class PipelineResult:
    dataset: str
    leads: list[Lead]
    reps: list[Rep]
    assignments: list[Assignment]
    manager_review: list[Assignment]
    metrics: dict[str, Any]
    timings: dict[str, float]       # seconds per stage
    lead_quality: DataQualityReport
    rep_quality: DataQualityReport
