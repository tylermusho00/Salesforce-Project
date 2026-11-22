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
    if isinstance(value, str):
        return _normalize_str(value)    # the common case: remembered per distinct spelling
    if is_blank(value):
        return ""
    return _normalize_str(str(value))


@lru_cache(maxsize=65536)
def _normalize_str(value: str) -> str:
    text = re.sub(r"[^0-9a-z]+", " ", value.lower())  # anything that isn't a letter/digit becomes a space
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
        if isinstance(value, float) and not math.isfinite(value):
            return None, INVALID
        return int(value), OK
    return _parse_int_text(value if isinstance(value, str) else str(value))


@lru_cache(maxsize=65536)
def _parse_int_text(value: str) -> tuple[int | None, str]:
    """parse_int for text. Pure, so each distinct spelling ("1,000", "5k", ...) is read once."""
    text = value.strip().lower()
    if is_missing_token(normalize_text(text)):
        return None, MISSING

    compact = text.replace(",", "").replace("_", "").replace(" ", "")
    status = OK if compact == text else FIXED   # e.g. "1,000" needed fixing

    single = _SINGLE_NUMBER.match(compact)
    if single:
        number, suffix = single.groups()
        if suffix:
            status = FIXED
        amount = float(number) * _MULTIPLIER[suffix]
        if not math.isfinite(amount):     # "9" x 400 overflows to infinity: not a usable number
            return None, INVALID
        return round(amount), status

    number_range = _NUMBER_RANGE.match(compact)
    if number_range:
        low, low_suffix, high, high_suffix = number_range.groups()
        low_value = float(low) * _MULTIPLIER[low_suffix or high_suffix]   # "1-5k" means 1k-5k
        high_value = float(high) * _MULTIPLIER[high_suffix]
        middle = (low_value + high_value) / 2
        if not math.isfinite(middle):
            return None, INVALID
        return round(middle), FIXED

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
    names = list(df.columns)
    if len(set(names)) != len(names):                 # odd file with repeated headers: slow, safe path
        df = df.copy()
        for col in columns:
            if col not in df.columns:
                df[col] = None
        return df.to_dict("records")
    # Faster than DataFrame.to_dict: pull each column out once as a plain Python list, then zip.
    # Same keys (file columns in order, then any missing optional column as None) and same values.
    values = [df[name].tolist() for name in names]
    for col in columns:
        if col not in df.columns:
            names.append(col)
            values.append([None] * len(df))
    return [dict(zip(names, row)) for row in zip(*values)]


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
    source_rows = set()
    for row_number, row in enumerate(rows_as_dicts(df, LEAD_COLUMNS), start=1):
        # Independent of duplicate classification: retain every distinct canonical source row.
        source_rows.add(tuple(str(row.get(column, "")) for column in LEAD_COLUMNS))
        lead = clean_lead_row(row, row_number, cleaners, report)
        if duplicates.should_keep(lead):
            leads.append(lead)

    report.source_rows_preserved = source_rows == {
        tuple(str(lead.raw.get(column, "")) for column in LEAD_COLUMNS) for lead in leads
    }
    report.rows_out = len(leads)
    report.value_map = build_value_map(df, LEAD_VALUE_FIELDS, cleaners)
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
        flags.append("vip_unknown")   # scored as not-VIP (no change), but a person should confirm

    return Lead(
        id=lead_id, company_name=company, region=region.value, language=language.value,
        industry=industry.value, company_size=company_size, recent_activity=activity.value,
        vip_referral=vip, alt_regions=region.alternates, imputed_fields=imputed, flags=flags,
        raw=row, row_number=row_number,
    )


def unique_id(original_id: str, taken: Any, times_seen: dict[str, int]) -> str:
    """Next free "<id>-N" for a clashing ID (N = 2, 3, ...), skipping any ID already in `taken`."""
    number = times_seen.get(original_id, 1)
    while True:
        number += 1
        candidate = f"{original_id}-{number}"
        if candidate not in taken:
            times_seen[original_id] = number
            return candidate


_DUPLICATE_CHECKER_FLAGS = ("duplicate_id_renamed", "possible_duplicate_of:")


class DuplicateChecker:
    """Decides what to do with repeated leads.

        same ID + same identity      -> the same lead uploaded twice: keep only the first
        same ID + different identity -> two real leads with a clashing ID: keep both, rename to "-2"
        new ID  + same content       -> maybe a duplicate: keep it, but flag it for the manager

    "Identity" compares the original source columns. Distinct spellings, unknown values and
    alternative regions remain available even when they normalize to the same cleaned content.

    "Same ID" means the ID as written in the file. A rename never reuses an ID that is already
    taken: with IDs A, A, A-2 the second A becomes A-2, so the real A-2 becomes A-2-2.
    Every kept lead has its own ID, and only a true re-upload is ever dropped.
    """

    def __init__(self, report: DataQualityReport):
        self.report = report
        # ID as written in the file -> the kept lead with that ID (a list once the ID repeats)
        self.kept_by_source_id: dict[str, Lead | list[Lead]] = {}
        self.ids_taken: set[str] = set()                  # IDs of the leads we kept (after renaming)
        self.times_id_seen: dict[str, int] = {}
        self.first_id_with_content: dict[tuple, str] = {}

    @staticmethod
    def content_of(lead: Lead) -> tuple:
        """The cleaned values. Two leads with the same content *look* alike (possible duplicate)."""
        return (lead.company_name.lower(), lead.region, lead.language, lead.industry,
                lead.company_size, lead.recent_activity, lead.vip_referral)

    @staticmethod
    def identity_of(lead: Lead) -> tuple:
        """Exact source-column values, before lossy parsing or normalization.

        Similar cleaned rows remain possible duplicates, not automatic merges. Rows made by
        callers without raw data use the complete cleaned identity as a compatibility fallback.
        """
        if lead.raw:
            return tuple(str(lead.raw.get(column, "")) for column in LEAD_COLUMNS)
        flags = sorted(f for f in lead.flags if not f.startswith(_DUPLICATE_CHECKER_FLAGS))
        return (DuplicateChecker.content_of(lead), tuple(lead.alt_regions),
                tuple(sorted(lead.imputed_fields)), tuple(flags))

    def should_keep(self, lead: Lead) -> bool:
        content = self.content_of(lead)
        original_id = lead.id

        # Identity only matters when the ID was seen before (rare), so it is only built then.
        earlier = self.kept_by_source_id.get(original_id)
        if earlier is None:
            self.kept_by_source_id[original_id] = lead
        else:
            if not isinstance(earlier, list):
                earlier = self.kept_by_source_id[original_id] = [earlier]
            identity = self.identity_of(lead)
            if any(self.identity_of(kept) == identity for kept in earlier):
                self.report.duplicates_removed += 1       # exact re-upload of a row we kept
                return False
            earlier.append(lead)

        if original_id in self.ids_taken:                 # ID clash with different content
            lead.id = unique_id(original_id, self.ids_taken, self.times_id_seen)
            lead.flags.append("duplicate_id_renamed")
            self.report.duplicate_ids_renamed += 1
        self.ids_taken.add(lead.id)

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
    ids_taken: set[str] = set()

    reps = []
    for row_number, row in enumerate(rows_as_dicts(df, REP_COLUMNS), start=1):
        flags: list[str] = []

        # --- ID (must be unique, because routing tracks reps by ID)
        rep_id = "" if is_blank(row["Rep_ID"]) else str(row["Rep_ID"]).strip()
        if not rep_id or is_missing_token(normalize_text(rep_id)):
            rep_id = f"R-AUTO-{row_number}"
            report.count("rep_id", "imputed")
        if rep_id in ids_taken:     # clash: next free "-2", "-3"... (never an ID already used)
            rep_id = unique_id(rep_id, ids_taken, times_id_seen)
            flags.append("duplicate_id_renamed")
            report.duplicate_ids_renamed += 1
        ids_taken.add(rep_id)

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
    report.value_map = build_value_map(df, REP_VALUE_FIELDS, cleaners)
    return reps, report


# ======================================================================================
# Value audit: every distinct raw value and what it became (DataQualityReport.value_map)
# ======================================================================================
# report field name -> (CSV column, how to read it). "text:<kind>" uses that Canonicalizer.
LEAD_VALUE_FIELDS = {
    "region": ("Region", "text:region"),
    "language": ("Language", "text:language"),
    "industry": ("Industry_Vertical", "text:industry"),
    "recent_activity": ("Recent_Activity", "text:activity"),
    "vip_referral": ("VIP_Referral", "vip"),
    "company_size": ("Company_Size", "company_size"),
}
REP_VALUE_FIELDS = {
    "region": ("Region", "text:region"),
    "language": ("Language", "text:language"),
    "industry": ("Industry_Vertical_Expertise", "text:industry"),
    "seniority": ("Seniority_Level", "text:seniority"),
    "max_capacity": ("Max_Capacity", "max_capacity"),
}

# Number formats, in the order the dashboard lists them.
NUMBER_FORMATS = ["plain number", "thousands comma", "k suffix", "range",
                  "missing token", "invalid", "non-positive"]


def number_format(value: Any) -> str:
    """Which way a number was written: "1000" plain number, "1,000" thousands comma,
    "5k" k suffix, "500-1000" range, "N/A" missing token, "lots" invalid, "-20" / "0" non-positive."""
    if is_blank(value) or is_missing_token(normalize_text(value)):
        return "missing token"
    number, _ = parse_int(value)
    if number is None:
        return "invalid"
    if number <= 0:
        return "non-positive"
    text = str(value).strip().lower()
    compact = text.replace(",", "").replace("_", "").replace(" ", "")
    if _NUMBER_RANGE.match(compact):
        return "range"
    single = _SINGLE_NUMBER.match(compact)
    if single and single.group(2):
        return "k suffix"
    return "thousands comma" if "," in text else "plain number"


def _company_size_becomes(value: Any) -> str:
    """What a company size value turns into (mirrors parse_company_size + scoring's fallback)."""
    size, _ = parse_company_size(value)
    if size is not None:
        return f"{size:,}"
    points = load_rules()["scoring"]["company_size"]["missing_points"]
    return f"unknown (standard +{points} points)"


def _max_capacity_becomes(value: Any) -> str:
    """What a max capacity value turns into (mirrors the capacity rules in clean_reps)."""
    number, _ = parse_int(value)
    if number is None:
        defaults = load_rules()["routing"]["default_max_capacity"]
        return "seniority default (" + " / ".join(f"{k} {v}" for k, v in defaults.items()) + ")"
    if number < 0:
        return f"{abs(number)} (sign typo fixed)"
    if number == 0:
        return "0 (never gets leads)"
    return str(number)


def _raw_counts(df: pd.DataFrame, column: str) -> list[tuple[str, int]]:
    """(raw text, rows) for every distinct raw value, most common first. Blank cells -> ""."""
    if column not in df.columns:
        return [("", len(df))] if len(df) else []
    counts: dict[str, int] = {}
    for raw, count in df[column].value_counts(dropna=False).items():   # pandas counts, fast
        key = "" if is_blank(raw) else str(raw)
        counts[key] = counts.get(key, 0) + int(count)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def build_value_map(df: pd.DataFrame, fields: dict[str, tuple[str, str]],
                    cleaners: dict[str, Canonicalizer]) -> dict[str, list[dict[str, Any]]]:
    """For each field, what cleaning did with every distinct raw value (counted over source rows).

    Text fields: one row per raw spelling
        {"raw": "ASIA   PACIFICEurope", "cleaned": "Asia-Pacific", "status": "fixed",
         "count": 12, "alternate": "Europe"}
    Number fields: one row per way of writing the number, with one example
        {"format": "k suffix", "count": 40, "example": "5k", "becomes": "5,000"}

    Each distinct value is worked out only once (the Canonicalizers also remember their answers),
    so the cost depends on the number of different spellings, not the number of rows.
    """
    value_map: dict[str, list[dict[str, Any]]] = {}
    for field_name, (column, kind) in fields.items():
        counts = _raw_counts(df, column)
        rows: list[dict[str, Any]] = []
        if kind.startswith("text:"):
            cleaner = cleaners[kind.split(":", 1)[1]]
            for raw, count in counts:
                result = cleaner.resolve(raw)
                rows.append({"raw": raw, "cleaned": result.value or "unknown", "status": result.status,
                             "count": count, "alternate": ", ".join(result.alternates)})
        elif kind == "vip":
            for raw, count in counts:
                vip, status = parse_vip(raw)
                cleaned = ("Yes" if vip else "No") if status == OK else "unknown"   # unknown = scored as No
                rows.append({"raw": raw, "cleaned": cleaned, "status": status, "count": count,
                             "alternate": ""})
        else:
            becomes = _company_size_becomes if kind == "company_size" else _max_capacity_becomes
            by_format: dict[str, dict[str, Any]] = {}
            for raw, count in counts:   # most common first, so each example is a typical one
                number_kind = number_format(raw)
                if number_kind not in by_format:   # first (most common) value of this format = example
                    by_format[number_kind] = {"count": 0, "example": raw, "becomes": becomes(raw)}
                by_format[number_kind]["count"] += count
            rows = [{"format": name, **by_format[name]} for name in NUMBER_FORMATS if name in by_format]
        value_map[field_name] = rows
    return value_map
