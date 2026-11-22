"""
Lead Router dashboard: a Salesforce Lightning-style "dream dashboard" for the routing engine.

Run (from the repository root):   python -m app.main   ->   http://127.0.0.1:8050

Layout, top to bottom (mirrors a Lightning dashboard page):
    global header   logo, search (jumps to the lead queue), icons
    app nav bar     Lead Router app with tabs; "Dashboards" is active
    page header     title, "As of ...", Run Sample / Run Scale / Reset, matching mode, Trace a messy lead,
                    upload, export
    canvas          white components on a blue-gray background:
                      row 1  Tiers vs Reference | Lead Routing Funnel | Assigned + Manager Review + Critical Waiting
                      row 2  Leads by Region | Who Got the Leads | Assigned vs Review | Review Reasons
                      row 3  six big numbers: Critical→Senior, fairness, perfect matches, speed, utilization, values auto-fixed
                      row 4  Rules Check: the rules proven on this run + time per stage
                      row 5  Capacity Used by Seniority | Team Capacity
    details         Lightning tabs: Lead Queue, Manager Review, Rep Capacity, Data Quality

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
from datetime import datetime
from typing import Any

import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash import ALL, Dash, Input, Output, State, callback, ctx, dcc, html, no_update

from app import charts
from lead_router import scenarios
from app.grid_server import rows_for_request
from lead_router.cleaning import LEAD_COLUMNS, LEAD_REQUIRED, REP_COLUMNS, REP_REQUIRED
from lead_router.config import dataset_paths
from lead_router.explain import FIELD_LABELS, VIP_UNKNOWN_TAG, lead_fixes
from lead_router.export import reps_frame, results_frame, salesforce_frame
from lead_router.models import Assignment, Lead, MatchLevel, OverflowReason, PipelineResult, Priority
from lead_router.pipeline import STATE, PipelineError, read_csv, reset_state, run_dataset, \
    run_pipeline_frames

TIERS = charts.TIERS
REGIONS = ["North America", "Europe", "Asia-Pacific", "Unknown"]
# the measured pipeline stages shown in the Rules Check bar ("metrics" = working out the KPIs;
# shown too, so the stages add up to the total and the slowest one is named honestly)
TIMED_STAGES = ["load", "clean", "score", "route", "metrics"]
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
            _CACHE.update(run_id=STATE.run_id, results=results, reps=reps_frame(STATE.result),
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
    """The lead "Trace a messy lead" opens, chosen the same way every time:
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
def report_link(text: str, tab: str, key: str) -> html.A:
    """A link that opens one of the detail tabs and scrolls to it (like 'View Report')."""
    return html.A(text, href="#details", id={"type": "open-tab", "tab": tab, "key": key})


def component(title: str, body, tab: str, report: str, key: str) -> html.Div:
    """One dashboard component: title + expand icon, body, 'View Report' footer."""
    return html.Div(className="component", children=[
        html.Div(className="component-head", children=[
            html.H2(title, className="component-title"),
            html.A("⤢", href="#details", className="component-expand", title="Open details",
                   id={"type": "open-tab", "tab": tab, "key": f"{key}-x"}),
        ]),
        html.Div(body, className="component-body"),
        html.Div(report_link(f"View Report ({report})", tab, key), className="component-foot"),
    ])


def chart(graph_id: str, height: int = 270) -> dcc.Graph:
    # responsive=True: redraw to the card's width (the canvas is hidden until the first run,
    # so a fixed size measured at page load would be wrong).
    return dcc.Graph(id=graph_id, figure=charts.empty(height), config={"displayModeBar": False},
                     responsive=True, style={"height": f"{height}px", "width": "100%"})


def metric(key: str, title: str, tab: str, report: str, small: bool = False) -> html.Div:
    body = html.Div(className="metric-body", children=[
        html.Div("—", id=f"m-{key}", className="metric-value" + (" small" if small else "")),
        html.Div("", id=f"m-{key}-cap", className="metric-caption"),
    ])
    return component(title, body, tab, report, key)


def global_header() -> html.Div:
    """Logo + search. Pressing Enter in the search box filters the lead queue and scrolls down to it."""
    return html.Div(className="global-header", children=[
        html.Img(src="/assets/salesforce-logo.svg", alt="Salesforce", className="app-logo"),
        html.Div(className="global-search", children=dcc.Input(
            id="global-search", type="search", placeholder="Search leads, companies or reps, then press Enter",
            debounce=True, n_submit=0)),
    ])


def app_nav() -> html.Div:
    """Every nav item does something: opens a detail tab and scrolls to it, or back to the top."""
    def item(label: str, tab: str | None = None, active: bool = False):
        if tab:
            return html.A(label, href="#details", className="nav-item",
                          id={"type": "open-tab", "tab": tab, "key": f"nav-{label}"})
        return html.A(label, href="#top", className="nav-item" + (" active" if active else ""))
    return html.Div(className="app-nav", children=[
        html.Div("Lead Router", className="app-name"),
        item("Home"), item("Leads", "queue"), item("Reps", "capacity"),
        item("Dashboards", active=True), item("Manager Review", "review"), item("Data Quality", "quality"),
    ])


DASHBOARD_ICON = html.Img(src=("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' "
                               "viewBox='0 0 24 24' fill='white'><rect x='3' y='12' width='4' height='8' rx='1'/>"
                               "<rect x='10' y='7' width='4' height='13' rx='1'/><rect x='17' y='3' width='4' "
                               "height='17' rx='1'/></svg>"), width=20, height=20)


def page_header() -> html.Div:
    return html.Div(className="page-header", id="top", children=[
        html.Div(DASHBOARD_ICON, className="page-icon"),
        html.Div(className="page-titles", children=[
            html.Div("Dashboard", className="page-eyebrow"),
            html.H1("Lead Routing Dashboard", className="page-title"),
            dcc.Loading(type="dot", color="#0176D3", children=html.Div(
                id="as-of", className="page-meta", children="No data yet · Viewing as Sales Operations")),
        ]),
        html.Div(className="page-actions", children=[
            html.Div(className="btn-group", children=[
                html.Button("Run Sample", id="btn-sample", className="slds-btn"),
                html.Button("Run Scale (50k)", id="btn-scale", className="slds-btn brand"),
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
            html.Button("Trace a messy lead", id="btn-trace", className="slds-btn", disabled=True,
                        title="Follow one lead from the raw CSV row to its rep: the Critical lead "
                              "with the most data fixes"),
            dcc.Upload(id="up-leads", children=html.Div("⬆ Leads CSV", id="up-leads-label", className="upload-chip")),
            dcc.Upload(id="up-reps", children=html.Div("⬆ Reps CSV", id="up-reps-label", className="upload-chip")),
            html.Button("Run Upload", id="btn-upload", className="slds-btn", disabled=True),
            dbc.DropdownMenu(label="Export", className="export-menu", align_end=True, children=[
                dbc.DropdownMenuItem("Routed leads (CSV)", id="exp-results"),
                dbc.DropdownMenuItem("Salesforce Data Loader update (CSV)", id="exp-sf"),
                dbc.DropdownMenuItem("Manager Review queue (CSV)", id="exp-review"),
                dbc.DropdownMenuItem("Rep capacity (CSV)", id="exp-reps"),
            ]),
            html.Button("✓ Run Tests", id="btn-tests", className="slds-btn tests-btn",
                        title="Run the business scenario checks and see the results"),
            dcc.Download(id="download"),
        ]),
    ])


def empty_state() -> html.Div:
    return html.Div(id="empty-state", className="component empty-component", children=[
        html.Div(DASHBOARD_ICON, className="page-icon", style={"margin": "0 auto", "width": "48px", "height": "48px"}),
        html.H3("Your dashboard is ready for data"),
        html.P("Click Run Sample (100 leads, 10 reps) or Run Scale (50,000 leads, 1,600 reps) above, "
               "or upload your own leads CSV. Results appear here in seconds."),
    ])


def canvas() -> html.Div:
    # All components sit in ONE grid row, so the gap between boxes is the same everywhere,
    # side to side and top to bottom (set by --gap in style.css).
    col = lambda child, lg, md=12: dbc.Col(child, xs=12, md=md, lg=lg)
    return html.Div(id="canvas", style={"display": "none"}, children=dbc.Row(className="canvas-grid", children=[
        # Row 1
        col(component("Leads by Priority Tier vs Reference", [
            chart("ch-tiers", 330),
            html.Div(id="tier-caption", className="chart-caption"),
        ], "queue", "Prioritized Lead Queue", "tiers"), 5),
        col(component("Lead Routing Funnel", chart("ch-funnel"), "queue", "Prioritized Lead Queue", "funnel"), 4, 6),
        col(html.Div(className="metric-stack", children=[
            metric("assigned", "Leads Assigned to Reps", "queue", "Prioritized Lead Queue", small=True),
            metric("review", "Waiting in Manager Review", "review", "Manager Review", small=True),
            metric("critwait", "Critical Leads Waiting", "review", "Manager Review", small=True),
        ]), 3, 6),
        # Row 2
        col(component("Leads by Region", chart("ch-region"), "queue", "Prioritized Lead Queue", "region"), 3, 6),
        col(component("Who Got the Leads (by Priority)", chart("ch-destination"), "capacity", "Rep Capacity", "dest"), 3, 6),
        col(component("Assigned vs. Manager Review", chart("ch-split"), "review", "Manager Review", "split"), 3, 6),
        col(component("Manager Review by Reason", chart("ch-reasons"), "review", "Manager Review", "reasons"), 3, 6),
        # Row 3: six big numbers, equal widths (nested row so they share one line)
        dbc.Col(xs=12, children=dbc.Row(className="canvas-grid", children=[
            dbc.Col(metric("critical", "Critical Leads to Seniors", "queue", "Prioritized Lead Queue"),
                    xs=12, md=6, lg=True),
            dbc.Col(metric("fairness", "Distribution Fairness", "capacity", "Rep Capacity"), xs=12, md=6, lg=True),
            dbc.Col(metric("matches", "Perfect Matches", "queue", "Prioritized Lead Queue"), xs=12, md=6, lg=True),
            dbc.Col(metric("speed", "Leads Routed per Second", "quality", "Data Quality"), xs=12, md=4, lg=True),
            dbc.Col(metric("util", "Team Capacity Used", "capacity", "Rep Capacity"), xs=12, md=4, lg=True),
            dbc.Col(metric("fixed", "Messy Values Auto-Fixed", "quality", "Data Quality"), xs=12, md=4, lg=True),
        ])),
        # Row 4: proof that the rules held on this run, and where the time went
        dbc.Col(xs=12, children=component("Rules Check", dbc.Row(className="g-3 px-2", children=[
            dbc.Col(html.Div(id="rules-check"), xs=12, lg=7),
            dbc.Col([html.H3("Time per stage (measured on this run)", className="sub-title"),
                     chart("ch-timings", 120),
                     html.Div(id="timing-caption", className="chart-caption")], xs=12, lg=5),
        ]), "capacity", "Rep Capacity", "rules")),
        # Row 5
        col(component("Capacity Used by Seniority", chart("ch-capacity", 300), "capacity", "Rep Capacity", "top"), 6),
        col(component("Team Capacity: Reps by % Full", chart("ch-util", 300), "capacity", "Rep Capacity", "util-h"), 6),
    ]))


# ---- detail tabs --------------------------------------------------------------------
QUEUE_COLS = [
    {"field": "queue_rank", "headerName": "#", "width": 80, "filter": "agNumberColumnFilter"},
    {"field": "lead_id", "headerName": "Lead", "width": 115},
    {"field": "company_name", "headerName": "Company", "flex": 2, "minWidth": 200},
    {"field": "priority", "headerName": "Tier", "width": 110, "cellRenderer": "TierBadge"},
    {"field": "score", "headerName": "Score", "width": 90, "filter": "agNumberColumnFilter"},
    {"field": "status", "headerName": "Status", "width": 145, "cellRenderer": "StatusPill"},
    {"field": "rep_name", "headerName": "Assigned Rep", "flex": 1.4, "minWidth": 170},
    {"field": "rep_seniority", "headerName": "Seniority", "width": 110},
    {"field": "region", "headerName": "Region", "width": 135},
    {"field": "language", "headerName": "Language", "width": 110},
    {"field": "industry", "headerName": "Industry", "width": 130},
    {"field": "notes", "headerName": "Notes", "flex": 1.4, "minWidth": 170, "tooltipField": "notes"},
    {"field": "reason", "headerName": "Why this rep?", "flex": 3, "minWidth": 320, "tooltipField": "reason"},
    {"field": "_search", "hide": True},                                   # driven by the search box
    {"field": "_flag", "hide": True},                                     # driven by Status = VIP unknown
    {"field": "_run", "hide": True, "filter": "agNumberColumnFilter"},    # forces a refresh per run
]
GRID_THEME = "ag-theme-quartz"


def _select(select_id: str, label: str, options: list[str], value: str = "all", all_label: str = "All"):
    opts = ([{"label": all_label, "value": "all"}] if all_label else []) + [{"label": o, "value": o} for o in options]
    return dbc.InputGroup([dbc.InputGroupText(label), dbc.Select(id=select_id, options=opts, value=value)], size="sm")


def queue_tab() -> html.Div:
    return html.Div([
        dbc.Row(className="g-2 mb-2 toolbar", children=[
            dbc.Col(dbc.Input(id="q-search", placeholder="Search lead, company or rep...", type="search",
                              debounce=True, size="sm"), xs=12, md=3),
            dbc.Col(_select("q-tier", "Tier", TIERS), xs=6, md=2),
            dbc.Col(_select("q-region", "Region", REGIONS), xs=6, md=2),
            dbc.Col(_select("q-status", "Status", ["Assigned", "Manager Review", VIP_UNKNOWN]), xs=6, md=2),
            dbc.Col(dbc.InputGroup([dbc.InputGroupText("Group by"), dbc.Select(id="q-group", value="none", options=[
                {"label": "None", "value": "none"}, {"label": "Tier", "value": "priority"},
                {"label": "Rep", "value": "rep"}])], size="sm"), xs=6, md=3),
        ]),
        html.Div("Click any row to see its score breakdown and routing explanation.", className="hint mb-2"),
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
                             "rowSelection": {"mode": "singleRow", "checkboxes": False, "enableClickSelection": True}},
            filterModel={"_run": {"filterType": "number", "type": "equals", "filter": 0}},
            style={"height": "520px"})),
    ])


def review_tab() -> html.Div:
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
    return html.Div([
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
                                    "rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                     "enableClickSelection": True}},
                   filterModel={"_run": {"filterType": "number", "type": "equals", "filter": 0}},
                   style={"height": "500px"}),
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
        {"field": "data_fixes", "headerName": "What we fixed in this row", "flex": 1.5, "minWidth": 220,
         "tooltipField": "data_fixes"},
    ]
    return html.Div([
        dbc.Row(className="g-2 mb-2 toolbar", children=[
            dbc.Col(_select("c-region", "Region", REGIONS), xs=6, md=3),
            dbc.Col(_select("c-seniority", "Seniority", ["Junior", "Senior", "Manager"]), xs=6, md=3),
            dbc.Col(dbc.InputGroup([dbc.InputGroupText("Sort"), dbc.Select(id="c-sort", value="most", options=[
                {"label": "Most loaded", "value": "most"}, {"label": "Least loaded", "value": "least"},
                {"label": "Most open slots", "value": "open"}])], size="sm"), xs=12, md=3),
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
        html.H3("Every value we found", className="component-title mt-3 mb-1"),
        html.Div("Every different way each column was written in the file, and what the router used instead. "
                 "Open a field to see its values.", className="hint mb-2"),
        dbc.Row([
            dbc.Col([html.H4("Leads", className="sub-title"), html.Div(id="va-leads")], lg=6),
            dbc.Col([html.H4("Representatives", className="sub-title"), html.Div(id="va-reps")], lg=6),
        ]),
    ])


def details() -> html.Div:
    return html.Div(id="details", className="component details", style={"display": "none"}, children=
        dbc.Tabs(id="tabs", active_tab="queue", children=[
            dbc.Tab(queue_tab(), label="Prioritized Lead Queue", tab_id="queue"),
            dbc.Tab(review_tab(), label="Manager Review", tab_id="review", id="tab-review"),
            dbc.Tab(capacity_tab(), label="Rep Capacity", tab_id="capacity"),
            dbc.Tab(quality_tab(), label="Data Quality", tab_id="quality"),
        ]))


def tests_modal() -> dbc.Modal:
    """The 'Run Tests' panel: runs the business scenario checks (lead_router/scenarios.py, the same
    code pytest runs) and shows one card per check."""
    return dbc.Modal(id="tests-modal", size="xl", scrollable=True, is_open=False, children=[
        dbc.ModalHeader(dbc.ModalTitle("Is Lead Router working correctly?")),
        dbc.ModalBody(dcc.Loading(type="circle", color="#0176D3", children=html.Div(
            id="tests-body", className="tests-body"))),
        dbc.ModalFooter([
            html.Span("These are the same checks our automated pytest suite runs on every change.", className="hint me-auto"),
            html.Button("↻ Run again", id="tests-again", className="slds-btn"),
            html.Button("Close", id="tests-close", className="slds-btn brand"),
        ]),
    ])


def layout() -> html.Div:
    return html.Div([
        global_header(),
        app_nav(),
        page_header(),
        html.Div(className="canvas", children=[
            html.Div(id="alert-area"),
            empty_state(),
            canvas(),
            details(),
        ]),
        dbc.Offcanvas(id="detail", title="Lead detail", placement="end", is_open=False, style={"width": "460px"}),
        dcc.Store(id="run-store", data={"run_id": 0}),
        dcc.Store(id="up-leads-store"),
        dcc.Store(id="up-reps-store"),
        dcc.Store(id="scroll-done"),  # dummy output for the search-scroll callback
        dcc.Store(id="tests-trigger"),  # bumps when the tests should (re)run
        tests_modal(),
    ])


app = Dash(__name__, title="Lead Routing Dashboard", external_stylesheets=[dbc.themes.BOOTSTRAP],
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
    "details_style": Output("details", "style"), "as_of": Output("as-of", "children"),
    **{f"value_{k}": Output(f"m-{k}", "children") for k in METRIC_KEYS},
    **{f"caption_{k}": Output(f"m-{k}-cap", "children") for k in METRIC_KEYS},
    "critwait_class": Output("m-critwait", "className"),
    **{f"fig_{c}": Output(c, "figure") for c in CHART_IDS},
    "tier_caption": Output("tier-caption", "children"), "rules_check": Output("rules-check", "children"),
    "timing_caption": Output("timing-caption", "children"),
    "review_label": Output("tab-review", "label"), "review_banner": Output("review-banner", "children"),
    "review_filter_options": Output("review-filter", "options"), "review_filter_value": Output("review-filter", "value"),
    "dq_leads": Output("dq-leads", "children"), "dq_reps": Output("dq-reps", "children"),
    "va_leads": Output("va-leads", "children"), "va_reps": Output("va-reps", "children"),
    "trace_disabled": Output("btn-trace", "disabled"),
}
METRIC_CLASS = "metric-value small"


@callback(output=RENDER_OUTPUTS, inputs=[Input("run-store", "data")])
def render(_store=None) -> dict[str, Any]:
    r = STATE.result
    if r is None:
        blank = charts.empty()
        return {
            "empty_style": {}, "canvas_style": {"display": "none"}, "details_style": {"display": "none"},
            "as_of": "No data yet · Viewing as Sales Operations",
            **{f"value_{k}": "—" for k in METRIC_KEYS}, **{f"caption_{k}": "" for k in METRIC_KEYS},
            "critwait_class": METRIC_CLASS,
            **{f"fig_{c}": blank for c in CHART_IDS},
            "tier_caption": None, "rules_check": None, "timing_caption": None,
            "review_label": "Manager Review", "review_banner": None,
            "review_filter_options": [{"label": "All", "value": "all"}], "review_filter_value": "all",
            "dq_leads": None, "dq_reps": None, "va_leads": None, "va_reps": None, "trace_disabled": True,
        }

    m, (results, reps) = r.metrics, frames()
    lq, rq = r.lead_quality, r.rep_quality
    seconds = r.timings["total"]

    as_of = (f"As of {datetime.now():%b %d, %Y, %I:%M %p} · {r.dataset.title()} dataset · "
             f"{m['total_leads']:,} leads routed in {seconds:.2f} s · Matching: {m['matching_mode']} · "
             "Viewing as Sales Operations")

    # --- match quality: 3 of 3 traits vs. 2 of 3 (see routing.pools_to_try)
    levels = m["match_levels"]
    by_traits = {3: 0, 2: 0, 1: 0}
    for name, count in levels.items():
        by_traits[MatchLevel(name).traits_matched] += count

    # --- big numbers: (value, caption)
    waiting = m["critical_waiting"]
    metrics = {
        "assigned": (f"{m['assigned']:,}", f"{m['assigned_rate']}% · traits matched 3/3: {by_traits[3]:,} · "
                                           f"2/3: {by_traits[2]:,} · 1/3: {by_traits[1]:,}"),
        "review": (f"{m['overflow']:,}", f"{m['overflow_rate']}% need a manager decision"),
        "critwait": (f"{waiting:,}", "Critical leads with no owner yet: handle these first" if waiting
                     else "Every Critical lead has a Senior or Manager"),
        "critical": (f"{m['critical_senior_rate']}%",
                     html.Span([f"{m['critical_to_senior']:,} of {m['critical_total']:,} · ",
                                html.Span(f"{m['junior_critical_violations']} given to Juniors", className="metric-good")])),
        # captions kept short (two lines max) so the big numbers line up across the row
        "speed": (f"{m['throughput_lps']:,}", f"{m['total_leads']:,} leads in {seconds:.2f} s"),
        "util": (f"{m['utilization']}%", f"{m['load_total']:,} of {m['capacity_total']:,} slots · "
                                         f"{m['capacity_violations']} over capacity"),
        "fixed": (f"{lq.fixed + rq.fixed:,}", f"{m['imputed_rate']}% of leads had a gap · 0 dropped"),
        # Share of assigned leads whose rep matches region, language AND industry
        "matches": (f"{100 * by_traits[3] / max(m['assigned'], 1):.1f}%",
                    f"rep has the same region, language & industry"),
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
        "empty_style": {"display": "none"}, "canvas_style": {}, "details_style": {}, "as_of": as_of,
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
        "trace_disabled": False,
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
        fp_state, fp_hint = "info", "Reset and run again: the fingerprint stays the same."
    elif previous_fingerprint == fingerprint:
        fp_state, fp_hint = "ok", (f"Same as the previous {dataset} run in this mode: every decision is identical. "
                                   "Reset and run again: the fingerprint stays the same.")
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
    """What Manager Review is, and what a manager does with each lead in it."""
    if not m["overflow"]:
        return [html.B("No lead needs a manager this run. "), "Every lead found an eligible rep with room."]
    return [html.B(f"{m['overflow']:,} of {m['total_leads']:,} leads ({m['overflow_rate']}%) need a manager. "),
            "Only leads no eligible rep could take land here, and nothing is dropped. For each one: ",
            html.B("1"), " read the suggested action, ", html.B("2"), " reassign the lead or raise a rep's capacity, ",
            html.B("3"), " re-run the routing."]


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
    """"Every value we found": one collapsible table per field. Text fields list each raw spelling and
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
@callback(Output("tabs", "active_tab", allow_duplicate=True),
          Input({"type": "open-tab", "tab": ALL, "key": ALL}, "n_clicks"), prevent_initial_call=True)
def open_tab(clicks):
    if not ctx.triggered_id or not any(clicks):
        return no_update
    return ctx.triggered_id["tab"]


@callback(Output("q-search", "value"), Output("tabs", "active_tab", allow_duplicate=True),
          Input("global-search", "value"), prevent_initial_call=True)
def global_search(text):
    """The search box in the header filters the lead queue and switches to it."""
    return text or "", "queue"


# Runs in the browser (no server round trip): after a search, scroll down to the lead queue
# so the results are on screen. Before the first run there is no queue yet, so it scrolls to
# the "ready for data" box instead.
app.clientside_callback(
    """
    function(nSubmit, value) {
        function scrollToResults() {
            var details = document.getElementById('details');
            var target = (details && details.style.display !== 'none') ? details
                       : document.getElementById('empty-state');
            if (target) { target.scrollIntoView({behavior: 'smooth', block: 'start'}); }
        }
        setTimeout(scrollToResults, 250);   // start moving right away
        setTimeout(scrollToResults, 1200);  // and settle exactly once the filtered table has loaded
        return window.dash_clientside.no_update;
    }
    """,
    Output("scroll-done", "data"),
    Input("global-search", "n_submit"), Input("global-search", "value"),
    prevent_initial_call=True,
)


# ======================================================================================
# Callbacks: lead queue (server-side rows)
# ======================================================================================
@callback(Output("queue-grid", "filterModel"),
          Input("run-store", "data"), Input("q-search", "value"), Input("q-tier", "value"),
          Input("q-region", "value"), Input("q-status", "value"), State("queue-grid", "filterModel"))
def external_filters(store, search, tier, region, status, current):
    """Turn the toolbar into AG Grid's filterModel. Changing it makes the grid fetch fresh rows.
    The hidden _run filter changes with every run, so the grid always refreshes after a run."""
    model = dict(current or {})
    flag = status if status == VIP_UNKNOWN else None   # "VIP unknown" is a flag, not a routing status
    status = None if flag else status
    for column, value, op in (("_search", (search or "").strip().lower(), "contains"), ("priority", tier, "equals"),
                              ("region", region, "equals"), ("status", status, "equals"), ("_flag", flag, "equals")):
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
          Input("run-store", "data"), Input("c-region", "value"), Input("c-seniority", "value"), Input("c-sort", "value"))
def capacity_table(_store, region, seniority, sort):
    _, reps = frames()
    if region and region != "all":
        reps = reps[reps["region"] == region]
    if seniority and seniority != "all":
        reps = reps[reps["seniority"] == seniority]
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
    "Trace a messy lead" opens the same drawer on pick_trace_lead's lead, in CSV -> dashboard order."""
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
        html.H6("Why this priority?"),
        html.Table(className="detail-table mb-1 w-100", children=html.Tbody(
            [row(label, points(p)) for label, p in lead.score_reasons] + [row(html.B("Total"), html.B(f"{lead.score} pts"))])),
        html.Div("Critical 85+ · High 60-84 · Medium 40-59 · Low 0-39 · a VIP referral is always Critical",
                 className="hint mb-3"),
    ])
    route = html.Div(id="trace-route", children=[
        html.H6("Why this rep?" if decision.rep_id else "Why is it waiting for a manager?"),
        html.P(decision.reason, className="plain-explanation mb-3"),
        _match_table(lead, decision),
        *suggestion,
    ])
    clean = html.Div(id="trace-clean", children=[html.H6("What we cleaned up in this lead's data"), fix_table])
    raw = html.Div(id="trace-raw", className="mb-3", children=(
        [html.H6(f"The row as it was in the file (row {lead.row_number:,})"), raw_table] if traced else
        [html.Details(className="hint", children=[html.Summary("Show the original row from the file"), raw_table])]))
    badges = html.Div(className="mb-3", children=[
        html.Span(lead.priority.value, className=f"slds-badge tier-{lead.priority.value.lower()} me-2"),
        html.Span(decision.status.value, className="slds-badge " + ("status-assigned" if decision.rep_id else "status-review")),
    ])
    sections = [raw, clean, score, route] if traced else [score, route, clean, raw]
    title = f"{lead.id} · {lead.company_name}"
    if traced:
        title = f"Traced lead: {title}"
        which = ("The Critical lead with the most data fixes" if lead.priority is Priority.CRITICAL and fixes
                 else "The lead with the most data fixes" if fixes else "The top lead in the queue (no row needed fixes)")
        intro = html.P(f"{which} in this run. Follow it from the CSV row to its rep, step by step.",
                       className="hint mb-2")
        return [trace_trail(lead, decision, fixes), intro, badges, *sections], title
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
# Callbacks: "Run Tests" panel
# ======================================================================================
@callback(Output("tests-modal", "is_open"), Output("tests-trigger", "data"),
          Input("btn-tests", "n_clicks"), Input("tests-again", "n_clicks"), Input("tests-close", "n_clicks"),
          State("tests-trigger", "data"), prevent_initial_call=True)
def toggle_tests(_open, _again, _close, trigger):
    """Open (and start the checks), run again, or close the panel."""
    if ctx.triggered_id == "tests-close":
        return False, no_update
    return True, (trigger or 0) + 1


@callback(Output("tests-body", "children"), Input("tests-trigger", "data"), prevent_initial_call=True)
def run_tests(_trigger):
    """Run every business scenario check and draw one card per check."""
    results, seconds = scenarios.run_all()
    passed = sum(1 for s in results if s.passed and not s.skipped)
    ran = sum(1 for s in results if not s.skipped)
    all_ok = passed == ran
    summary = html.Div(className="tests-summary " + ("ok" if all_ok else "bad"), children=[
        html.Div("✓" if all_ok else "✗", className="tests-summary-mark"),
        html.Div([
            html.P(f"{passed} of {ran} checks passed", className="tests-summary-title"),
            html.P(f"Each check runs the whole program (clean → score → assign) on the real 50,000-lead file "
                   f"or a small made-up case. Took {seconds:.1f} seconds.", className="tests-summary-sub"),
        ]),
    ])
    cards = [dbc.Col(_test_card(s), xs=12, lg=6, className="mb-3") for s in results]
    return [summary, html.P("Checks use Best available matching on fixed scenarios. "
                            "Your current dashboard run stays unchanged.", className="hint mt-2"),
            dbc.Row(cards, className="mt-3")]


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
            html.Div([html.P(f"Test {s.number}", className="test-eyebrow"), html.H3(s.title, className="test-title")]),
            badge,
        ]),
        html.Div(_test_graphic(s), className="test-graphic"),
        blurb("Why this test", s.why),
        blurb("Result", s.result),
        blurb("Why it shows it works", s.meaning),
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
@callback(Output("download", "data"),
          Input("exp-results", "n_clicks"), Input("exp-sf", "n_clicks"), Input("exp-review", "n_clicks"),
          Input("exp-reps", "n_clicks"), prevent_initial_call=True)
def export(*_):
    """The Salesforce file uses Data Loader column names (Id, OwnerId, *__c custom fields)."""
    if STATE.result is None:
        return no_update
    results, reps = frames()
    results = results.drop(columns=["_search", "_flag"])
    name = STATE.result.dataset
    match ctx.triggered_id:
        case "exp-results":
            return dcc.send_data_frame(results.to_csv, f"lead_routing_{name}.csv", index=False)
        case "exp-sf":
            return dcc.send_data_frame(salesforce_frame(results).to_csv, f"salesforce_lead_update_{name}.csv", index=False)
        case "exp-review":
            return dcc.send_data_frame(results[results["status"] == "Manager Review"].to_csv,
                                       f"manager_review_{name}.csv", index=False)
        case "exp-reps":
            return dcc.send_data_frame(reps.to_csv, f"rep_capacity_{name}.csv", index=False)
    return no_update


if __name__ == "__main__":
    import os
    app.run(debug=False, port=int(os.environ.get("PORT", 8050)))
