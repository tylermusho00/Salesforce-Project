"""The dashboard's evidence panels, tested by calling app.main's helpers and callbacks directly
(no browser): render(), Rules Check, tier caption, Manager Review rows, value audit, trace lead,
and the run/reset/upload callback with the matching-mode toggle."""
import base64
from contextvars import copy_context

import pytest
from dash._callback_context import context_value
from dash._utils import AttributeDict

import app.main as dashboard
from conftest import LEAD_HEADER, REP_HEADER
from lead_router.explain import VIP_UNKNOWN_TAG, lead_fixes
from lead_router.models import Priority
from lead_router.pipeline import STATE, PipelineError, run_dataset


def sample(**kw):
    try:
        return run_dataset("sample", **kw)
    except PipelineError:
        pytest.skip("sample dataset not present in data/")


def show(result):
    """Make `result` the dashboard's current run, the way the Run buttons do."""
    dashboard.remember_fingerprint(STATE.set(result), result)
    return result


def click(button: str, *args):
    """Call run_or_reset as if `button` had been clicked in the browser."""
    def call():
        context_value.set(AttributeDict(triggered_inputs=[{"prop_id": f"{button}.n_clicks", "value": 1}]))
        return dashboard.run_or_reset(1, 1, 1, 1, *args)
    return copy_context().run(call)


def upload(text: str) -> str:
    """A browser upload's `contents` string for a CSV."""
    return "data:text/csv;base64," + base64.b64encode(text.encode()).decode()


def text_of(component) -> str:
    """All the text inside a Dash component tree."""
    if component is None:
        return ""
    if isinstance(component, (str, int, float)):
        return str(component)
    if isinstance(component, (list, tuple)):
        return "".join(text_of(c) for c in component)
    return text_of(getattr(component, "children", None))


# ---------------------------------------------------------------- render
def test_render_without_a_run_shows_empty_state_and_disables_trace():
    out = dashboard.render()
    assert out["canvas_style"] == {"display": "none"}
    assert out["trace_disabled"] is True
    assert out["as_of"].startswith("No data yet")


def test_render_after_sample_run_shows_every_evidence_panel():
    result = show(sample())
    m = result.metrics
    out = dashboard.render()
    assert out["canvas_style"] == {} and out["trace_disabled"] is False
    assert f"Matching: {m['matching_mode']}" in out["as_of"]
    assert "traits matched 3/3" in out["caption_assigned"]          # 3/2/1 split kept
    assert out["value_critwait"] == f"{m['critical_waiting']:,}"
    assert ("metric-bad" in out["critwait_class"]) == (m["critical_waiting"] > 0)
    rules = text_of(out["rules_check"])
    for label in ("Critical leads given to Juniors", "Reps over max capacity", "Rows in = rows out",
                  "Every row routed exactly once", m["fingerprint"], "Reset and run again"):
        assert label in rules
    assert f"{m['rows_in']:,} in → {m['rows_out']:,} out" in rules
    assert "✗" not in rules                                          # every rule held
    assert "Slowest stage" in out["timing_caption"]
    assert out["fig_ch-timings"].data, "stage timing bar is drawn"
    assert "pts Critical vs the reference mix" in text_of(out["tier_caption"])
    assert f"{m['critical_slots']:,}" in text_of(out["tier_caption"])
    assert out["va_leads"] and out["va_reps"]


def test_tier_chart_shows_actual_share_and_reference():
    show(sample())
    fig = dashboard.render()["fig_ch-tiers"]
    names = [trace.name for trace in fig.data]
    assert "This file" in names and any("Reference" in n for n in names)
    assert not any("target" in (n or "").lower() for n in names)


def test_tier_caption_explains_the_critical_gap_with_vip_numbers():
    m = {"tier_share": {"Critical": 13.93}, "expected_tier_share": {"Critical": 10}, "vip_total": 1587,
         "vip_promoted": 1420, "critical_total": 6967, "critical_slots": 14311}
    caption = text_of(dashboard.tier_caption(m))
    assert "+3.9 pts Critical" in caption and "1,587 VIP referrals" in caption and "1,420" in caption
    assert "6,967 Critical leads" in caption and "14,311 Senior + Manager slots" in caption


def test_rules_check_marks_a_broken_rule_with_a_cross():
    m = {"rows_merged_as_duplicates": 0, "ids_renamed": 509, "rows_in": 50000, "rows_out": 50000,
         "junior_critical_violations": 2, "capacity_violations": 0, "every_row_once": True, "fingerprint": "abc"}
    rows = dashboard.rules_check(m).children.children
    assert rows[0].children[0].children == "✗"           # Critical leads given to Juniors: 2
    assert rows[1].children[0].children == "✓"
    assert "509 reused IDs kept as -2" in text_of(rows[2])


def test_fingerprint_is_confirmed_identical_after_reset_and_rerun():
    show(sample())
    first = STATE.result.metrics["fingerprint"]
    click("btn-reset", None, None)
    assert STATE.result is None
    click("btn-sample", None, None, dashboard.MODE_BEST)
    assert STATE.result.metrics["fingerprint"] == first
    assert "Same as the previous sample run" in text_of(dashboard.render()["rules_check"])


# ---------------------------------------------------------------- matching mode toggle
def test_strict_toggle_assigns_fewer_leads_on_sample():
    sample()  # skip early when the data is missing
    click("btn-sample", None, None, dashboard.MODE_BEST)
    best = STATE.result.metrics
    click("btn-sample", None, None, dashboard.MODE_STRICT)
    strict = STATE.result.metrics
    assert strict["min_traits"] == 3 and best["min_traits"] == 1
    assert strict["assigned"] < best["assigned"]
    assert "Strict (3 of 3)" in dashboard.render()["as_of"]


def test_upload_uses_the_toggle_and_a_bad_upload_keeps_the_previous_result():
    show(sample())
    previous = STATE.result
    leads = upload(LEAD_HEADER + "X-1,Acme,NA,EN,Technology,5k,Requested Live Demo,True\n")
    reps = upload(REP_HEADER + "R-1,Ann,Europe,German,Retail,Senior,0,10\n")
    store, alert = click("btn-upload", upload("foo,bar\n1,2\n"), reps, dashboard.MODE_STRICT)
    assert alert.color == "danger" and "missing required column" in alert.children
    assert STATE.result is previous                                   # the previous result stays
    click("btn-upload", leads, reps, dashboard.MODE_STRICT)
    m = STATE.result.metrics
    assert STATE.result.dataset == "upload" and m["min_traits"] == 3
    assert m["assigned"] == 0                                         # no 3-of-3 rep: strict holds
    assert "load" in STATE.result.timings                             # upload reading is timed too
    click("btn-upload", leads, reps, dashboard.MODE_BEST)
    assert STATE.result.metrics["min_traits"] == 1


# ---------------------------------------------------------------- Manager Review tab
def test_review_rows_have_suggested_action_and_critical_first():
    show(sample(min_traits=3))
    rows = dashboard.review_frame("all").to_dict("records")
    assert rows and all(row["suggested_action"] for row in rows)
    ranks = [(dashboard.TIERS.index(r["priority"]), -r["score"]) for r in rows]
    assert ranks == sorted(ranks)
    critical = dashboard.review_frame("critical").to_dict("records")
    assert critical and {r["priority"] for r in critical} == {"Critical"}
    assert len(critical) == STATE.result.metrics["critical_waiting"]


def test_review_chips_count_critical_and_each_reason():
    show(sample(min_traits=3))
    m = STATE.result.metrics
    results, _ = dashboard.frames()
    options = dashboard.review_filter_options(results)
    assert options[0]["label"] == f"All ({m['overflow']:,})"
    assert options[1]["value"] == "critical"
    for reason, count in m["overflow_reasons"].items():
        option = next(o for o in options if o["value"] == f"reason:{reason}")
        assert option["label"].endswith(f"({count:,})")
        assert len(dashboard.review_frame(option["value"])) == count
    assert "suggested action" in text_of(dashboard.review_banner(m))


def test_review_grid_is_paged_on_the_server_with_the_chip_in_the_filter_model():
    show(sample(min_traits=3))
    model = dashboard.review_filters({"run_id": STATE.run_id}, "critical", {})
    assert model["_chip"]["filter"] == "critical" and model["_run"]["filter"] == STATE.run_id
    page = dashboard.review_page({"startRow": 0, "endRow": 5, "filterModel": model, "sortModel": []})
    assert page["rowCount"] == STATE.result.metrics["critical_waiting"]
    assert len(page["rowData"]) == 5
    expected = dashboard.review_frame("critical").head(5)["lead_id"].tolist()
    assert [row["lead_id"] for row in page["rowData"]] == expected       # default Critical-first order
    assert set(page["rowData"][0]) == set(dashboard.REVIEW_COLUMNS)        # only what the grid shows
    by_score = dashboard.review_page({"startRow": 0, "endRow": 50, "filterModel": {},
                                      "sortModel": [{"colId": "score", "sort": "asc"}]})
    scores = [row["score"] for row in by_score["rowData"]]
    assert by_score["rowCount"] == STATE.result.metrics["overflow"] and scores == sorted(scores)


# ---------------------------------------------------------------- lead queue: VIP unknown
def test_vip_unknown_status_option_filters_the_queue():
    show(sample())
    model = dashboard.external_filters({"run_id": STATE.run_id}, "", "all", "all", dashboard.VIP_UNKNOWN, {})
    assert "status" not in model and model["_flag"]["filter"] == dashboard.VIP_UNKNOWN
    results, _ = dashboard.frames()
    page = dashboard.rows_for_request(results, {"startRow": 0, "endRow": 500, "filterModel": model})
    assert page["rowCount"] == STATE.result.metrics["vip_unknown"] > 0
    assert all(VIP_UNKNOWN_TAG in row["notes"] for row in page["rowData"])
    assert "_flag" not in page["rowData"][0]
    model = dashboard.external_filters({"run_id": STATE.run_id}, "", "all", "all", "Assigned", model)
    assert "_flag" not in model and model["status"]["filter"] == "Assigned"


# ---------------------------------------------------------------- Data Quality: every value
def test_value_audit_has_one_collapsible_table_per_field():
    show(sample())
    report = STATE.result.lead_quality
    blocks = dashboard.value_audit(report)
    assert len(blocks) == len(report.value_map)
    region = text_of(blocks[list(report.value_map).index("region")])
    assert "Backup (2nd value)" in region                 # glued regions keep their second value
    size = text_of(blocks[list(report.value_map).index("company_size")])
    assert "k suffix" in size and "How it was written" in size


def test_value_audit_caps_long_tables():
    class Report:
        value_map = {"region": [{"raw": f"r{i}", "cleaned": "Europe", "status": "fixed", "count": 1,
                                 "alternate": ""} for i in range(100)]}
    table = dashboard.value_audit(Report())[0].children[1]
    rows = table.children[1].children
    assert len(rows) == dashboard.VALUE_ROWS_SHOWN + 1 and "+ 60 more values" in text_of(rows[-1])


# ---------------------------------------------------------------- trace a messy lead
def test_trace_picks_the_critical_lead_with_most_fixes():
    result = show(sample())
    lead_id = dashboard.pick_trace_lead(result)
    fixes = {lead.id: len(lead_fixes(lead)) for lead in result.leads}
    critical = [lead for lead in result.leads if lead.priority is Priority.CRITICAL and fixes[lead.id]]
    best = max(fixes[lead.id] for lead in critical)
    assert fixes[lead_id] == best
    assert lead_id == min(lead.id for lead in critical if fixes[lead.id] == best)
    assert dashboard.pick_trace_lead(result) == lead_id == dashboard.trace_lead_id()   # deterministic
    assert dashboard.pick_trace_lead(None) is None


def test_trace_drawer_has_four_step_trail_in_csv_order():
    show(sample())
    body, title = dashboard.lead_detail_view(dashboard.trace_lead_id(), traced=True)
    assert title.startswith("Traced lead:")
    trail = text_of(body[0])
    for step in ("Raw row", "Cleaned", "Scored", "Routed"):
        assert step in trail
    assert [getattr(part, "id", None) for part in body[3:]] == ["trace-raw", "trace-clean", "trace-score",
                                                               "trace-route"]


def test_review_lead_drawer_shows_the_suggestion():
    show(sample(min_traits=3))
    row = dashboard.review_frame("all").to_dict("records")[0]
    body, _ = dashboard.lead_detail_view(row["lead_id"])
    text = text_of(body)
    assert "Suggested next step" in text and row["suggested_action"] in text
    assert dashboard.lead_detail_view("no-such-lead") is None
