"""
Lead Router dashboard: a Salesforce Lightning-style "dream dashboard" for the routing engine.

Run (from the repository root):   python -m app.main   ->   http://127.0.0.1:8050

Five pages share run/reset/upload, matching mode, Trace a messy lead, export and Run Tests:
    /         Dashboard: manager KPIs, tier/reference, region and capacity charts
    /leads    Leads & Reps: paged lead queue, review suggestions and rep tables
    /review   Manager Review: suggestions and manual assignment
    /tests    Tests: real pytest results and illustrated business scenarios
    /about    About: blueprint, run evidence, Rules Check, timings and full data-quality audit

How Dash works, in one paragraph: `layout()` builds the page once. Each `@callback` is a
Python function that Dash runs when one of its Inputs changes (a click, a dropdown), and its
return values are written into its Outputs. Results live on the server in `STATE`; the browser
only receives what it shows. Business logic lives in lead_router/; this file only displays it.
"""
from __future__ import annotations

import base64
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash import ALL, Dash, Input, Output, State, callback, ctx, dcc, html, no_update
from flask import abort, send_file
from werkzeug.utils import secure_filename

from app import charts, test_runner
from app.capacity_simulator import capacity_simulator
from lead_router import scenarios
from app.grid_server import rows_for_request
from lead_router.cleaning import LEAD_COLUMNS, LEAD_REQUIRED, REP_COLUMNS, REP_REQUIRED
from lead_router.config import dataset_paths
from lead_router.explain import FIELD_LABELS, VIP_UNKNOWN_TAG, lead_fixes
from lead_router.export import reps_frame, results_frame, salesforce_frame
from lead_router.models import Assignment, AssignmentStatus, Lead, MatchLevel, OverflowReason, PipelineResult, Priority
from lead_router.review import ReviewError, closest_reps, manual_assign, rep_choices
from lead_router.pipeline import STATE, PipelineError, read_csv, reset_state, run_dataset, \
    run_pipeline_frames

TIERS = charts.TIERS
REGIONS = ["North America", "Europe", "Asia-Pacific", "Unknown"]
PAGES = {
    "/": ("Dashboard", "Lead routing"),
    "/leads": ("Leads & Reps", "Leads & Reps"),
    "/review": ("Manager Review", "Manager Review"),
    "/tests": ("Tests", "Tests: Proof It Works"),
    "/about": ("About", "About Lead Router"),
}
TAB_PAGE = {"queue": "/leads", "review": "/review", "capacity": "/leads",
            "tests": "/tests", "quality": "/about", "about": "/about"}
# the measured pipeline stages shown in the Rules Check bar ("metrics" = working out the KPIs;
# shown too, so the stages add up to the total and the slowest one is named honestly)
TIMED_STAGES = ["load", "clean", "score", "route", "anomaly", "metrics"]
MODE_BEST, MODE_STRICT = 1, 3                        # min_traits for the two matching modes
VIP_UNKNOWN = "VIP unknown"                          # lead queue filter option (Status select)

# ======================================================================================
# Result tables, built once per run and cached (keyed by STATE.run_id)
# ======================================================================================
_CACHE: dict[str, Any] = {"run_id": None, "results": None, "reps": None, "by_id": None, "trace": None}
_CACHE_LOCK = threading.Lock()  # several callbacks fire at once after a run; build the tables only once

# Fingerprint of the last run per (dataset, matching mode). Kept through Reset on purpose: it is
# what lets the Rules Check say "same fingerprint as before Reset" (same input -> same decisions).
_FINGERPRINTS: dict[tuple[str, int], str] = {}
_PREVIOUS_FINGERPRINT: dict[int, str | None] = {}   # run_id -> fingerprint of the run before it


def frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per lead, one row per rep) for the latest run."""
    with _CACHE_LOCK:
        if _CACHE["run_id"] != STATE.run_id:
            results = results_frame(STATE.result)
            results["_search"] = (results["lead_id"] + " " + results["company_name"] + " " +
                                  results["rep_name"].fillna("") + " " + results["rep_id"].fillna("")).str.lower()
            # hidden column behind the "VIP unknown" option of the lead queue's Status filter
            results["_flag"] = results["notes"].str.contains(VIP_UNKNOWN_TAG, regex=False).map(
                {True: VIP_UNKNOWN, False: ""})
            results["ml_flag"] = results["unusual_record"].map(lambda note: "Unusual" if note else "")
            reps = reps_frame(STATE.result)
            reps["ml_flag"] = reps["unusual_record"].map(lambda note: "Unusual" if note else "")
            _CACHE.update(run_id=STATE.run_id, results=results, reps=reps,
                          by_id=None, trace=None)
        return _CACHE["results"], _CACHE["reps"]


def find_lead(lead_id: str) -> tuple[Lead | None, Assignment | None]:
    """The cleaned lead and its routing decision, looked up by ID (index built once per run)."""
    result = STATE.result
    if result is None:
        return None, None
    with _CACHE_LOCK:
        index = _CACHE["by_id"]
        if index is None or index[0] != STATE.run_id:
            decisions = {a.lead_id: a for a in result.assignments}
            index = (STATE.run_id, {lead.id: (lead, decisions.get(lead.id)) for lead in result.leads})
            _CACHE["by_id"] = index
    return index[1].get(lead_id, (None, None))


def pick_trace_lead(result: PipelineResult | None) -> str | None:
    """The lead "Trace lead" opens, chosen the same way every time:
    the Critical lead with the most data fixes (ties: lowest lead ID), else any lead with fixes,
    else the top of the queue. None when there is no run."""
    if result is None or not result.assignments:
        return None
    fix_counts = {lead.id: len(lead_fixes(lead)) for lead in result.leads}
    messy = [lead for lead in result.leads if fix_counts[lead.id]]
    pool = [lead for lead in messy if lead.priority is Priority.CRITICAL] or messy
    if pool:
        return min(pool, key=lambda lead: (-fix_counts[lead.id], lead.id)).id
    return result.assignments[0].lead_id


def remember_fingerprint(run_id: int, result: PipelineResult) -> None:
    """Note this run's fingerprint and keep the one from the previous run of the same dataset and
    matching mode, so the Rules Check can compare them. Uploads are not compared: two different
    files would share the name "upload"."""
    previous = None
    if result.dataset != "upload":
        key = (result.dataset, result.metrics["min_traits"])
        previous = _FINGERPRINTS.get(key)
        _FINGERPRINTS[key] = result.metrics["fingerprint"]
    _PREVIOUS_FINGERPRINT.clear()
    _PREVIOUS_FINGERPRINT[run_id] = previous


def trace_lead_id() -> str | None:
    """pick_trace_lead for the current run, worked out once per run."""
    with _CACHE_LOCK:
        cached = _CACHE["trace"]
        if cached is None or cached[0] != STATE.run_id:
            cached = (STATE.run_id, pick_trace_lead(STATE.result))
            _CACHE["trace"] = cached
    return cached[1]


# ======================================================================================
# Layout building blocks
# ======================================================================================
def report_link(text: str, tab: str, key: str) -> html.Button:
    return html.Button(text, type="button", className="report-link",
                       id={"type": "open-tab", "tab": tab, "key": key})


def component(title: str, body, tab: str | None, report: str | None, key: str) -> html.Div:
    children = [html.Div(html.H2(title, className="component-title"), className="component-head"),
                html.Div(body, className="component-body")]
    if tab and report:
        label = {"queue": "View leads", "review": "View review", "capacity": "View capacity",
                 "quality": "View data quality"}.get(tab, "View details")
        children.append(html.Div(report_link(label, tab, key), className="component-foot"))
    return html.Div(className="component", children=children)


def chart(graph_id: str, height: int = 270) -> dcc.Graph:
    # responsive=True: redraw to the card's width (the canvas is hidden until the first run,
    # so a fixed size measured at page load would be wrong).
    return dcc.Graph(id=graph_id, figure=charts.empty(height), config={"displayModeBar": False},
                     responsive=True, style={"height": f"{height}px", "width": "100%", "overflow": "hidden"})


def metric(key: str, title: str, tab: str | None, report: str | None, small: bool = False) -> html.Div:
    body = html.Div(className="metric-body", children=[
        html.Div("—", id=f"m-{key}", className="metric-value" + (" small" if small else "")),
        html.Div("", id=f"m-{key}-cap", className="metric-caption"),
    ])
    return component(title, body, tab, report, key)


def global_header() -> html.Div:
    return html.Div(className="global-header", children=[
        html.Img(src="/assets/salesforce-logo.svg", alt="Salesforce", className="app-logo"),
        html.Div(className="global-search", children=[
            html.Label("Search leads or reps", htmlFor="global-search", className="visually-hidden"),
            dcc.Input(id="global-search", type="search", placeholder="Search leads or reps", debounce=True, n_submit=0),
        ]),
    ])


def app_nav() -> html.Div:
    """One tab per page. dcc.Link changes the address without reloading the page."""
    return html.Div(className="app-nav", children=[html.Div("Lead Router", className="app-name")] + [
        dcc.Link(label, href=path, className="nav-item", id={"type": "nav", "page": path})
        for path, (label, _) in PAGES.items()
    ])


DASHBOARD_ICON = html.Img(src=("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
                               "viewBox='0 0 24 24' fill='white'><rect x='3' y='12' width='4' height='8' rx='1'/>"
                               "<rect x='10' y='7' width='4' height='13' rx='1'/><rect x='17' y='3' width='4' "
                               "height='17' rx='1'/></svg>"), width=20, height=20)


def page_header() -> html.Div:
    return html.Div(className="page-header", id="top", children=[
        html.Div(className="page-titles", children=[
            html.Span("Dashboard", id="page-eyebrow", className="visually-hidden", **{"aria-hidden": "true"}),
            html.H1("Lead routing", id="page-title", className="page-title"),
            dcc.Loading(type="dot", color="#0176D3", children=html.Div(
                id="as-of", className="page-meta", children="No data yet")),
        ]),
        html.Div(className="page-actions", children=[
            html.Div(className="btn-group", children=[
                html.Button("Run sample", id="btn-sample", className="slds-btn"),
                html.Button("Run scale (50k)", id="btn-scale", className="slds-btn brand"),
                html.Button("Reset", id="btn-reset", className="slds-btn destructive"),
            ]),
            # Matching mode: used by the NEXT run (sample, scale or upload). Best available lets a
            # lead go to a rep sharing 2 or 1 traits when no rep shares all 3; Strict never does.
            html.Div(className="mode-toggle", title="How many of region, language and industry must match. "
                                                    "Strict needs all 3, except that a lead whose industry "
                                                    "is unknown can still match on region + language "
                                                    "(counted as 2/3 under Leads Assigned). "
                                                    "Applies to the next run.", children=[
                html.Span("Matching", className="mode-label"),
                dbc.RadioItems(id="match-mode", value=MODE_BEST, className="btn-group",
                               inputClassName="btn-check", labelClassName="slds-btn",
                               labelCheckedClassName="checked", options=[
                                   {"label": "Best available (3→2→1)", "value": MODE_BEST},
                                   {"label": "Strict (3 of 3)", "value": MODE_STRICT}]),
            ]),
            html.Button("Trace lead", id="btn-trace", className="slds-btn", disabled=True,
                        title="Follow one lead from the raw CSV row to its rep: the Critical lead "
                              "with the most data fixes"),
            html.Details(className="upload-menu", children=[
                html.Summary("Upload CSVs", className="slds-btn"),
                html.Div(className="upload-panel", children=[
                    dcc.Upload(id="up-leads", accept=".csv,text/csv", children=html.Div("Leads CSV", id="up-leads-label", className="upload-chip")),
                    dcc.Upload(id="up-reps", accept=".csv,text/csv", children=html.Div("Reps CSV (optional)", id="up-reps-label", className="upload-chip")),
                    html.Small("No reps file? Uses the scale team.", className="hint"),
                    html.Button("Run upload", id="btn-upload", className="slds-btn", disabled=True),
                ]),
            ]),
            dbc.DropdownMenu(label="Export", id="export-menu", disabled=True, className="export-menu", align_end=True, children=[
                dbc.DropdownMenuItem("Routed leads · CSV", id="exp-results", href="/exports/results.csv", external_link=True),
                dbc.DropdownMenuItem([
                    html.Span("Salesforce template · CSV"),
                    html.Small("Map record IDs before importing", className="export-note"),
                ], id="exp-sf", href="/exports/salesforce.csv", external_link=True),
                dbc.DropdownMenuItem("Manager Review · CSV", id="exp-review", href="/exports/review.csv", external_link=True),
                dbc.DropdownMenuItem("Rep capacity · CSV", id="exp-reps", href="/exports/reps.csv", external_link=True),
            ]),
            html.Button("Run tests", id="btn-tests", className="slds-btn tests-btn",
                        title="Open Tests and run the full automated suite"),
        ]),
    ])


def empty_state() -> html.Div:
    return html.Div(id="empty-state", className="component empty-component", children=[
        html.H3("Start a routing run"),
        html.P("Run sample data or upload your CSVs."),
    ])


def canvas() -> html.Div:
    """At-a-glance Dashboard; detailed run evidence is on About."""
    col = lambda child, lg, md=12: dbc.Col(child, xs=12, md=md, lg=lg)
    return html.Div(id="canvas", style={"display": "none"}, children=dbc.Row(className="canvas-grid", children=[
        dbc.Col(xs=12, children=dbc.Row(className="canvas-grid", children=[
            dbc.Col(metric("assigned", "Assigned leads", "queue", "Lead Queue"), xs=12, md=6, lg=True),
            dbc.Col(metric("review", "Manager Review", "review", "Manager Review"), xs=12, md=6, lg=True),
            dbc.Col(metric("critwait", "Critical waiting", "review", "Manager Review", small=True), xs=12, md=6, lg=True),
            dbc.Col(metric("critical", "Critical to Seniors", "queue", "Lead Queue"), xs=12, md=6, lg=True),
            dbc.Col(metric("util", "Capacity used", "capacity", "Rep capacity"), xs=12, md=6, lg=True),
        ])),
        col(component("Priority tiers", [chart("ch-tiers", 330),
            html.Details(className="chart-caption", children=[html.Summary("Reference comparison"), html.Div(id="tier-caption")])], "queue", "Lead Queue", "tiers"), 6),
        col(component("Leads by region", chart("ch-region"), "queue", "Lead Queue", "region"), 6),
        col(component("Capacity by seniority", chart("ch-capacity", 300), "capacity", "Rep capacity", "top"), 6),
        col(component("Rep utilization", chart("ch-util", 300), "capacity", "Rep capacity", "util-h"), 6),
    ]))


# ---- detail tabs --------------------------------------------------------------------
QUEUE_COLS = [
    {"field": "queue_rank", "headerName": "#", "width": 80, "filter": "agNumberColumnFilter"},
    {"field": "lead_id", "headerName": "Lead", "width": 115},
    {"field": "company_name", "headerName": "Company", "flex": 2, "minWidth": 200},
    {"field": "priority", "headerName": "Tier", "width": 110, "cellRenderer": "TierBadge"},
    {"field": "score", "headerName": "Score", "width": 90, "filter": "agNumberColumnFilter"},
    {"field": "status", "headerName": "Status", "width": 145, "cellRenderer": "StatusPill"},
    {"field": "ml_flag", "headerName": "ML check", "width": 125, "cellRenderer": "MlFlag", "tooltipField": "unusual_record"},
    {"field": "rep_name", "headerName": "Assigned rep", "flex": 1.4, "minWidth": 170},
    {"field": "rep_seniority", "headerName": "Seniority", "width": 110},
    {"field": "region", "headerName": "Region", "width": 135},
    {"field": "language", "headerName": "Language", "width": 110},
    {"field": "industry", "headerName": "Industry", "width": 130},
    {"field": "notes", "headerName": "Notes", "flex": 1.4, "minWidth": 170, "tooltipField": "notes"},
    {"field": "reason", "headerName": "Why this rep?", "flex": 3, "minWidth": 320, "tooltipField": "reason"},
    {"field": "unusual_record", "hide": True},                            # the ML reason (tooltip)
    {"field": "_search", "hide": True},                                   # driven by the search box
    {"field": "_flag", "hide": True},                                     # driven by Status = VIP unknown
    {"field": "_run", "hide": True, "filter": "agNumberColumnFilter"},    # forces a refresh per run
]
GRID_THEME = "ag-theme-quartz"


def _select(select_id: str, label: str, options: list[str], value: str = "all", all_label: str = "All"):
    opts = ([{"label": all_label, "value": "all"}] if all_label else []) + [{"label": o, "value": o} for o in options]
    return dbc.InputGroup([html.Label(label, htmlFor=select_id, className="input-group-text"), dbc.Select(id=select_id, options=opts, value=value)], size="sm")


def queue_tab() -> html.Div:
    return html.Div([
        dbc.Row(className="g-2 mb-2 toolbar", children=[
            dbc.Col(dbc.Input(id="q-search", placeholder="Search lead, company or rep...", type="search",
                              debounce=True, size="sm"), xs=12, md=3),
            dbc.Col(_select("q-tier", "Tier", TIERS), xs=6, md=2),
            dbc.Col(_select("q-region", "Region", REGIONS), xs=6, md=2),
            dbc.Col(_select("q-status", "Status", ["Assigned", "Manager Review", VIP_UNKNOWN]), xs=6, md=2),
            dbc.Col(dbc.InputGroup([html.Label("Group by", htmlFor="q-group", className="input-group-text"), dbc.Select(id="q-group", value="none", options=[
                {"label": "None", "value": "none"}, {"label": "Tier", "value": "priority"},
                {"label": "Rep", "value": "rep"}])], size="sm"), xs=6, md=3),
            dbc.Col(dbc.InputGroup([dbc.InputGroupText("ML check"), dbc.Select(id="q-ml", value="all", options=[
                {"label": "All leads", "value": "all"},
                {"label": "⚠ Flagged as unusual", "value": "Unusual"}])], size="sm", className="ml-filter"),
                    xs=12, md=4),
        ]),
        html.Div(id="group-wrap", style={"display": "none"}, children=dag.AgGrid(
            id="group-grid", columnDefs=[], rowData=[], className=GRID_THEME,
            defaultColDef={"sortable": True, "filter": True, "resizable": True},
            dashGridOptions={"pagination": True, "paginationPageSize": 20}, style={"height": "520px"})),
        html.Div(id="queue-wrap", children=dag.AgGrid(
            id="queue-grid", rowModelType="infinite", columnDefs=QUEUE_COLS, className=GRID_THEME,
            defaultColDef={"sortable": True, "resizable": True, "filter": "agTextColumnFilter",
                           "floatingFilter": True, "suppressHeaderMenuButton": True},
            dashGridOptions={"pagination": True, "paginationPageSize": 50, "cacheBlockSize": 50,
                             "maxBlocksInCache": 10, "tooltipShowDelay": 300,
                                    # Superseded Dash requests must not exhaust AG Grid loading slots.
                                    "maxConcurrentDatasourceRequests": -1, "blockLoadDebounceMillis": 100,
                             "rowSelection": {"mode": "singleRow", "checkboxes": False, "enableClickSelection": True}},
            filterModel={"_run": {"filterType": "number", "type": "equals", "filter": 0}},
            style={"height": "520px"})),
    ])


def review_layout() -> html.Div:
    """Leads waiting for a manager (left) and the assign panel for the selected lead (right)."""
    cols = [
        {"field": "lead_id", "headerName": "Lead", "width": 115, "pinned": "left"},
        {"field": "company_name", "headerName": "Company", "flex": 2, "minWidth": 200},
        {"field": "priority", "headerName": "Tier", "width": 110, "cellRenderer": "TierBadge"},
        {"field": "score", "headerName": "Score", "width": 90, "filter": "agNumberColumnFilter"},
        {"field": "overflow_reason", "headerName": "Why it's here", "flex": 1.5, "minWidth": 210},
        {"field": "suggested_action", "headerName": "Suggested action", "flex": 3.5, "minWidth": 380,
         "tooltipField": "suggested_action"},
        {"field": "region", "headerName": "Region", "width": 135},
        {"field": "language", "headerName": "Language", "width": 110},
        {"field": "industry", "headerName": "Industry", "width": 130},
        {"field": "reason", "headerName": "What happened", "flex": 3, "minWidth": 320, "tooltipField": "reason"},
        # hidden: AG Grid only keeps filters on columns it knows, and a filter change is what
        # makes it fetch fresh pages (new run -> _run, chip click -> _chip)
        {"field": "_chip", "hide": True},
        {"field": "_run", "hide": True, "filter": "agNumberColumnFilter"},
    ]
    queue = html.Div([
        html.H2(id="review-count", className="component-title"),
        html.Div(id="review-banner", className="review-banner mb-2"),
        dbc.RadioItems(id="review-filter", value="all", options=[{"label": "All", "value": "all"}],
                       className="chip-group mb-2", inputClassName="btn-check", labelClassName="chip",
                       labelCheckedClassName="checked", inline=True),
        # Server-side pages (like the lead queue): a run can leave thousands of leads in review, and
        # the browser only ever receives the 20 rows on screen.
        dag.AgGrid(id="review-grid", columnDefs=cols, rowModelType="infinite", className=GRID_THEME,
                   defaultColDef={"sortable": True, "filter": "agTextColumnFilter", "floatingFilter": True,
                                  "resizable": True, "suppressHeaderMenuButton": True},
                   dashGridOptions={"pagination": True, "paginationPageSize": 20, "cacheBlockSize": 20,
                                    "maxBlocksInCache": 10, "tooltipShowDelay": 300,
                                    # Superseded Dash requests must not exhaust AG Grid loading slots.
                                    "maxConcurrentDatasourceRequests": -1, "blockLoadDebounceMillis": 100,
                                    "rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                     "enableClickSelection": True}},
                   filterModel={"_run": {"filterType": "number", "type": "equals", "filter": 0}},
                   style={"height": "500px"}),
    ])
    return html.Div(className="page-stack", children=[
        dbc.Row(className="canvas-grid", children=[
        dbc.Col(html.Div(className="component", children=html.Div(queue, className="component-body review-body")), xs=12, xl=7),
        dbc.Col(html.Div(className="component", children=[
            html.Div(className="component-head", children=html.H2("Assign this lead", className="component-title")),
            html.Div(className="component-body review-panel", children=[html.Div(id="review-msg"), html.Div(id="review-panel")]),
        ]), xs=12, xl=5),
        ]),
    ])


def capacity_tab() -> html.Div:
    cols = [
        {"field": "rep_id", "headerName": "Rep", "width": 105},
        {"field": "name", "headerName": "Name", "flex": 1.5, "minWidth": 180},
        {"field": "seniority", "headerName": "Seniority", "width": 110},
        {"field": "region", "headerName": "Region", "width": 135},
        {"field": "language", "headerName": "Language", "width": 110},
        {"field": "industry", "headerName": "Expertise", "width": 130},
        {"field": "load", "headerName": "Load vs. Max", "minWidth": 200, "flex": 1.5, "cellRenderer": "CapacityBar",
         "filter": "agNumberColumnFilter"},
        {"field": "utilization", "headerName": "Used %", "width": 100, "filter": "agNumberColumnFilter"},
        {"field": "available", "headerName": "Open Slots", "width": 110, "filter": "agNumberColumnFilter"},
        {"field": "ml_flag", "headerName": "ML check", "width": 125, "cellRenderer": "MlFlag",
     "tooltipField": "unusual_record"},
        {"field": "data_fixes", "headerName": "What we fixed in this row", "flex": 1.5, "minWidth": 220,
         "tooltipField": "data_fixes"},
    ]
    return html.Div([
        capacity_simulator(),
        dbc.Row(className="g-2 mb-2 toolbar", children=[
            dbc.Col(_select("c-region", "Region", REGIONS), xs=6, md=3),
            dbc.Col(_select("c-seniority", "Seniority", ["Junior", "Senior", "Manager"]), xs=6, md=3),
            dbc.Col(dbc.InputGroup([html.Label("Sort", htmlFor="c-sort", className="input-group-text"), dbc.Select(id="c-sort", value="most", options=[
                {"label": "Most loaded", "value": "most"}, {"label": "Least loaded", "value": "least"},
                {"label": "Most open slots", "value": "open"}])], size="sm"), xs=12, md=3),
            dbc.Col(dbc.InputGroup([dbc.InputGroupText("ML check"), dbc.Select(id="c-ml", value="all", options=[
                {"label": "All reps", "value": "all"},
                {"label": "⚠ Flagged as unusual", "value": "Unusual"}])], size="sm", className="ml-filter"),
                    xs=12, md=3),
        ]),
        dag.AgGrid(id="rep-grid", columnDefs=cols, rowData=[], className=GRID_THEME,
                   defaultColDef={"sortable": True, "filter": True, "resizable": True},
                   dashGridOptions={"pagination": True, "paginationPageSize": 20, "tooltipShowDelay": 300},
                   style={"height": "500px"}),
    ])


def quality_tab() -> html.Div:
    return html.Div([
        dbc.Row([
            dbc.Col([html.H3("Leads", className="component-title mb-2"), html.Div(id="dq-leads")], lg=6),
            dbc.Col([html.H3("Representatives", className="component-title mb-2"), html.Div(id="dq-reps")], lg=6),
        ]),
        html.H3("Source values", className="component-title mt-3 mb-1"),
        dbc.Row([
            dbc.Col([html.H4("Leads", className="sub-title"), html.Div(id="va-leads")], lg=6),
            dbc.Col([html.H4("Representatives", className="sub-title"), html.Div(id="va-reps")], lg=6),
        ]),
    ])


def about_page() -> html.Div:
    """The About page: how good the matches are, how fast it runs, how it works, and the data clean-up."""
    step = lambda n, title, text: html.Div(className="about-step", children=[
        html.P(n, className="about-step-n"), html.Div([html.H3(title, className="about-step-title"),
                                                      html.P(text, className="about-step-text")])])
    how = html.Details(className="component about-how", children=[
        html.Summary("How routing works", className="section-summary"),
        html.Div(className="about-steps", children=[
            step("1", "Clean", "Normalizes values and preserves distinct source rows."),
            step("2", "Score", "Points for company size, activity and industry → Critical, High, Medium or Low. "
                               "VIP referrals are always Critical."),
            step("3", "Route", "Most urgent first, to the best-matching rep with room. Critical → Senior → Manager, "
                               "never Junior. Equal reps share work evenly."),
            step("4", "Double-check (ML)", "A machine-learning model flags records that look unusual, like a company "
                                           "with 10 billion employees, so a person can check them. It never changes a route."),
        ]),
    ])
    quality = html.Div(id="data-quality", className="component", children=[
        html.Div(className="component-head", children=html.H2("Data quality", className="component-title")),
        html.Div(quality_tab(), className="component-body about-quality"),
    ])
    unusual = html.Div(className="component", children=[
        html.Div(className="component-head", children=html.H2(
            "Unusual records, found by machine learning", className="component-title")),
        html.Div(className="component-body about-quality", children=[
            html.P(["An ", html.B("Isolation Forest"), " model (scikit-learn) learns what normal leads and reps look like "
                    "and flags the rows that stand out most, each with the reason. It needs no training answers. "
                    "It only adds a note for a person to check: scores, priorities and routes are never changed, "
                    "so every business rule still holds."], className="hint mb-2"),
            html.Div(id="ml-summary", className="ml-summary"),
            html.P(["See them in context: ", html.B("Leads & Reps → ML check: ⚠ Flagged as unusual"),
                    " (works on both the Lead Queue and Rep Capacity tabs)."], className="hint mb-2"),
            dbc.Row([
                dbc.Col([html.H3("Most unusual leads", className="about-sub mb-2"), html.Div(id="ml-leads")], lg=7),
                dbc.Col([html.H3("Most unusual reps", className="about-sub mb-2"), html.Div(id="ml-reps")], lg=5),
            ]),
        ]),
    ])
    return html.Div(className="page-stack", children=[
        how,
        html.Div(id="about-data", style={"display": "none"}, className="page-stack", children=[
            dbc.Row(className="canvas-grid", children=[
                dbc.Col(metric("matches", "Exact matches", "queue", "Lead Queue"), xs=12, md=6, lg=3),
                dbc.Col(metric("fairness", "Distribution fairness", "capacity", "Rep capacity"), xs=12, md=6, lg=3),
                dbc.Col(metric("speed", "Leads per second", None, None), xs=12, md=6, lg=3),
                dbc.Col(metric("fixed", "Values corrected", "quality", "Data Quality"), xs=12, md=6, lg=3),
                dbc.Col(component("Rules Check", dbc.Row(className="g-3 px-2", children=[
                    dbc.Col(html.Div(id="rules-check"), xs=12, lg=7),
                    dbc.Col([html.H3("Stage timings", className="sub-title"),
                             chart("ch-timings", 120), html.Div(id="timing-caption", className="chart-caption")], xs=12, lg=5),
                ]), "capacity", "Rep capacity", "rules"), xs=12),
                dbc.Col(component("Routing funnel", chart("ch-funnel"), "queue", "Lead Queue", "funnel"), xs=12, lg=6),
                dbc.Col(component("Review reasons", chart("ch-reasons"), "review", "Manager Review", "reasons"), xs=12, lg=6),
                dbc.Col(component("Assignments by priority", chart("ch-destination"), "capacity", "Rep capacity", "dest"), xs=12, lg=6),
                dbc.Col(component("Assignment status", chart("ch-split"), "review", "Manager Review", "split"), xs=12, lg=6),
            ]),
            unusual,
            quality,
        ]),
    ])


def details() -> html.Div:
    return html.Div(id="details", className="component details", style={"display": "none"}, children=
        dbc.Tabs(id="tabs", active_tab="queue", children=[
            dbc.Tab(queue_tab(), label="Lead queue", tab_id="queue"),
            dbc.Tab(capacity_tab(), label="Rep capacity", tab_id="capacity"),
        ]))


def tests_page() -> html.Div:
    """Runs the real pytest suite on demand. Results are grouped by what they prove (app/test_runner.py)."""
    return html.Div(className="page-stack", children=[
        html.Div(className="component", children=[
            html.Div(className="component-head", children=html.H2("Is Lead Router really working?", className="component-title")),
            html.Div(className="component-body tests-intro", children=[
                html.P(["Run the full automated suite. Results show what passed and what needs attention."],
                       className="mb-2"),
                html.Button("Run all tests", id="tests-run", className="slds-btn brand"),
                html.Span("Runs separately; the active routing run stays unchanged.", className="hint ms-2"),
            ]),
        ]),
        dcc.Loading(type="circle", color="#0176D3", children=html.Div(id="tests-body", className="page-stack")),
    ])


def layout() -> html.Div:
    return html.Div([
        global_header(),
        app_nav(),
        page_header(),
        dcc.Location(id="url", refresh="callback-nav"),  # the address bar: one address per page
        html.Div(className="canvas", children=[
            html.Div(id="alert-area"),
            empty_state(),
            html.Div(id="page-dashboard", children=canvas()),
            html.Div(id="page-leads", style={"display": "none"}, children=details()),
            html.Div(id="page-review", style={"display": "none"}, children=review_layout()),
            html.Div(id="page-tests", style={"display": "none"}, children=tests_page()),
            html.Div(id="page-about", style={"display": "none"}, children=about_page()),
        ]),
        dbc.Offcanvas(id="detail", title="Lead detail", placement="end", is_open=False, style={"width": "min(100%, 480px)"}),
        dcc.Store(id="run-store", data={"run_id": 0}),
        dcc.Store(id="up-leads-store"),
        dcc.Store(id="up-reps-store"),
        dcc.Store(id="scroll-done"),  # dummy output for the scroll-to-top callback
        dcc.Store(id="tests-trigger"),  # bumps when the tests should (re)run
    ])


app = Dash(__name__, title="Lead routing", external_stylesheets=[dbc.themes.BOOTSTRAP],
           suppress_callback_exceptions=True)  # some components live in tabs not rendered yet
app.layout = layout
server = app.server  # for a production server such as gunicorn / waitress


# ======================================================================================
# Callbacks: run / reset / upload
# ======================================================================================
def _decode_upload(contents: str, label: str, expected, required) -> pd.DataFrame:
    try:
        _, b64 = contents.split(",", 1)
        buffer = io.BytesIO(base64.b64decode(b64))
    except Exception:  # noqa: BLE001 - any decoding problem means a bad upload
        raise PipelineError(f"Could not read the uploaded {label} file.") from None
    return read_csv(buffer, label, expected, required)


@callback(Output("up-leads-store", "data"), Output("up-leads-label", "children"), Output("btn-upload", "disabled"),
          Input("up-leads", "contents"), State("up-leads", "filename"), prevent_initial_call=True)
def stash_leads(contents, filename):
    if not filename or not filename.lower().endswith(".csv"):
        return None, "⚠ Leads must be .csv", True
    return contents, f"✓ {filename}", False


@callback(Output("up-reps-store", "data"), Output("up-reps-label", "children"),
          Input("up-reps", "contents"), State("up-reps", "filename"), prevent_initial_call=True)
def stash_reps(contents, filename):
    if not filename or not filename.lower().endswith(".csv"):
        return None, "⚠ Reps must be .csv"
    return contents, f"✓ {filename}"


@callback(
    Output("run-store", "data"), Output("alert-area", "children"),
    Input("btn-sample", "n_clicks"), Input("btn-scale", "n_clicks"), Input("btn-reset", "n_clicks"),
    Input("btn-upload", "n_clicks"), State("up-leads-store", "data"), State("up-reps-store", "data"),
    State("match-mode", "value"),
    prevent_initial_call=True,
    running=[(Output("btn-sample", "disabled"), True, False), (Output("btn-scale", "disabled"), True, False),
             (Output("btn-reset", "disabled"), True, False)],
)
def run_or_reset(_s, _sc, _r, _u, up_leads, up_reps, mode=MODE_BEST):
    """Runs when one of the buttons is clicked (ctx.triggered_id says which). Errors become an alert.
    `mode` is the matching toggle: the minimum traits (1 or 3) this run requires."""
    trigger = ctx.triggered_id
    if trigger == "btn-reset":
        reset_state()
        return {"run_id": STATE.run_id}, None
    min_traits = MODE_STRICT if str(mode) == str(MODE_STRICT) else MODE_BEST
    try:
        if trigger == "btn-upload":
            if not up_leads:
                raise PipelineError("Upload a leads CSV first.")
            start = time.perf_counter()  # reading the uploaded files is the "load" stage
            leads_df = _decode_upload(up_leads, "Leads", LEAD_COLUMNS, LEAD_REQUIRED)
            reps_df = (_decode_upload(up_reps, "Reps", REP_COLUMNS, REP_REQUIRED) if up_reps
                       else read_csv(dataset_paths("scale")[1], "Reps", REP_COLUMNS, REP_REQUIRED))
            result = run_pipeline_frames(leads_df, reps_df, dataset="upload", min_traits=min_traits,
                                         timings={"load": time.perf_counter() - start})
        else:
            result = run_dataset("sample" if trigger == "btn-sample" else "scale", min_traits=min_traits)
    except (PipelineError, KeyError) as error:
        return no_update, dbc.Alert(str(error), color="danger", dismissable=True)
    remember_fingerprint(STATE.set(result), result)
    frames()  # build the result tables now, before the redraw callbacks ask for them
    warnings = result.lead_quality.warnings + result.rep_quality.warnings
    return {"run_id": STATE.run_id}, (dbc.Alert(" ".join(warnings), color="warning", dismissable=True) if warnings else None)


# ======================================================================================
# Callback: redraw the whole dashboard after a run or reset
# ======================================================================================
METRIC_KEYS = ["assigned", "review", "critwait", "critical", "fairness", "matches", "speed", "util", "fixed"]
CHART_IDS = ["ch-tiers", "ch-funnel", "ch-region", "ch-destination", "ch-reasons", "ch-capacity", "ch-util",
             "ch-timings", "ch-split"]
# Everything render() writes, by name. Callers (and tests) get a dict with these keys.
RENDER_OUTPUTS = {
    "empty_style": Output("empty-state", "style"), "canvas_style": Output("canvas", "style"),
    "details_style": Output("details", "style"), "about_style": Output("about-data", "style"), "as_of": Output("as-of", "children"),
    **{f"value_{k}": Output(f"m-{k}", "children") for k in METRIC_KEYS},
    **{f"caption_{k}": Output(f"m-{k}-cap", "children") for k in METRIC_KEYS},
    "critwait_class": Output("m-critwait", "className"),
    **{f"fig_{c}": Output(c, "figure") for c in CHART_IDS},
    "tier_caption": Output("tier-caption", "children"), "rules_check": Output("rules-check", "children"),
    "timing_caption": Output("timing-caption", "children"),
    "review_label": Output("review-count", "children"), "review_banner": Output("review-banner", "children"),
    "review_filter_options": Output("review-filter", "options"), "review_filter_value": Output("review-filter", "value"),
    "dq_leads": Output("dq-leads", "children"), "dq_reps": Output("dq-reps", "children"),
    "va_leads": Output("va-leads", "children"), "va_reps": Output("va-reps", "children"),
    "trace_disabled": Output("btn-trace", "disabled"),
    "export_disabled": Output("export-menu", "disabled"),
    "ml_summary": Output("ml-summary", "children"), "ml_leads": Output("ml-leads", "children"),
    "ml_reps": Output("ml-reps", "children")
}
METRIC_CLASS = "metric-value small"


@callback(output=RENDER_OUTPUTS, inputs=[Input("run-store", "data")])
def render(_store=None) -> dict[str, Any]:
    r = STATE.result
    if r is None:
        blank = charts.empty()
        return {
            "empty_style": {}, "canvas_style": {"display": "none"}, "details_style": {"display": "none"}, "about_style": {"display": "none"},
            "as_of": "No data yet",
            **{f"value_{k}": "—" for k in METRIC_KEYS}, **{f"caption_{k}": "" for k in METRIC_KEYS},
            "critwait_class": METRIC_CLASS,
            **{f"fig_{c}": blank for c in CHART_IDS},
            "tier_caption": None, "rules_check": None, "timing_caption": None,
            "review_label": "Manager Review", "review_banner": None,
            "review_filter_options": [{"label": "All", "value": "all"}], "review_filter_value": "all",
            "dq_leads": None, "dq_reps": None, "va_leads": None, "va_reps": None, "trace_disabled": True, "export_disabled": True, "ml_summary": None, "ml_leads": None, "ml_reps": None,
        }

    m, (results, reps) = r.metrics, frames()
    lq, rq = r.lead_quality, r.rep_quality
    seconds = r.timings["total"]

    as_of = (f"{r.dataset.title()} · {m['total_leads']:,} {'lead' if m['total_leads'] == 1 else 'leads'} · {seconds:.2f} s · "
             f"Matching: {m['matching_mode']}")

    # --- match quality: 3 of 3 traits vs. 2 of 3 (see routing.pools_to_try)
    levels = m["match_levels"]
    by_traits = {3: 0, 2: 0, 1: 0}
    for name, count in levels.items():
        by_traits[MatchLevel(name).traits_matched] += count

    # --- big numbers: (value, caption)
    waiting = m["critical_waiting"]
    metrics = {
        "assigned": (f"{m['assigned']:,}", f"{m['assigned_rate']}% of leads"),
        "review": (f"{m['overflow']:,}", f"{m['overflow_rate']}% of leads"),
        "critwait": (f"{waiting:,}", "Needs an owner" if waiting else "All Critical leads assigned"),
        "critical": (f"{m['critical_senior_rate']}%",
                     html.Span([f"{m['critical_to_senior']:,} of {m['critical_total']:,} · ",
                                html.Span(f"{m['junior_critical_violations']} given to Juniors", className="metric-good")])),
        # captions kept short (two lines max) so the big numbers line up across the row
        "speed": (f"{m['throughput_lps']:,}", f"{m['total_leads']:,} leads in {seconds:.2f} s"),
        "util": (f"{m['utilization']}%", f"{m['load_total']:,} of {m['capacity_total']:,} slots · "
                                         f"{m['capacity_violations']} over capacity"),
        "fixed": (f"{lq.fixed + rq.fixed:,}", f"{m['imputed_rate']}% of leads had missing data"),
        # Share of assigned leads whose rep matches region, language AND industry
        "matches": (f"{100 * by_traits[3] / max(m['assigned'], 1):.1f}%",
                    html.Details(children=[html.Summary("3 of 3 traits"),
                        f"3/3: {by_traits[3]:,} · 2/3: {by_traits[2]:,} · 1/3: {by_traits[1]:,}"])),
    }

    metrics["fairness"] = (f"{m['fairness_during_run']:.3f}", "Jain index · 1.000 = perfectly even")

    # --- charts
    reasons = m["overflow_reasons"]
    stages = [
        ("Leads uploaded", lq.rows_in),
        ("Kept after cleanup", m["total_leads"]),
        ("Given to a rep", m["assigned"]),
    ]
    region_counts = results["region"].value_counts().reindex(REGIONS, fill_value=0)
    region_counts = region_counts[region_counts > 0]
    if m["overflow"]:
        reason_fig = charts.donut(list(reasons), list(reasons.values()), f"{m['overflow']:,}", "Reason",
                                  [charts.ORANGE, charts.PEACH, charts.TEAL, charts.LIGHT_TEAL])
    else:
        reason_fig = charts.donut(["No overflow"], [1], "0", "Reason", [charts.LIGHT_GREY])
        reason_fig.update_traces(textinfo="none", hoverinfo="skip")
    timed = [s for s in TIMED_STAGES if s in r.timings]
    figures = {
        "ch-tiers": charts.tier_vs_reference(m["tiers"], m["tier_share"], m["expected_tier_share"]),
        "ch-funnel": charts.routing_funnel(stages),
        "ch-region": charts.donut(list(region_counts.index), list(region_counts.values),
                                  f"{m['total_leads']:,}", "Region"),
        "ch-destination": charts.destination_columns(results),
        "ch-reasons": reason_fig,
        "ch-capacity": charts.capacity_by_seniority(reps),
        "ch-util": charts.rep_fullness(reps),
        "ch-split": charts.assigned_vs_review(m["assigned"], m["overflow"]),
        "ch-timings": charts.stage_timings(r.timings, timed),
    }

    return {
        "empty_style": {"display": "none"}, "canvas_style": {}, "details_style": {}, "about_style": {}, "as_of": as_of,
        **{f"value_{k}": metrics[k][0] for k in METRIC_KEYS}, **{f"caption_{k}": metrics[k][1] for k in METRIC_KEYS},
        "critwait_class": METRIC_CLASS + (" metric-bad" if waiting else ""),
        **{f"fig_{c}": figures[c] for c in CHART_IDS},
        "tier_caption": tier_caption(m),
        "rules_check": rules_check(m, _PREVIOUS_FINGERPRINT.get(STATE.run_id), r.dataset),
        "timing_caption": timing_caption(r.timings, timed),
        "review_label": f"Manager Review ({m['overflow']:,})", "review_banner": review_banner(m),
        "review_filter_options": review_filter_options(results), "review_filter_value": "all",
        "dq_leads": _quality_table(lq), "dq_reps": _quality_table(rq),
        "va_leads": value_audit(lq), "va_reps": value_audit(rq),
        "trace_disabled": False, "export_disabled": False, **dict(zip(("ml_summary", "ml_leads", "ml_reps"), _unusual_view(r))),
    }


# ---- pieces of render(), kept separate so each one is easy to read and to test -------
def tier_caption(m: dict[str, Any]) -> list:
    """Why our tier mix differs from the cheat sheet's reference, and Critical demand vs capacity."""
    lines = []
    reference = m.get("expected_tier_share") or {}
    if "Critical" in reference:
        gap = m["tier_share"].get("Critical", 0.0) - reference["Critical"]
        if m["vip_total"]:
            why = (f"{m['vip_total']:,} VIP referrals are always Critical, {m['vip_promoted']:,} of them "
                   "would otherwise score lower")
        else:
            why = "no VIP referrals in this file, so the tiers come from points alone"
        lines.append(html.Div(f"{gap:+.1f} pts Critical vs the reference mix: {why}."))
    lines.append(html.Div([html.B(f"{m['critical_total']:,}"), " Critical leads · ",
                           html.B(f"{m['critical_slots']:,}"), " Senior + Manager slots (the only reps "
                           "allowed to take them)"]))
    return lines


def _check_row(state: str, label: str, value, hint: str | None = None) -> html.Tr:
    icon = {"ok": "✓", "bad": "✗", "info": "#"}[state]
    return html.Tr([html.Td(icon, className=f"check-icon check-{state}"), html.Td(label, className="check-label"),
                    html.Td([html.Div(value, className="check-value"),
                             html.Div(hint, className="hint") if hint else None])])


def rules_check(m: dict[str, Any], previous_fingerprint: str | None = None, dataset: str = "") -> html.Table:
    """The routing rules, checked on this run's actual decisions: ✓ held, ✗ broken."""
    merged, renamed = m["rows_merged_as_duplicates"], m["ids_renamed"]
    rows_note = f"{m['rows_in']:,} in → {m['rows_out']:,} out"
    if renamed:
        rows_note += f" · {renamed:,} reused ID{'s' if renamed != 1 else ''} kept as -2"
    if merged:
        rows_note += f" · {merged:,} exact cop{'ies' if merged != 1 else 'y'} merged"
    juniors, over = m["junior_critical_violations"], m["capacity_violations"]
    fingerprint = m["fingerprint"]
    if previous_fingerprint is None:
        fp_state, fp_hint = "info", "Reset and run again to compare placements."
    elif previous_fingerprint == fingerprint:
        fp_state, fp_hint = "ok", (f"Same as the previous {dataset} run in this mode: every lead got the same status, rep and match level. "
                                   "Reset and run again to compare placements.")
    else:
        fp_state, fp_hint = "info", (f"The previous {dataset} run in this mode gave {previous_fingerprint}: "
                                     "the input file or the rules changed since.")
    return html.Table(className="rules-table w-100", children=html.Tbody([
        _check_row("ok" if juniors == 0 else "bad", "Critical leads given to Juniors", f"{juniors:,}"),
        _check_row("ok" if over == 0 else "bad", "Reps over max capacity", f"{over:,}"),
        _check_row("ok" if m["rows_in"] == m["rows_out"] + merged else "bad", "Rows in = rows out", rows_note),
        _check_row("ok" if m["every_row_once"] else "bad", "Every row routed exactly once",
                   ("Yes: one decision per lead, none lost or doubled"
                    + (f" (after merging {merged:,} exact cop{'ies' if merged != 1 else 'y'})" if merged else ""))
                   if m["every_row_once"] else "No"),
        _check_row(fp_state, "Run fingerprint", html.Code(fingerprint, className="fingerprint"), fp_hint),
    ]))


def timing_caption(timings: dict[str, float], stages: list[str]) -> str:
    if not stages:
        return ""
    slowest = max(stages, key=lambda s: timings[s])
    return f"Slowest stage: {slowest} ({timings[slowest]:.2f} s of {timings['total']:.2f} s in total)."


def review_banner(m: dict[str, Any]) -> list:
    return ["Suggestions need manager approval."] if m["overflow"] else ["All leads assigned."]


REVIEW_COLUMNS = ["lead_id", "company_name", "priority", "score", "overflow_reason", "suggested_action",
                  "region", "language", "industry", "reason"]  # only what the table shows: keeps the page light
_TIER_RANK = {tier: rank for rank, tier in enumerate(TIERS)}


def review_filter_options(results: pd.DataFrame) -> list[dict]:
    """Quick-filter chips for the Manager Review tab: All, Critical only, then one per reason."""
    review = results[results["status"] == "Manager Review"]
    options = [{"label": f"All ({len(review):,})", "value": "all"},
               {"label": f"Critical only ({int((review['priority'] == 'Critical').sum()):,})", "value": "critical"}]
    for reason, count in review["overflow_reason"].value_counts().items():
        options.append({"label": f"{reason} ({count:,})", "value": f"reason:{reason}"})
    return options


def review_frame(choice: str | None = "all") -> pd.DataFrame:
    """Manager Review leads for the chosen chip, Critical first, then highest score."""
    results, _ = frames()
    review = results[results["status"] == "Manager Review"]
    if choice == "critical":
        review = review[review["priority"] == "Critical"]
    elif choice and choice.startswith("reason:"):
        review = review[review["overflow_reason"] == choice.removeprefix("reason:")]
    review = review.assign(_rank=review["priority"].map(_TIER_RANK)).sort_values(
        ["_rank", "score", "lead_id"], ascending=[True, False, True], kind="stable")
    return review[REVIEW_COLUMNS]


def review_page(request: dict) -> dict:
    """One page of the Manager Review grid. The chip choice travels in the filterModel as "_chip"
    (not a real column, so the column filters ignore it); with no column sorted, the default
    Critical-first order is kept."""
    choice = ((request.get("filterModel") or {}).get("_chip") or {}).get("filter", "all")
    return rows_for_request(review_frame(choice), request)


@callback(Output("review-grid", "filterModel"),
          Input("run-store", "data"), Input("review-filter", "value"), State("review-grid", "filterModel"))
def review_filters(store, choice, current):
    """A new run or chip changes the filterModel, which makes the grid fetch fresh pages."""
    model = dict(current or {})
    model["_chip"] = {"filterType": "text", "type": "equals", "filter": choice or "all"}
    model["_run"] = {"filterType": "number", "type": "equals", "filter": (store or {}).get("run_id", 0)}
    return model


@callback(Output("review-grid", "getRowsResponse"), Input("review-grid", "getRowsRequest"))
def review_rows(request):
    if not request:
        return no_update
    return review_page(request)


def _quality_table(report) -> list:
    """Plain-English data quality: how many values per field we cleaned up, filled in or couldn't read,
    with real examples from the file."""
    rows = [{
        "Field": FIELD_LABELS.get(name, name.replace("_", " ").title()),
        "Cleaned up": f"{v.get('fixed', 0):,}",
        "Blank (filled in)": f"{v.get('imputed', 0):,}",
        "Unreadable": f"{v.get('invalid', 0):,}",
        "Examples from the file": " · ".join(e.replace(" -> ", " → ") for e in report.examples.get(name, [])) or "—",
    } for name, v in report.by_field.items()]
    summary = html.Ul(className="hint", children=[
        html.Li(f"{report.rows_in:,} rows in the file → {report.rows_out:,} kept. Nothing was thrown away "
                "except exact copies of the same row."),
        html.Li(f"{report.duplicates_removed:,} exact copies merged · {report.duplicate_ids_renamed:,} reused IDs "
                f"renamed so both records are kept · {report.possible_duplicates:,} look-alike rows flagged."),
        html.Li("Cleaned up = fixed spelling, spacing or format (e.g. \"EUR\" → Europe). "
                "Blank = no value given, so a safe default was used. "
                "Unreadable = junk we couldn't interpret, treated as unknown."),
    ])
    return [summary, dbc.Table.from_dataframe(pd.DataFrame(rows), striped=True, hover=True, size="sm",
                                              className="dq-table")]


# status from cleaning -> (badge text, css class)
VALUE_STATUS = {"ok": ("as is", "vs-ok"), "fixed": ("fixed", "vs-fixed"), "missing": ("missing", "vs-missing"),
                "invalid": ("unreadable", "vs-invalid")}
VALUE_ROWS_SHOWN = 40   # an uploaded file could hold thousands of spellings; keep each table light


def _raw_text(value: Any) -> html.Code:
    """A raw CSV value exactly as written (spaces kept visible), or (blank)."""
    return html.Code("(blank)" if value in (None, "") else str(value), className="raw-value")


def _status_badge(status: str) -> html.Span:
    text, css = VALUE_STATUS.get(status, (status, "vs-ok"))
    return html.Span(text, className=f"vs-badge {css}")


def value_audit(report) -> list:
    """"Source values": one collapsible table per field. Text fields list each raw spelling and
    what it became; number fields list each way of writing the number with an example."""
    blocks = []
    for name, rows in report.value_map.items():
        label = FIELD_LABELS.get(name, name.replace("_", " ").title())
        shown, hidden = rows[:VALUE_ROWS_SHOWN], rows[VALUE_ROWS_SHOWN:]
        if rows and "format" in rows[0]:
            summary = f"{label} · {len(rows)} way{'s' if len(rows) != 1 else ''} of writing it"
            head = ["How it was written", "Rows", "Example", "What we used"]
            body = [html.Tr([html.Td(v["format"]), html.Td(f"{v['count']:,}", className="num"),
                             html.Td(_raw_text(v["example"])), html.Td(v["becomes"])]) for v in shown]
        else:
            statuses = {s: sum(1 for v in rows if v["status"] == s) for s in VALUE_STATUS}
            parts = [f"{n} {VALUE_STATUS[s][0]}" for s, n in statuses.items() if n]
            summary = f"{label} · {len(rows)} different value{'s' if len(rows) != 1 else ''}: " + ", ".join(parts)
            with_backup = any(v.get("alternate") for v in rows)
            head = ["In the file", "What we used", "Rows", "Status"] + (["Backup (2nd value)"] if with_backup else [])
            body = [html.Tr([html.Td(_raw_text(v["raw"])), html.Td(v["cleaned"]),
                             html.Td(f"{v['count']:,}", className="num"), html.Td(_status_badge(v["status"]))]
                            + ([html.Td(v.get("alternate") or "")] if with_backup else [])) for v in shown]
        if hidden:
            body.append(html.Tr(html.Td(f"+ {len(hidden):,} more values ({sum(v['count'] for v in hidden):,} rows); "
                                        "the less common ones.",
                                        colSpan=len(head), className="hint")))
        blocks.append(html.Details(className="value-audit", children=[
            html.Summary(summary),
            html.Table(className="va-table w-100", children=[html.Thead(html.Tr([html.Th(h) for h in head])),
                                                              html.Tbody(body)]),
        ]))
    return blocks or [html.Div("No values to show.", className="hint")]


# ======================================================================================
# Callbacks: navigation ("View Report" links, nav bar, global search)
# ======================================================================================
PAGE_IDS = {"/": "page-dashboard", "/leads": "page-leads", "/review": "page-review", "/tests": "page-tests",
            "/about": "page-about"}


@callback(*[Output(page_id, "style") for page_id in PAGE_IDS.values()],
          Output({"type": "nav", "page": ALL}, "className"),
          Output("page-title", "children"), Output("page-eyebrow", "children"),
          Input("url", "pathname"))
def show_page(pathname):
    """Show the page for the current address and highlight its tab. Unknown addresses show the Dashboard."""
    path = pathname if pathname in PAGES else "/"
    show = lambda page: {} if page == path else {"display": "none"}
    nav_ids = [o["id"]["page"] for o in ctx.outputs_list[len(PAGE_IDS)]]
    label, title = PAGES[path]
    return (*[show(page) for page in PAGE_IDS],
            ["nav-item active" if page == path else "nav-item" for page in nav_ids],
            title, "Dashboard" if path == "/" else label)


@callback(Output("url", "pathname", allow_duplicate=True), Output("tabs", "active_tab", allow_duplicate=True),
          Output("url", "hash", allow_duplicate=True),
          Input({"type": "open-tab", "tab": ALL, "key": ALL}, "n_clicks"), prevent_initial_call=True)
def open_tab(clicks):
    """'View Report' links and expand icons: go to the page (and tab) with the details."""
    if not ctx.triggered_id or not any(clicks):
        return no_update, no_update, no_update
    tab = ctx.triggered_id["tab"]
    return (TAB_PAGE.get(tab, "/"), (tab if tab in ("queue", "capacity") else no_update),
            "#data-quality" if tab == "quality" else "")


@callback(Output("q-search", "value"), Output("tabs", "active_tab", allow_duplicate=True),
          Output("url", "pathname", allow_duplicate=True),
          Input("global-search", "value"), prevent_initial_call=True)
def global_search(text):
    """The search box in the header filters the lead queue and opens the Leads & Reps page."""
    return text or "", "queue", "/leads"


# Page navigation starts at the top without replacing the active routing run.
app.clientside_callback(
    """
    function(pathname, hash) {
        setTimeout(function () {
            var target = pathname === '/about' && hash === '#data-quality'
                       ? document.getElementById('data-quality') : null;
            if (target) target.scrollIntoView({block: 'start'});
            else window.scrollTo({top: 0, behavior: 'instant'});
        }, 150);
        return window.dash_clientside.no_update;
    }
    """,
    Output("scroll-done", "data"),
    Input("url", "pathname"), Input("url", "hash"),
)


# ======================================================================================
# Callbacks: lead queue (server-side rows)
# ======================================================================================
@callback(Output("queue-grid", "filterModel"),
          Input("run-store", "data"), Input("q-search", "value"), Input("q-tier", "value"),
          Input("q-region", "value"), Input("q-status", "value"), Input("q-ml", "value"),
          State("queue-grid", "filterModel"))
def external_filters(store, search, tier, region, status, ml, current):
    """Turn the toolbar into AG Grid's filterModel. Changing it makes the grid fetch fresh rows.
    The hidden _run filter changes with every run, so the grid always refreshes after a run."""
    model = dict(current or {})
    flag = status if status == VIP_UNKNOWN else None   # "VIP unknown" is a flag, not a routing status
    status = None if flag else status
    for column, value, op in (("_search", (search or "").strip().lower(), "contains"), ("priority", tier, "equals"),
                              ("region", region, "equals"), ("status", status, "equals"), ("_flag", flag, "equals"), ("ml_flag", ml, "equals")):
        if value and value != "all":
            model[column] = {"filterType": "text", "type": op, "filter": value}
        else:
            model.pop(column, None)
    model["_run"] = {"filterType": "number", "type": "equals", "filter": (store or {}).get("run_id", 0)}
    return model


@callback(Output("queue-grid", "getRowsResponse"), Input("queue-grid", "getRowsRequest"))
def queue_rows(request):
    if not request:
        return no_update
    results, _ = frames()
    return rows_for_request(results, request)


@callback(Output("group-wrap", "style"), Output("queue-wrap", "style"), Output("group-grid", "columnDefs"),
          Output("group-grid", "rowData"), Input("q-group", "value"), Input("run-store", "data"))
def group_view(group, _store):
    """'Group by' swaps the lead grid for a summary per tier or per rep."""
    if group in (None, "none") or STATE.result is None:
        return {"display": "none"}, {}, [], []
    results, _ = frames()
    if group == "priority":
        table = results.groupby("priority").agg(
            leads=("lead_id", "size"), avg_score=("score", "mean"),
            assigned=("status", lambda s: (s == "Assigned").sum()),
            manager_review=("status", lambda s: (s == "Manager Review").sum()),
        ).reindex(TIERS).fillna(0).reset_index()
        table["avg_score"] = table["avg_score"].round(1)
        cols = [{"field": "priority", "headerName": "Tier", "cellRenderer": "TierBadge"}, {"field": "leads"},
                {"field": "assigned"}, {"field": "manager_review", "headerName": "Manager Review"},
                {"field": "avg_score", "headerName": "Avg Score"}]
    else:
        assigned = results[results["status"] == "Assigned"]
        table = pd.crosstab([assigned["rep_id"], assigned["rep_name"], assigned["rep_seniority"]],
                            assigned["priority"]).reindex(columns=TIERS, fill_value=0)
        table["leads"] = table.sum(axis=1)
        table = table.reset_index().sort_values("leads", ascending=False)
        cols = [{"field": "rep_id", "headerName": "Rep"}, {"field": "rep_name", "headerName": "Name", "flex": 2},
                {"field": "rep_seniority", "headerName": "Seniority"}, {"field": "leads", "headerName": "Total"}] + \
               [{"field": t} for t in TIERS]
    return {}, {"display": "none"}, cols, table.to_dict("records")


# ======================================================================================
# Callback: rep capacity table
# ======================================================================================
@callback(Output("rep-grid", "rowData"),
          Input("run-store", "data"), Input("c-region", "value"), Input("c-seniority", "value"), Input("c-sort", "value"),
          Input("c-ml", "value"))
def capacity_table(_store, region, seniority, sort, ml):
    _, reps = frames()
    if region and region != "all":
        reps = reps[reps["region"] == region]
    if seniority and seniority != "all":
        reps = reps[reps["seniority"] == seniority]
    if ml and ml != "all":
        reps = reps[reps["ml_flag"] == ml]
    column, ascending = {"most": ("utilization", False), "least": ("utilization", True),
                         "open": ("available", False)}[sort or "most"]
    return reps.sort_values([column, "load"], ascending=[ascending, ascending], kind="stable").to_dict("records")


# ======================================================================================
# Callback: lead detail drawer
# ======================================================================================
@callback(Output("detail", "is_open"), Output("detail", "children"), Output("detail", "title"),
          Input("queue-grid", "selectedRows"), Input("review-grid", "selectedRows"), Input("btn-trace", "n_clicks"),
          prevent_initial_call=True)
def lead_detail(queue_selection, review_selection, _trace_clicks=None):
    """Clicking a row explains the score, the routing decision, and how the raw row was cleaned.
    "Trace lead" opens the same drawer on pick_trace_lead's lead, in CSV -> dashboard order."""
    traced = ctx.triggered_id == "btn-trace"
    if traced:
        lead_id = trace_lead_id()
    else:
        selection = queue_selection if ctx.triggered_id == "queue-grid" else review_selection
        lead_id = selection[0]["lead_id"] if selection else None
    view = lead_detail_view(lead_id, traced) if lead_id else None
    if view is None:
        return False, no_update, no_update
    return (True, *view)


def trace_trail(lead: Lead, decision: Assignment, fixes: list) -> html.Ol:
    """The 4 steps one lead goes through, CSV row -> dashboard. Each step links to its section below."""
    routed = (f"{decision.rep_name} ({decision.rep_seniority.value})" if decision.rep_id else "Manager Review")
    steps = [
        ("Raw row", f"row {lead.row_number:,} of the CSV", "trace-raw"),
        ("Cleaned", f"{len(fixes)} fix{'es' if len(fixes) != 1 else ''}" if fixes else "already clean", "trace-clean"),
        ("Scored", f"{lead.score} pts · {lead.priority.value}", "trace-score"),
        ("Routed", routed, "trace-route"),
    ]
    return html.Ol(className="trace-trail", children=[
        html.Li(html.A(href=f"#{anchor}", children=[
            html.Span(str(number), className="step-num"),
            html.Span([html.Span(title, className="step-title"), html.Span(sub, className="step-sub")],
                      className="step-text"),
        ])) for number, (title, sub, anchor) in enumerate(steps, start=1)])


def lead_detail_view(lead_id: str, traced: bool = False) -> tuple[list, str] | None:
    """(drawer body, drawer title) for one lead, or None when the lead isn't in the current run.
    traced=True puts the sections in trail order (raw row first, shown open)."""
    lead, decision = find_lead(lead_id)
    if lead is None or decision is None:
        return None

    row = lambda label, value: html.Tr([html.Td(label), html.Td(value)])
    points = lambda p: html.Span(f"{p:+d}", className="points-pos" if p > 0 else "points-zero")
    fixes = lead_fixes(lead)
    fix_table = (html.Table(className="detail-table fix-table mb-3 w-100", children=[
                     html.Thead(html.Tr([html.Th("Field"), html.Th("In the file"), html.Th("What we used")])),
                     html.Tbody([html.Tr([html.Td(f), html.Td(html.Code(b)), html.Td(a)]) for f, b, a in fixes]),
                 ]) if fixes else html.P("Nothing: this row was already clean.", className="hint mb-3"))
    raw_table = html.Table(className="detail-table w-100 mt-2", children=html.Tbody(
        [row(k.replace("_", " "), html.Code("(blank)" if v in (None, "") else str(v))) for k, v in lead.raw.items()]))
    suggestion = ([html.H6("Suggested next step"), html.P(decision.suggested_action, className="plain-explanation "
                                                          "suggestion mb-3")] if decision.suggested_action else [])
    score = html.Div(id="trace-score", children=[
        _anomaly_note(lead),
        html.H6("Why this priority?"),
        html.Table(className="detail-table mb-1 w-100", children=html.Tbody(
            [row(label, points(p)) for label, p in lead.score_reasons] + [row(html.B("Total"), html.B(f"{lead.score} pts"))])),
        html.Details(className="hint mb-3", children=[html.Summary("Scoring rules"),
            "Critical 85+ · High 60-84 · Medium 40-59 · Low 0-39. VIP referrals are always Critical."]),
    ])
    route = html.Div(id="trace-route", children=[
        html.H6("Why this rep?" if decision.rep_id else "Why is it waiting for a manager?"),
        html.P(decision.reason, className="plain-explanation mb-3"),
        _match_table(lead, decision),
        *suggestion,
    ])
    clean = html.Div(id="trace-clean", children=[html.H6("Data corrections"), fix_table])
    raw = html.Div(id="trace-raw", className="mb-3", children=(
        [html.H6(f"The row as it was in the file (row {lead.row_number:,})"), raw_table] if traced else
        [html.Details(className="hint", children=[html.Summary("Original row"), raw_table])]))
    badges = html.Div(className="mb-3", children=[
        html.Span(lead.priority.value, className=f"slds-badge tier-{lead.priority.value.lower()} me-2"),
        html.Span(decision.status.value, className="slds-badge " + ("status-assigned" if decision.rep_id else "status-review")),
    ])
    sections = [raw, clean, score, route] if traced else [score, route, clean, raw]
    title = f"{lead.id} · {lead.company_name}"
    if traced:
        title = f"Traced lead: {title}"
        return [trace_trail(lead, decision, fixes), badges, *sections], title
    return [trace_trail(lead, decision, fixes), badges, *sections], title


def _match_table(lead, decision):
    """Lead vs. assigned rep, trait by trait, with a check or cross: the evidence behind the choice."""
    rep = next((r for r in STATE.result.reps if r.id == decision.rep_id), None) if decision.rep_id else None
    if rep is None:
        return html.Div(className="mb-3")
    lead_regions = [r for r in (lead.region, *lead.alt_regions) if r]
    checks = [
        ("Region", lead.region or "Unknown", rep.region or "-", bool(rep.region and rep.region in lead_regions)),
        ("Language", lead.language or "Unknown", rep.language or "-", bool(rep.language and rep.language == lead.language)),
        ("Industry", lead.industry or "Unknown", rep.industry or "None listed",
         bool(rep.industry) and rep.industry == lead.industry),
        ("Seniority", f"{lead.priority.value} lead", rep.seniority.value, True),
        ("Workload", "", f"{rep.current_capacity} of {rep.max_capacity} leads", True),
    ]
    def mark(trait, ok):
        if trait == "Industry" and lead.industry is None:
            return html.Span("–", title="The lead's industry isn't known")
        return html.Span("✓", className="match-yes") if ok else html.Span("✗", className="match-no")
    return html.Table(className="detail-table match-table mb-3 w-100", children=[
        html.Thead(html.Tr([html.Th(""), html.Th("Lead"), html.Th(rep.name), html.Th("")])),
        html.Tbody([html.Tr([html.Td(t), html.Td(a), html.Td(b), html.Td(mark(t, ok) if t in ("Region", "Language", "Industry") else "")])
                    for t, a, b, ok in checks]),
    ])


# ======================================================================================
# Callbacks: Manager Review page
# ======================================================================================
TRAIT_LABEL = {"region": "Region", "language": "Language", "industry": "Industry"}


def _waiting_leads() -> list[str]:
    """Lead IDs still waiting for a manager, most urgent first (routing order)."""
    if STATE.result is None:
        return []
    return [a.lead_id for a in STATE.result.assignments if a.status is AssignmentStatus.MANAGER_REVIEW]


@callback(Output("review-panel", "children"),
          Input("review-grid", "selectedRows"), Input("run-store", "data"))
def review_panel(selection, _store):
    """Details of the selected lead (or the most urgent one) + the 3 closest reps who can take it."""
    r, revision = STATE.snapshot()
    if r is None:
        return html.P("Run Sample or Run Scale first: leads that need a manager will show up here.", className="hint")
    waiting = [a.lead_id for a in r.assignments if a.status is AssignmentStatus.MANAGER_REVIEW]
    if not waiting:
        return html.Div(className="review-done", children=[
            html.Div("✓", className="review-done-mark"),
            html.P("No leads are waiting. Every lead has a rep.", className="mb-0"),
        ])
    picked = selection[0]["lead_id"] if selection and selection[0]["lead_id"] in waiting else None
    lead_id = picked or waiting[0]
    lead = next(l for l in r.leads if l.id == lead_id)
    decision = next(a for a in r.assignments if a.lead_id == lead_id)

    region = lead.region or "Unknown"
    if lead.alt_regions:
        region += f" (or {', '.join(lead.alt_regions)})"
    facts = [
        ("Region", region), ("Language", lead.language or "Unknown"), ("Industry", lead.industry or "Unknown"),
        ("Company size", f"{lead.company_size:,} employees" if lead.company_size else "Unknown"),
        ("Recent activity", lead.recent_activity or "None"), ("VIP referral", "Yes" if lead.vip_referral else "No"),
    ]
    lead_traits = {"region": lead.region, "language": lead.language, "industry": lead.industry}

    def trait_chip(candidate, trait):
        rep_value = getattr(candidate.rep, trait) or "Unknown"
        if trait in candidate.matched:
            return html.Span(f"✓ {rep_value}", className="trait-chip yes", title=f"Same {trait} as the lead")
        if trait in candidate.missed:
            return html.Span(f"✗ {rep_value}", className="trait-chip no",
                             title=f"Lead: {lead_traits[trait] or 'unknown'}")
        return html.Span(f"– {rep_value}", className="trait-chip unk", title=f"The lead's {trait} is unknown")

    def candidate_card(rank, c):
        rep = c.rep
        return html.Div(className="cand-card", children=[
            html.Div(className="cand-head", children=[
                html.Span(f"#{rank}", className="cand-rank"),
                html.Div([html.P(rep.name, className="cand-name"),
                          html.P(f"{rep.id} · {rep.seniority.value}", className="cand-sub")]),
                html.Span(f"{c.traits} of 3", className=f"cand-score s{c.traits}"),
            ]),
            html.Div([trait_chip(c, t) for t in ("region", "language", "industry")], className="cand-traits"),
            html.Div(className="cand-foot", children=[
                html.Div(className="cand-load", children=[
                    html.Div(className="cand-bar", children=html.Div(
                        className="cand-bar-fill", style={"width": f"{100 * rep.load_ratio:.0f}%"})),
                    html.Span(f"{rep.current_capacity} of {rep.max_capacity} leads · {rep.available} open",
                              className="cand-sub"),
                ]),
                html.Button(f"Assign to {rep.name.split()[0]}", className="slds-btn brand",
                            id={"type": "assign-rep", "rep": rep.id}),
            ]),
        ])

    choices = rep_choices(lead, r.reps)
    top = choices[:3]
    suggestions = ([candidate_card(i, c) for i, c in enumerate(top, start=1)] if top else
                   [html.P("No rep with open slots is allowed to take this lead"
                           + (" (Critical leads can't go to Juniors)." if lead.priority.value == "Critical" else ".")
                           + " Free up capacity first, e.g. raise a rep's Max_Capacity.", className="hint")])
    picker = html.Div(className="review-any", children=[
        html.P("Or choose any rep with room", className="review-label"),
        html.Div(className="review-any-row", children=[
            dcc.Dropdown(id="review-any", placeholder="Search reps by name, ID, seniority…", clearable=True,
                         options=[{"label": f"{c.rep.name} ({c.rep.id}) · {c.rep.seniority.value} · "
                                            f"{c.traits} of 3 traits · {c.rep.available} open",
                                   "value": c.rep.id} for c in choices],
                         className="review-any-dd"),
            html.Button("Assign", id="review-assign-any", className="slds-btn"),
        ]),
    ]) if choices else None

    return [
        dcc.Store(id="review-context", data={"run_id": revision, "lead_id": lead_id}),
        html.Div(className="review-lead-head", children=[
            html.Div([html.P(f"{lead.id} · {lead.company_name}", className="review-lead-name"),
                      html.P("" if picked else "Showing the most urgent waiting lead. Click any row to switch.",
                             className="cand-sub")]),
            html.Div([html.Span(lead.priority.value, className=f"slds-badge tier-{lead.priority.value.lower()} me-1"),
                      html.Span(f"{lead.score} pts", className="score-badge")]),
        ]),
        html.Div(className="review-why", children=[
            html.P("Why it's waiting", className="review-label"),
            html.P(decision.reason, className="mb-0"),
        ]),
        _anomaly_note(lead),
        html.Table(className="detail-table w-100 mb-3", children=html.Tbody(
            [html.Tr([html.Td(k), html.Td(v)]) for k, v in facts])),
        html.P(f"Closest reps who can take it ({len(choices):,} reps have room and are allowed)",
               className="review-label"),
        html.Div(suggestions, className="cand-list"),
        picker,
    ]


@callback(Output("run-store", "data", allow_duplicate=True), Output("review-msg", "children"),
          Input({"type": "assign-rep", "rep": ALL}, "n_clicks"), Input("review-assign-any", "n_clicks", allow_optional=True),
          State("review-any", "value", allow_optional=True), State("review-context", "data", allow_optional=True),
          prevent_initial_call=True)
def assign_from_review(_card_clicks, _any_click, any_rep, review_context):
    """A manager clicked Assign: give the lead to that rep (same safety rules as automatic routing)."""
    if not ctx.triggered or not ctx.triggered[0]["value"] or STATE.result is None:
        return no_update, no_update
    lead_id = (review_context or {}).get("lead_id")
    rep_id = ctx.triggered_id["rep"] if isinstance(ctx.triggered_id, dict) else any_rep
    if lead_id is None:
        return no_update, no_update
    if not rep_id:
        return no_update, dbc.Alert("Choose a rep from the list first.", color="warning", dismissable=True)
    try:
        decision, run_id = STATE.assign_review(lead_id, rep_id, review_context.get("run_id"))
    except ReviewError as error:
        return no_update, dbc.Alert(str(error), color="danger", dismissable=True)
    left = len(_waiting_leads())
    return {"run_id": run_id}, dbc.Alert(
        f"✓ {lead_id} assigned to {decision.rep_name} ({decision.rep_seniority.value}). "
        f"{left:,} lead{'s' if left != 1 else ''} still waiting.", color="success", dismissable=True)


# ======================================================================================
# Callbacks: Tests page
# ======================================================================================
@callback(Output("url", "pathname", allow_duplicate=True), Output("tests-trigger", "data"),
          Input("btn-tests", "n_clicks"), Input("tests-run", "n_clicks"),
          State("tests-trigger", "data"), prevent_initial_call=True)
def start_tests(_header, _page, trigger):
    """'✓ Run Tests' in the header opens the Tests page; both buttons start a run."""
    return ("/tests" if ctx.triggered_id == "btn-tests" else no_update), (trigger or 0) + 1


_SCALE_RUN: dict[str, Any] = {}


def _scale_run_for_cards():
    """The 50k run behind the picture cards, done once and kept in memory: re-clicking Run all tests
    then only waits for pytest. (pytest itself always does fresh runs.)"""
    if "run" not in _SCALE_RUN:
        _SCALE_RUN["run"] = scenarios.full_run()
    return _SCALE_RUN["run"]


@callback(Output("tests-body", "children"), Input("tests-trigger", "data"), prevent_initial_call=True,
          running=[(Output("tests-run", "disabled"), True, False)])
def run_tests(_trigger):
    """Run pytest (in its own process) and the illustrated business scenarios side by side."""
    started = time.perf_counter()
    process, report = test_runner.start_suite()
    with ThreadPoolExecutor(1) as pool:
        scenario_job = pool.submit(lambda: scenarios.run_all(full=_scale_run_for_cards()))
        suite = test_runner.finish_suite(process, report, started)
        scenario_results, _ = scenario_job.result()
    return _suite_view(suite) + [_scenario_section(scenario_results)]


def _suite_view(suite: test_runner.SuiteRun) -> list:
    if suite.error:
        return [dbc.Alert(suite.error, color="danger")]
    ok = suite.failed_cases == 0
    summary = html.Div(className="tests-summary " + ("ok" if ok else "bad"), children=[
        html.Div("✓" if ok else "✗", className="tests-summary-mark"),
        html.Div([
            html.P(f"{suite.passed_cases:,} of {suite.total_cases:,} test cases passed"
                   + ("" if ok else f" · {suite.failed_cases} failed"), className="tests-summary-title"),
            html.P(f"{len(suite.results)} pytest tests (some run several inputs each) · "
                   f"finished in {suite.seconds:.1f} s · {datetime.now():%I:%M %p}", className="tests-summary-sub"),
        ]),
    ])
    shown, sections = set(), [summary]
    for title, intro, entries in test_runner.CATALOG:
        rows = []
        for name, plain, proves in entries:
            result = suite.results.get(name)
            if result is None:
                continue
            shown.add(name)
            rows.append(_proof_row(plain, proves, result))
        passed = sum(1 for name, *_ in entries if name in suite.results and suite.results[name].status != "failed")
        sections.append(html.Div(className="component", children=[
            html.Div(className="component-head", children=html.H2(
                [title, html.Span(f"{passed} of {len(rows)} passing", className="proof-count")],
                className="component-title proof-group-title")),
            html.Div(className="component-body", children=[html.P(intro, className="hint mb-2") if intro else None,
                                                           html.Div(rows, className="proof-list")]),
        ]))
    others = [r for n, r in sorted(suite.results.items(), key=lambda kv: (kv[1].file, kv[0])) if n not in shown]
    sections.append(html.Div(className="component", children=[
        html.Div(className="component-head", children=html.H2(
            [f"Every other test ({len(others)})",
             html.Span(f"{sum(r.status != 'failed' for r in others)} of {len(others)} passing", className="proof-count")],
            className="component-title proof-group-title")),
        html.Div(className="component-body", children=html.Details([
            html.Summary("Show all of them (cleaning, scoring, routing, distribution, explanations…)"),
            html.Div([_proof_row(r.name.removeprefix("test_").replace("_", " ").capitalize(), "", r) for r in others],
                     className="proof-list mt-2"),
        ])),
    ]))
    return sections


def _proof_row(plain: str, proves: str, result: test_runner.TestResult) -> html.Div:
    mark = {"passed": "✓", "failed": "✗", "skipped": "–"}[result.status]
    cases = f" · {result.cases} inputs" if result.cases > 1 else ""
    return html.Div(className=f"proof-row {result.status}", children=[
        html.Span(mark, className=f"proof-mark {result.status}"),
        html.Div([
            html.P(plain, className="proof-title"),
            html.P(proves, className="proof-text") if proves else None,
            *[html.Pre(msg, className="proof-error") for msg in result.messages[:2]],
            html.P(f"{result.file} · {result.name}{cases} · {result.seconds:.2f} s", className="proof-name"),
        ]),
    ])


def _scenario_section(results) -> html.Div:
    """The 8 business scenarios (also part of pytest), each with a small chart and a plain explanation."""
    return html.Div(className="component", children=[
        html.Div(className="component-head", children=html.H2("Business scenarios, with pictures", className="component-title")),
        html.Div(className="component-body", children=[
            html.P("The same checks as the \"Rules that must never break\" tests above, shown as a picture of the result.",
                   className="hint mb-2"),
            dbc.Row([dbc.Col(_test_card(s), xs=12, lg=6, className="mb-3") for s in results]),
        ]),
    ])


def _unusual_view(r) -> tuple:
    """Summary line + the most unusual leads and reps the ML model flagged (lead_router/anomaly.py)."""
    leads = sorted((l for l in r.leads if l.anomaly), key=lambda l: -l.anomaly_score)
    reps = sorted((x for x in r.reps if x.anomaly), key=lambda x: -x.anomaly_score)
    summary = html.P([html.B(f"{len(leads):,} leads"), f" of {len(r.leads):,} and ", html.B(f"{len(reps):,} reps"),
                      f" of {len(r.reps):,} look unusual (checked in {r.timings.get('anomaly', 0):.2f} s). "
                      "Flagged leads still went through the normal rules. The note also appears in each lead's "
                      "detail panel and in the export (column unusual_record)."], className="mb-2")

    def table(rows, head):
        if not rows:
            return html.P("Nothing unusual found.", className="hint")
        return dbc.Table([html.Thead(html.Tr([html.Th(h) for h in head])), html.Tbody(rows)],
                         striped=True, hover=True, size="sm", className="dq-table")
    lead_rows = [html.Tr([html.Td(l.id), html.Td(l.company_name), html.Td(l.anomaly)]) for l in leads[:12]]
    rep_rows = [html.Tr([html.Td(f"{x.name} ({x.id})"), html.Td(x.anomaly)]) for x in reps[:8]]
    return summary, table(lead_rows, ["Lead", "Company", "Why it stands out"]), table(rep_rows, ["Rep", "Why it stands out"])


def _anomaly_note(lead):
    """A yellow note when the ML model thinks this lead looks unusual."""
    if not lead.anomaly:
        return None
    return html.Div(className="ml-note mb-3", children=[
        html.B("⚠ Unusual record (machine learning): "), lead.anomaly,
        html.Div("Routed by the normal rules; this is only a prompt to double-check the data.", className="cand-sub"),
    ])


def _test_card(s: scenarios.Scenario) -> html.Div:
    if s.skipped:
        badge = html.Span("Skipped", className="test-badge skip")
    elif s.passed:
        badge = html.Span("✓ Passed", className="test-badge pass")
    else:
        badge = html.Span("✗ Failed", className="test-badge fail")
    def blurb(label, text):
        """A labelled one-liner, or a short bullet list when given several lines."""
        body = (html.Ul([html.Li(line) for line in text], className="test-blurb-list") if isinstance(text, list)
                else html.P(text, className="test-blurb-text"))
        return html.Div(className="test-blurb", children=[html.P(label, className="test-blurb-label"), body])
    return html.Div(className="test-card", children=[
        html.Div(className="test-card-head", children=[
            html.H3(s.title, className="test-title"),
            badge,
        ]),
        html.Div(_test_graphic(s), className="test-graphic"),
        html.Details(children=[html.Summary("Test details"),
            blurb("Purpose", s.why), blurb("Result", s.result), blurb("What it proves", s.meaning)]),
    ])


def _test_graphic(s: scenarios.Scenario):
    """A small picture of the result: bars, a before → after list, or outcome chips."""
    chart = s.chart
    kind = chart.get("type")
    config = {"displayModeBar": False, "staticPlot": True}
    if kind == "bars":
        fig = charts.test_bars(chart["labels"], chart["values"], chart["colors"], chart.get("unit", ""), chart.get("max"))
        return dcc.Graph(figure=fig, config=config)
    if kind == "grouped":
        return dcc.Graph(figure=charts.test_grouped(chart["labels"], chart["series"], chart.get("unit", "")), config=config)
    if kind == "pairs":
        return html.Div(className="pairs", children=[
            html.Div(className="pair", children=[html.Span(f"“{before}”", className="pair-before"),
                                                 html.Span("→", className="pair-arrow"),
                                                 html.Span(after, className="pair-after")])
            for before, after in chart["rows"]])
    if kind == "outcomes":
        return html.Div(className="pairs", children=[
            html.Div(className="pair", children=[html.Span(label, className="pair-before label"),
                                                 html.Span("→", className="pair-arrow"),
                                                 html.Span(("✓ " if ok else "✗ ") + outcome,
                                                           className="pair-after " + ("ok" if ok else "bad"))])
            for label, outcome, ok in chart["rows"]])
    return html.P("Add the 50,000-lead file to see this chart.", className="hint")


# ======================================================================================
# Callback: CSV export
# ======================================================================================
@server.get("/exports/<kind>.csv")
def export_csv(kind):
    """Serve named attachments directly so download names survive browser preview environments."""
    prefixes = {"results": "lead_routing", "salesforce": "salesforce_mapping_template",
                "review": "manager_review", "reps": "rep_capacity"}
    if kind not in prefixes:
        abort(404)
    result, _ = STATE.snapshot()
    if result is None:
        return "Run a dataset or upload before exporting.", 409
    results = results_frame(result)
    if kind == "reps":
        table = reps_frame(result)
    elif kind == "salesforce":
        table = salesforce_frame(results)
    elif kind == "review":
        table = results[results["status"] == "Manager Review"]
    else:
        table = results
    name = secure_filename(result.dataset) or "custom"
    response = send_file(io.BytesIO(table.to_csv(index=False).encode("utf-8-sig")),
                         mimetype="text/csv; charset=utf-8", as_attachment=True,
                         download_name=f"{prefixes[kind]}_{name}.csv", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    import os
    app.run(debug=False, port=int(os.environ.get("PORT", 8050)))
