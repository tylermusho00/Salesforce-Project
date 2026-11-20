"""
cleaning.py - STEP 1: turn messy CSV rows into clean Lead and Rep objects.

The challenge data is deliberately messy:
    regions   "NA", "Nort America", " EUROPE ", "Asa-Pacific", "ASIA   PACIFICEurope"
    languages "EN", "english", "DE", "es"
    sizes     "1,000", "5k", "500-1000", "null", "N/A"
    VIP       "True", "False", "Unknown"
    reps      "sr", "MGR", " junior", max capacity of -20

Golden rule: NEVER drop a lead. If a value can't be read, we record it as unknown (None),
note the field in lead.imputed_fields, and later stages use a fallback for it.

How a text value is cleaned (see Canonicalizer.resolve):
    1. Already correct                       "Europe"               -> ok
    2. Correct after lowercasing/spacing     " EUROPE "             -> fixed
    3. Known alias from rules.yaml           "EUR"                  -> fixed
    4. Two regions glued together            "ASIA PACIFICEurope"   -> fixed (first one kept, second saved)
    5. Typo, closest known spelling (>=85%)  "Asa-Pacific"          -> fixed
    6. Blank / "N/A" / "Unknown"             ""                     -> missing
       Anything else                         "@@@"                  -> invalid
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

import pandas as pd
from rapidfuzz import fuzz, process

from .config import load_rules
from .models import DataQualityReport, Lead, Rep, Seniority

# Status labels returned by every parser.
OK, FIXED, MISSING, INVALID = "ok", "fixed", "missing", "invalid"

# Column names from the data dictionary.
LEAD_COLUMNS = ["Lead_ID", "Company_Name", "Region", "Language", "Industry_Vertical",
                "Company_Size", "Recent_Activity", "VIP_Referral"]
LEAD_REQUIRED = ["Lead_ID", "Region", "Language", "Industry_Vertical"]

REP_COLUMNS = ["Rep_ID", "Name", "Region", "Language", "Industry_Vertical_Expertise",
               "Seniority_Level", "Current_Capacity", "Max_Capacity"]
REP_REQUIRED = ["Rep_ID", "Region", "Language", "Industry_Vertical_Expertise",
                "Seniority_Level", "Max_Capacity"]


# ======================================================================================
# Small text helpers
# ======================================================================================
def is_blank(value: Any) -> bool:
    """True for None and for pandas' NaN ("not a number", used for empty cells)."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def normalize_text(value: Any) -> str:
    """Lowercase, turn punctuation into spaces, collapse repeated spaces.

    "  Asia.  Pacific " -> "asia pacific"      "N/A" -> "n a"      None -> ""
    """
    if is_blank(value):
        return ""
    text = str(value).lower()
    text = re.sub(r"[^0-9a-z]+", " ", text)  # anything that isn't a letter/digit becomes a space
    return text.strip()


@lru_cache(maxsize=1)
def _missing_tokens() -> frozenset[str]:
    return frozenset(normalize_text(token) for token in load_rules()["missing_tokens"])


def is_missing_token(normalized: str) -> bool:
    """Does this (already normalized) text mean "we don't know"? e.g. "", "n a", "null", "unknown"."""
    return normalized in _missing_tokens()


# ======================================================================================
# Canonicalizer: maps any messy spelling to one official value
# ======================================================================================
@dataclass(frozen=True, slots=True)
class Resolution:
    value: str | None                  # the official value, or None if unknown
    status: str                        # OK / FIXED / MISSING / INVALID
    alternates: tuple[str, ...] = ()   # extra regions found in values like "ASIA PACIFICEurope"


class Canonicalizer:
    """Cleans one kind of value (region, language, ...).

    Results are remembered per raw spelling, so 50,000 rows that only use ~15 different
    spellings cost ~15 real lookups.
    """

    def __init__(self, official_values: Iterable[str], aliases: dict[str, str] | None = None,
                 typo_threshold: int = 85, split_glued_values: bool = False):
        self.official_values = list(official_values)
        # normalized spelling -> official value, e.g. "north america" -> "North America"
        self.by_normalized = {normalize_text(v): v for v in self.official_values}
        self.aliases = {normalize_text(k): v for k, v in (aliases or {}).items()}
        self.typo_threshold = typo_threshold
        self.split_glued_values = split_glued_values
        self._memory: dict[str, Resolution] = {}

    def resolve(self, raw: Any) -> Resolution:
        key = "" if is_blank(raw) else str(raw)
        if key not in self._memory:
            self._memory[key] = self._resolve(key)
        return self._memory[key]

    def _resolve(self, raw: str) -> Resolution:
        # 1. Already perfect.
        if raw in self.official_values:
            return Resolution(raw, OK)

        normalized = normalize_text(raw)
        if is_missing_token(normalized):
            return Resolution(None, MISSING)

        # 2. Right word, wrong casing/spacing/punctuation.
        if normalized in self.by_normalized:
            return Resolution(self.by_normalized[normalized], FIXED)

        # 3. Known abbreviation.
        if normalized in self.aliases:
            return Resolution(self.aliases[normalized], FIXED)

        # 4. Glued values: remove all spaces, then look for each official value inside the text.
        if self.split_glued_values:
            found = self._find_glued_values(normalized)
            if found:
                return Resolution(found[0], FIXED, alternates=tuple(found[1:]))

        # 5. Typo: take the closest known spelling if it is similar enough.
        all_spellings = {**self.by_normalized, **self.aliases}
        best = process.extractOne(normalized, all_spellings.keys(), scorer=fuzz.ratio,
                                  score_cutoff=self.typo_threshold)
        if best is not None:
            matched_spelling = best[0]
            return Resolution(all_spellings[matched_spelling], FIXED)

        # 6. Give up: unknown.
        return Resolution(None, INVALID)

    def _find_glued_values(self, normalized: str) -> list[str]:
        """'asia pacificeurope' -> ['Asia-Pacific', 'Europe'] (in the order they appear)."""
        squashed = normalized.replace(" ", "")
        hits = []
        for spelling, official in self.by_normalized.items():
            position = squashed.find(spelling.replace(" ", ""))
            if position >= 0:
                hits.append((position, official))
        hits.sort()
        return list(dict.fromkeys(official for _, official in hits))  # remove repeats, keep order


def build_canonicalizers() -> dict[str, Canonicalizer]:
    """One Canonicalizer per kind of value, configured from rules.yaml."""
    rules = load_rules()
    official, aliases, threshold = rules["canonical"], rules["aliases"], rules["fuzzy_threshold"]
    return {
        "region": Canonicalizer(official["region"], aliases.get("region"), threshold, split_glued_values=True),
        "language": Canonicalizer(official["language"], aliases.get("language"), threshold),
        "industry": Canonicalizer(official["industry"], aliases.get("industry"), threshold),
        "seniority": Canonicalizer(official["seniority"], aliases.get("seniority"), threshold),
        "activity": Canonicalizer(official["activity"], aliases.get("activity"), threshold),
    }


# ======================================================================================
# Number and yes/no parsers
# ======================================================================================
_MULTIPLIER = {None: 1, "": 1, "k": 1_000, "m": 1_000_000}
_SINGLE_NUMBER = re.compile(r"^(-?\d+(?:\.\d+)?)([km])?\+?$")                      # "1000", "5k", "-20", "10+"
_NUMBER_RANGE = re.compile(r"^(\d+(?:\.\d+)?)([km])?(?:-|to)(\d+(?:\.\d+)?)([km])?$")  # "500-1000", "1k-5k"


def parse_int(value: Any) -> tuple[int | None, str]:
    """Read a whole number from messy text.

    "1,000" -> 1000    "5k" -> 5000    "500-1000" -> 750 (middle of the range)
    "-20" -> -20 (the caller decides if negatives make sense)
    "", "N/A", "null" -> (None, MISSING)      "lots" -> (None, INVALID)
    """
    if is_blank(value):
        return None, MISSING
    if isinstance(value, bool):          # True/False are technically ints in Python; reject them
        return None, INVALID
    if isinstance(value, (int, float)):
        return int(value), OK

    text = str(value).strip().lower()
    if is_missing_token(normalize_text(text)):
        return None, MISSING

    compact = text.replace(",", "").replace("_", "").replace(" ", "")
    status = OK if compact == text else FIXED   # e.g. "1,000" needed fixing

    single = _SINGLE_NUMBER.match(compact)
    if single:
        number, suffix = single.groups()
        if suffix:
            status = FIXED
        return round(float(number) * _MULTIPLIER[suffix]), status

    number_range = _NUMBER_RANGE.match(compact)
    if number_range:
        low, low_suffix, high, high_suffix = number_range.groups()
        low_value = float(low) * _MULTIPLIER[low_suffix or high_suffix]   # "1-5k" means 1k-5k
        high_value = float(high) * _MULTIPLIER[high_suffix]
        return round((low_value + high_value) / 2), FIXED

    return None, INVALID


def parse_company_size(value: Any) -> tuple[int | None, str]:
    """Employee count. Zero or negative employees is impossible, so it is treated as unknown."""
    number, status = parse_int(value)
    if number is not None and number <= 0:
        return None, INVALID
    return number, status


_YES = {"true", "t", "yes", "y", "1"}
_NO = {"false", "f", "no", "n", "0"}


def parse_vip(value: Any) -> tuple[bool, str]:
    """true / TRUE / yes / 1 / Y -> True. Anything else (including "Unknown") -> False."""
    if isinstance(value, bool):
        return value, OK
    normalized = normalize_text(value)
    if normalized in _YES:
        return True, OK
    if normalized in _NO:
        return False, OK
    return False, MISSING if is_missing_token(normalized) else INVALID


def clean_name(value: Any, fallback: str) -> tuple[str, str]:
    """Tidy spacing; turn ALL CAPS or all lowercase into Title Case."""
    if is_blank(value) or not str(value).strip():
        return fallback, MISSING
    name = " ".join(str(value).split())
    if name.isupper() or name.islower():
        words = name.title().split()
        # keep business abbreviations in capitals: "Vortex Networks Llc" -> "Vortex Networks LLC"
        words = [w.upper() if w.upper() in _KEEP_UPPER else w for w in words]
        return " ".join(words), FIXED
    return name, OK if name == value else FIXED


_KEEP_UPPER = {"LLC", "LLP", "PLC", "LP", "AG", "GMBH", "SA", "NV", "BV", "USA", "UK", "IT", "AI"}


# ======================================================================================
# DataFrame helpers
# ======================================================================================
def standardize_columns(df: pd.DataFrame, expected: list[str]) -> pd.DataFrame:
    """Accept headers with odd casing/spacing: 'lead id' or ' REGION ' -> 'Lead_ID', 'Region'."""
    wanted = {normalize_text(name): name for name in expected}
    return df.rename(columns={col: wanted.get(normalize_text(col), col) for col in df.columns})


def missing_columns(df: pd.DataFrame, required: list[str]) -> list[str]:
    return [col for col in required if col not in df.columns]


def rows_as_dicts(df: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    """Every row as {column: value}. Optional columns that are absent come back as None."""
    df = df.copy()
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df.to_dict("records")


def _record(report: DataQualityReport, field_name: str, status: str,
            before: Any = None, after: Any = None) -> None:
    """Add a parser's status to the data-quality report (OK is not counted).
    For fixed values, also keep a few "before -> after" examples to show on the dashboard."""
    if status == FIXED:
        report.count(field_name, "fixed")
        if before is not None and after is not None:
            report.example(field_name, before, after)
    elif status == MISSING:
        report.count(field_name, "imputed")
    elif status == INVALID:
        report.count(field_name, "invalid")


# ======================================================================================
# Leads
# ======================================================================================
def clean_leads(df: pd.DataFrame) -> tuple[list[Lead], DataQualityReport]:
    """Clean every lead row. Returns the leads plus a report of what was fixed."""
    df = standardize_columns(df, LEAD_COLUMNS)
    report = DataQualityReport(rows_in=len(df))
    cleaners = build_canonicalizers()
    duplicates = DuplicateChecker(report)

    leads = []
    for row_number, row in enumerate(rows_as_dicts(df, LEAD_COLUMNS), start=1):
        lead = clean_lead_row(row, row_number, cleaners, report)
        if duplicates.should_keep(lead):
            leads.append(lead)

    report.rows_out = len(leads)
    return leads, report


def clean_lead_row(row: dict[str, Any], row_number: int, cleaners: dict[str, Canonicalizer],
                   report: DataQualityReport) -> Lead:
    """Clean a single lead row. Each field: parse it, count the result, note it if unknown."""
    imputed: list[str] = []
    flags: list[str] = []

    lead_id = "" if is_blank(row["Lead_ID"]) else str(row["Lead_ID"]).strip()
    if not lead_id or is_missing_token(normalize_text(lead_id)):
        lead_id = f"L-AUTO-{row_number}"
        imputed.append("lead_id")
        report.count("lead_id", "imputed")

    company, status = clean_name(row["Company_Name"], "(Unknown company)")
    _record(report, "company_name", status, row["Company_Name"], company)

    region = cleaners["region"].resolve(row["Region"])
    _record(report, "region", region.status, row["Region"], region.value)
    if region.value is None:
        imputed.append("region")
    if region.alternates:
        flags.append("ambiguous_region")

    language = cleaners["language"].resolve(row["Language"])
    _record(report, "language", language.status, row["Language"], language.value)
    if language.value is None:
        imputed.append("language")

    industry = cleaners["industry"].resolve(row["Industry_Vertical"])
    _record(report, "industry", industry.status, row["Industry_Vertical"], industry.value)
    if industry.value is None:
        imputed.append("industry")

    company_size, status = parse_company_size(row["Company_Size"])
    _record(report, "company_size", status, row["Company_Size"],
            f"{company_size:,}" if company_size is not None else None)
    if company_size is None:
        imputed.append("company_size")

    activity = cleaners["activity"].resolve(row["Recent_Activity"])
    _record(report, "recent_activity", activity.status, row["Recent_Activity"], activity.value)
    if activity.value is None:
        imputed.append("recent_activity")

    vip, status = parse_vip(row["VIP_Referral"])
    _record(report, "vip_referral", status)
    if status in (MISSING, INVALID):
        imputed.append("vip_referral")

    return Lead(
        id=lead_id, company_name=company, region=region.value, language=language.value,
        industry=industry.value, company_size=company_size, recent_activity=activity.value,
        vip_referral=vip, alt_regions=region.alternates, imputed_fields=imputed, flags=flags,
        raw=row, row_number=row_number,
    )


class DuplicateChecker:
    """Decides what to do with repeated leads.

        same ID + same content       -> the same lead uploaded twice: keep only the first
        same ID + different content  -> two real leads with a clashing ID: keep both, rename to "-2"
        new ID  + same content       -> maybe a duplicate: keep it, but flag it for the manager
    """

    def __init__(self, report: DataQualityReport):
        self.report = report
        self.content_by_id: dict[str, tuple] = {}
        self.times_id_seen: dict[str, int] = {}
        self.first_id_with_content: dict[tuple, str] = {}

    @staticmethod
    def content_of(lead: Lead) -> tuple:
        return (lead.company_name.lower(), lead.region, lead.language, lead.industry,
                lead.company_size, lead.recent_activity, lead.vip_referral)

    def should_keep(self, lead: Lead) -> bool:
        content = self.content_of(lead)
        original_id = lead.id

        if original_id in self.content_by_id:
            if self.content_by_id[original_id] == content:
                self.report.duplicates_removed += 1
                return False
            self.times_id_seen[original_id] += 1
            lead.id = f"{original_id}-{self.times_id_seen[original_id]}"
            lead.flags.append("duplicate_id_renamed")
            self.report.duplicate_ids_renamed += 1
        else:
            self.times_id_seen[original_id] = 1
        self.content_by_id[lead.id] = content

        if content in self.first_id_with_content:
            lead.flags.append(f"possible_duplicate_of:{self.first_id_with_content[content]}")
            self.report.possible_duplicates += 1
        else:
            self.first_id_with_content[content] = lead.id
        return True


# ======================================================================================
# Reps
# ======================================================================================
def clean_reps(df: pd.DataFrame) -> tuple[list[Rep], DataQualityReport]:
    """Clean every rep row. Capacity rules:
        max capacity negative      -> made positive (it's a sign typo: -20 means 20)
        max capacity missing/junk  -> default for the seniority (rules.yaml)
        current capacity           -> always starts at 0 (data dictionary)
        seniority unknown          -> Junior, the safest choice (Juniors never get Critical leads)
    """
    df = standardize_columns(df, REP_COLUMNS)
    report = DataQualityReport(rows_in=len(df))
    cleaners = build_canonicalizers()
    default_capacity = load_rules()["routing"]["default_max_capacity"]
    times_id_seen: dict[str, int] = {}

    reps = []
    for row_number, row in enumerate(rows_as_dicts(df, REP_COLUMNS), start=1):
        flags: list[str] = []

        # --- ID (must be unique, because routing tracks reps by ID)
        rep_id = "" if is_blank(row["Rep_ID"]) else str(row["Rep_ID"]).strip()
        if not rep_id or is_missing_token(normalize_text(rep_id)):
            rep_id = f"R-AUTO-{row_number}"
            report.count("rep_id", "imputed")
        if rep_id in times_id_seen:
            times_id_seen[rep_id] += 1
            rep_id = f"{rep_id}-{times_id_seen[rep_id]}"
            flags.append("duplicate_id_renamed")
            report.duplicate_ids_renamed += 1
        else:
            times_id_seen[rep_id] = 1

        # --- Name ("Ava Lee (R-12)" in the source data: drop the repeated ID)
        name, status = clean_name(row["Name"], rep_id)
        name = name.removesuffix(f" ({rep_id})")
        _record(report, "name", status)

        # --- Skills
        region = cleaners["region"].resolve(row["Region"])
        _record(report, "region", region.status, row["Region"], region.value)
        if region.alternates:
            flags.append("ambiguous_region")

        language = cleaners["language"].resolve(row["Language"])
        _record(report, "language", language.status, row["Language"], language.value)

        industry = cleaners["industry"].resolve(row["Industry_Vertical_Expertise"])
        _record(report, "industry", industry.status, row["Industry_Vertical_Expertise"], industry.value)
        if industry.value is None:
            flags.append("expertise_unknown")

        if region.value is None or language.value is None:
            flags.append("missing_region_or_language")   # only 2-of-3 matches possible

        # --- Seniority
        seniority_result = cleaners["seniority"].resolve(row["Seniority_Level"])
        _record(report, "seniority", seniority_result.status, row["Seniority_Level"], seniority_result.value)
        if seniority_result.value is None:
            flags.append("seniority_defaulted_junior")
        seniority = Seniority(seniority_result.value or Seniority.JUNIOR)

        # --- Capacity
        max_capacity, status = parse_int(row["Max_Capacity"])
        if max_capacity is None:
            max_capacity = int(default_capacity[seniority.value])
            flags.append("max_capacity_defaulted")
            report.count("max_capacity", "imputed" if status == MISSING else "invalid")
        elif max_capacity < 0:
            max_capacity = abs(max_capacity)
            flags.append("negative_max_capacity_fixed")
            report.count("max_capacity", "fixed")
            report.example("max_capacity", row["Max_Capacity"], max_capacity)
        else:
            _record(report, "max_capacity", status)

        current_capacity, _ = parse_int(row["Current_Capacity"])
        if current_capacity not in (None, 0):
            flags.append("current_capacity_reset")
            report.count("current_capacity", "fixed")

        reps.append(Rep(
            id=rep_id, name=name, region=region.value, language=language.value,
            industry=industry.value, seniority=seniority, current_capacity=0,
            max_capacity=max_capacity, flags=flags, raw=row,
        ))

    report.rows_out = len(reps)
    return reps, report
