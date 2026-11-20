import pytest

from conftest import LEAD_HEADER, REP_HEADER, csv
from lead_router.cleaning import build_canonicalizers, clean_leads, clean_reps, normalize_text, \
    parse_company_size, parse_vip
from lead_router.models import Seniority


@pytest.fixture(scope="module")
def canon():
    return build_canonicalizers()


def test_normalize_text():
    assert normalize_text("  NORTH   America. ") == "north america"
    assert normalize_text("Asia-Pacific") == "asia pacific"
    assert normalize_text(None) == ""
    assert normalize_text(float("nan")) == ""


@pytest.mark.parametrize("raw,expected", [
    ("North America", "North America"), (" North America ", "North America"),
    ("north america", "North America"), ("NA", "North America"), ("Nort America", "North America"),
    ("North  Amer", "North America"), ("EUR", "Europe"), ("Europ", "Europe"), (" EUROPE ", "Europe"),
    ("APAC", "Asia-Pacific"), ("Asia.  Pacific", "Asia-Pacific"), ("Asa-Pacific", "Asia-Pacific"),
])
def test_region_aliases_casing_and_typos(canon, raw, expected):
    assert canon["region"].resolve(raw).value == expected


def test_compound_region_keeps_first_and_records_alternate(canon):
    r = canon["region"].resolve("ASIA   PACIFICEurope")
    assert (r.value, r.alternates) == ("Asia-Pacific", ("Europe",))
    r = canon["region"].resolve("NORTH   AMERICA Asia-Pacific ")
    assert (r.value, r.alternates) == ("North America", ("Asia-Pacific",))


@pytest.mark.parametrize("field,raw,expected", [
    ("language", "EN", "English"), ("language", "es", "Spanish"), ("language", "DE", "German"),
    ("language", "german", "German"), ("industry", " Finance", "Finance"),
    ("industry", "Technology ", "Technology"), ("seniority", "sr", "Senior"), ("seniority", " junior", "Junior"),
    ("seniority", "MGR", "Manager"), ("seniority", "JR", "Junior"),
    ("activity", "requested live demo ", "Requested Live Demo"), ("activity", "Downloaded  Whitepaper", "Downloaded Whitepaper"),
])
def test_aliases(canon, field, raw, expected):
    assert canon[field].resolve(raw).value == expected


@pytest.mark.parametrize("raw", ["", "  ", None, "N/A", "null", "None", "Unknown", float("nan")])
def test_blanks_and_nulls_are_missing(canon, raw):
    r = canon["industry"].resolve(raw)
    assert r.value is None and r.status == "missing"


def test_junk_is_invalid_not_a_crash(canon):
    r = canon["region"].resolve("@@@ zzz ###")
    assert r.value is None and r.status == "invalid"


@pytest.mark.parametrize("raw,expected", [
    ("1015", 1015), ("1,000", 1000), ("5k", 5000), ("500-1000", 750), (" 42 ", 42), (78, 78),
    ("", None), ("None", None), ("null", None), ("N/A", None), ("-5", None), ("0", None),
    ("lots", None), ("12abc", None),
])
def test_company_size_parser(raw, expected):
    assert parse_company_size(raw)[0] == expected


@pytest.mark.parametrize("raw,expected", [
    ("True", True), ("true", True), ("TRUE", True), ("yes", True), ("1", True), ("Y", True),
    ("False", False), ("no", False), ("0", False), ("Unknown", False), ("", False), ("maybe", False),
])
def test_vip_parser(raw, expected):
    assert parse_vip(raw)[0] is expected


def test_messy_lead_row_is_kept_and_imputed():
    df = csv(LEAD_HEADER + "L-1,,???,,Unknown,N/A,,Unknown\n")
    out, report = clean_leads(df)
    assert len(out) == 1
    lead = out[0]
    assert lead.region is None and lead.language is None and lead.industry is None
    assert lead.company_size is None and lead.vip_referral is False
    assert {"region", "language", "industry", "company_size", "recent_activity", "vip_referral"} <= set(lead.imputed_fields)
    assert report.imputed + report.invalid >= 5


def test_missing_lead_id_is_generated():
    out, _ = clean_leads(csv(LEAD_HEADER + ",Acme,NA,EN,Retail,10,,False\n"))
    assert out[0].id.startswith("L-AUTO-")


def test_exact_duplicate_rows_merge_but_conflicting_ids_are_kept():
    df = csv(LEAD_HEADER +
             "L-1,Acme,NA,EN,Retail,10,Attended Webinar,False\n"
             "L-1,Acme,NA,EN,Retail,10,Attended Webinar,False\n"   # exact duplicate -> merged
             "L-1,Other,EUR,DE,Finance,99,,False\n"                # same ID, different lead -> kept
             "L-2,Acme,NA,EN,Retail,10,Attended Webinar,False\n")  # same content, new ID -> flagged
    out, report = clean_leads(df)
    assert [l.id for l in out] == ["L-1", "L-1-2", "L-2"]
    assert report.duplicates_removed == 1 and report.duplicate_ids_renamed == 1
    assert any(f.startswith("possible_duplicate_of:L-1") for f in out[2].flags)


def test_headers_are_case_and_space_tolerant():
    df = csv("lead id, REGION ,language,industry_vertical\nL-9,EUR,es,Tech\n")
    out, _ = clean_leads(df)
    assert (out[0].id, out[0].region, out[0].language, out[0].industry) == ("L-9", "Europe", "Spanish", "Technology")


def test_rep_capacity_validation():
    df = csv(REP_HEADER +
             "R-1,Ann (R-1),NA,EN,Technology,sr,0,-20\n"     # negative -> abs
             "R-2,Bob,NA,EN,Technology,Mgr,0,\n"             # missing -> seniority default
             "R-3,Cy,NA,EN,Technology,jr,7,junk\n"           # junk max, current != 0 -> reset
             "R-4,Di,NA,EN,Technology,???,0,40\n")           # unknown seniority -> Junior (safe)
    reps, report = clean_reps(df)
    r1, r2, r3, r4 = reps
    assert r1.max_capacity == 20 and "negative_max_capacity_fixed" in r1.flags and r1.name == "Ann"
    assert r2.max_capacity == 7 and r2.seniority is Seniority.MANAGER
    assert r3.max_capacity == 45 and r3.current_capacity == 0 and "current_capacity_reset" in r3.flags
    assert r4.seniority is Seniority.JUNIOR
    assert all(r.current_capacity == 0 for r in reps)
    assert report.rows_out == 4


def test_duplicate_rep_ids_are_renamed():
    reps, _ = clean_reps(csv(REP_HEADER + "R-1,A,NA,EN,Retail,sr,0,20\nR-1,B,NA,EN,Retail,sr,0,20\n"))
    assert [r.id for r in reps] == ["R-1", "R-1-2"]
