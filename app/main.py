"""
Lead Router dashboard: a Salesforce Lightning-style "dream dashboard" for the routing engine.

Run (from the repository root):   python -m app.main   ->   http://127.0.0.1:8050

Layout, top to bottom (mirrors a Lightning dashboard page):
    global header   logo, search (jumps to the lead queue), icons
    app nav bar     Lead Router app with tabs; "Dashboards" is active
    page header     title, description, "As of ...", Run Sample / Run Scale / Reset, upload, export
    canvas          white components on a blue-gray background:
                      row 1  Leads by Priority | From Upload to Rep | Leads Assigned + Manager Review
                      row 2  Leads by Region | Who Got the Leads | Assigned vs. Manager Review
                      row 3  four big numbers: Critical→Senior, speed, utilization, values auto-fixed
                      row 4  How Busy Each Team Is | How Full Are Our Reps?
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
from datetime import datetime
from typing import Any

import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash import ALL, Dash, Input, Output, State, callback, ctx, dcc, html, no_update

from app import charts
from app.grid_server import rows_for_request
from lead_router.cleaning import LEAD_COLUMNS, LEAD_REQUIRED, REP_COLUMNS, REP_REQUIRED
from lead_router.config import dataset_paths
from lead_router.explain import FIELD_LABELS, lead_fixes
from lead_router.export import reps_frame, results_frame, salesforce_frame
from lead_router import scenarios
from lead_router.models import MatchLevel, OverflowReason
from lead_router.pipeline import STATE, PipelineError, read_csv, reset_state, run_dataset, \
    run_pipeline_frames

TIERS = charts.TIERS
REGIONS = ["North America", "Europe", "Asia-Pacific", "Unknown"]

# ======================================================================================
# Result tables, built once per run and cached (keyed by STATE.run_id)
# ======================================================================================
_CACHE: dict[str, Any] = {"run_id": None, "results": None, "reps": None}
_CACHE_LOCK = threading.Lock()  # several callbacks fire at once after a run; build the tables only once


def frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per lead, one row per rep) for the latest run."""
    with _CACHE_LOCK:
        if _CACHE["run_id"] != STATE.run_id:
            results = results_frame(STATE.result)
            results["_search"] = (results["lead_id"] + " " + results["company_name"] + " " +
                                  results["rep_name"].fillna("") + " " + results["rep_id"].fillna("")).str.lower()
            _CACHE.update(run_id=STATE.run_id, results=results, reps=reps_frame(STATE.result))
        return _CACHE["results"], _CACHE["reps"]


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
        col(component("Leads by Priority", chart("ch-tiers"), "queue", "Prioritized Lead Queue", "tiers"), 5),
        col(component("From Upload to Rep", chart("ch-funnel"), "queue", "Prioritized Lead Queue", "funnel"), 4, 6),
        col(html.Div(className="metric-stack", children=[
            metric("assigned", "Leads Assigned to Reps", "queue", "Prioritized Lead Queue", small=True),
            metric("review", "Waiting in Manager Review", "review", "Manager Review", small=True),
        ]), 3, 6),
        # Row 2
        col(component("Leads by Region", chart("ch-region"), "queue", "Prioritized Lead Queue", "region"), 4, 6),
        col(component("Who Got the Leads (by Priority)", chart("ch-destination"), "capacity", "Rep Capacity", "dest"), 4, 6),
        col(component("Assigned vs. Manager Review", chart("ch-split"), "review", "Manager Review", "split"), 4),
        # Row 3: five big numbers, equal widths (nested row so they share one line)
        dbc.Col(xs=12, children=dbc.Row(className="canvas-grid", children=[
            dbc.Col(metric("critical", "Critical Leads to Seniors", "queue", "Prioritized Lead Queue"),
                    xs=12, md=6, lg=True),
            dbc.Col(metric("matches", "Perfect Matches", "queue", "Prioritized Lead Queue"), xs=12, md=6, lg=True),
            dbc.Col(metric("speed", "Leads Routed per Second", "quality", "Data Quality"), xs=12, md=4, lg=True),
            dbc.Col(metric("util", "Team Capacity Used", "capacity", "Rep Capacity"), xs=12, md=4, lg=True),
            dbc.Col(metric("fixed", "Messy Values Auto-Fixed", "quality", "Data Quality"), xs=12, md=4, lg=True),
        ])),
        # Row 4
        col(component("How Busy Each Team Is", chart("ch-capacity", 300), "capacity", "Rep Capacity", "top"), 6),
        col(component("How Full Are Our Reps?", chart("ch-fullness", 300), "capacity", "Rep Capacity", "util-h"), 6),
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
            dbc.Col(_select("q-status", "Status", ["Assigned", "Manager Review"]), xs=6, md=2),
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
        {"field": "region", "headerName": "Region", "width": 135},
        {"field": "language", "headerName": "Language", "width": 110},
        {"field": "industry", "headerName": "Industry", "width": 130},
        {"field": "reason", "headerName": "What happened", "flex": 3, "minWidth": 320, "tooltipField": "reason"},
    ]
    return html.Div([
        html.Div("Leads no eligible rep could take. Nothing is dropped: each one needs a manager decision.",
                 className="review-banner mb-2"),
        dag.AgGrid(id="review-grid", columnDefs=cols, rowData=[], className=GRID_THEME,
                   defaultColDef={"sortable": True, "filter": True, "floatingFilter": True, "resizable": True,
                                  "suppressHeaderMenuButton": True},
                   dashGridOptions={"pagination": True, "paginationPageSize": 20, "tooltipShowDelay": 300,
                                    "rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                     "enableClickSelection": True}},
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
    return dbc.Row([
        dbc.Col([html.H3("Leads", className="component-title mb-2"), html.Div(id="dq-leads")], lg=6),
        dbc.Col([html.H3("Representatives", className="component-title mb-2"), html.Div(id="dq-reps")], lg=6),
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
    prevent_initial_call=True,
    running=[(Output("btn-sample", "disabled"), True, False), (Output("btn-scale", "disabled"), True, False),
             (Output("btn-reset", "disabled"), True, False)],
)
def run_or_reset(_s, _sc, _r, _u, up_leads, up_reps):
    """Runs when one of the buttons is clicked (ctx.triggered_id says which). Errors become an alert."""
    trigger = ctx.triggered_id
    if trigger == "btn-reset":
        reset_state()
        return {"run_id": STATE.run_id}, None
    try:
        if trigger == "btn-upload":
            if not up_leads:
                raise PipelineError("Upload a leads CSV first.")
            leads_df = _decode_upload(up_leads, "Leads", LEAD_COLUMNS, LEAD_REQUIRED)
            reps_df = (_decode_upload(up_reps, "Reps", REP_COLUMNS, REP_REQUIRED) if up_reps
                       else read_csv(dataset_paths("scale")[1], "Reps", REP_COLUMNS, REP_REQUIRED))
            result = run_pipeline_frames(leads_df, reps_df, dataset="upload")
        else:
            result = run_dataset("sample" if trigger == "btn-sample" else "scale")
    except (PipelineError, KeyError) as error:
        return no_update, dbc.Alert(str(error), color="danger", dismissable=True)
    STATE.set(result)
    frames()  # build the result tables now, before the redraw callbacks ask for them
    warnings = result.lead_quality.warnings + result.rep_quality.warnings
    return {"run_id": STATE.run_id}, (dbc.Alert(" ".join(warnings), color="warning", dismissable=True) if warnings else None)


# ======================================================================================
# Callback: redraw the whole dashboard after a run or reset
# ======================================================================================
METRIC_KEYS = ["assigned", "review", "critical", "matches", "speed", "util", "fixed"]
CHART_IDS = ["ch-tiers", "ch-funnel", "ch-region", "ch-destination", "ch-split", "ch-capacity", "ch-fullness"]


@callback(
    Output("empty-state", "style"), Output("canvas", "style"), Output("details", "style"), Output("as-of", "children"),
    *[Output(f"m-{k}", "children") for k in METRIC_KEYS], *[Output(f"m-{k}-cap", "children") for k in METRIC_KEYS],
    *[Output(c, "figure") for c in CHART_IDS],
    Output("tab-review", "label"), Output("review-grid", "rowData"), Output("dq-leads", "children"), Output("dq-reps", "children"),
    Input("run-store", "data"),
)
def render(_store):
    r = STATE.result
    if r is None:
        blank = charts.empty()
        return ({}, {"display": "none"}, {"display": "none"}, "No data yet · Viewing as Sales Operations",
                *["—"] * len(METRIC_KEYS), *[""] * len(METRIC_KEYS), *[blank] * len(CHART_IDS),
                "Manager Review", [], None, None)

    m, (results, reps) = r.metrics, frames()
    lq, rq = r.lead_quality, r.rep_quality
    seconds = r.timings["total"]

    as_of = (f"As of {datetime.now():%b %d, %Y, %I:%M %p} · {r.dataset.title()} dataset · "
             f"{m['total_leads']:,} leads routed in {seconds:.2f} s · Viewing as Sales Operations")

    # --- match quality: 3 of 3 traits vs. 2 of 3 (see routing.pools_to_try)
    levels = m["match_levels"]
    by_traits = {3: 0, 2: 0, 1: 0}
    for name, count in levels.items():
        by_traits[MatchLevel(name).traits_matched] += count

    # --- big numbers: (value, caption)
    metrics = {
        "assigned": (f"{m['assigned']:,}", f"{m['assigned_rate']}% of {m['total_leads']:,} leads"),
        "review": (f"{m['overflow']:,}", f"{m['overflow_rate']}% need a manager decision"),
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

    # --- charts
    stages = [
        ("Leads uploaded", lq.rows_in),
        ("Kept after cleanup", m["total_leads"]),
        ("Given to a rep", m["assigned"]),
    ]
    region_counts = results["region"].value_counts().reindex(REGIONS, fill_value=0)
    region_counts = region_counts[region_counts > 0]
    figures = [
        charts.tier_bar(m["tiers"]),
        charts.routing_funnel(stages),
        charts.donut(list(region_counts.index), list(region_counts.values),
                     f"<b>{m['total_leads']:,}</b><br><span style='font-size:12px'>leads</span>"),
        charts.destination_columns(results),
        charts.assigned_vs_review(m["assigned"], m["overflow"]),
        charts.capacity_by_seniority(reps),
        charts.rep_fullness(reps),
    ]

    review_columns = ["lead_id", "company_name", "priority", "score", "overflow_reason", "region", "language",
                      "industry", "reason"]  # only what the table shows: keeps the page light
    review_rows = results.loc[results["status"] == "Manager Review", review_columns].to_dict("records")
    return ({"display": "none"}, {}, {}, as_of,
            *[metrics[k][0] for k in METRIC_KEYS], *[metrics[k][1] for k in METRIC_KEYS],
            *figures,
            f"Manager Review ({m['overflow']:,})", review_rows, _quality_table(lq), _quality_table(rq))


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
    for column, value, op in (("_search", (search or "").strip().lower(), "contains"), ("priority", tier, "equals"),
                              ("region", region, "equals"), ("status", status, "equals")):
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
          Input("queue-grid", "selectedRows"), Input("review-grid", "selectedRows"), prevent_initial_call=True)
def lead_detail(queue_selection, review_selection):
    """Clicking a row explains the score, the routing decision, and how the raw row was cleaned."""
    selection = queue_selection if ctx.triggered_id == "queue-grid" else review_selection
    if not selection or STATE.result is None:
        return False, no_update, no_update
    lead_id = selection[0]["lead_id"]
    lead = next((l for l in STATE.result.leads if l.id == lead_id), None)
    decision = next((a for a in STATE.result.assignments if a.lead_id == lead_id), None)
    if lead is None or decision is None:
        return False, no_update, no_update

    row = lambda label, value: html.Tr([html.Td(label), html.Td(value)])
    points = lambda p: html.Span(f"{p:+d}", className="points-pos" if p > 0 else "points-zero")
    fixes = lead_fixes(lead)
    fix_table = (html.Table(className="detail-table fix-table mb-3 w-100", children=[
                     html.Thead(html.Tr([html.Th("Field"), html.Th("In the file"), html.Th("What we used")])),
                     html.Tbody([html.Tr([html.Td(f), html.Td(html.Code(b)), html.Td(a)]) for f, b, a in fixes]),
                 ]) if fixes else html.P("Nothing: this row was already clean.", className="hint mb-3"))
    body = [
        html.Div(className="mb-3", children=[
            html.Span(lead.priority.value, className=f"slds-badge tier-{lead.priority.value.lower()} me-2"),
            html.Span(decision.status.value, className="slds-badge " + ("status-assigned" if decision.rep_id else "status-review")),
        ]),
        html.H6("Why this priority?"),
        html.Table(className="detail-table mb-1 w-100", children=html.Tbody(
            [row(label, points(p)) for label, p in lead.score_reasons] + [row(html.B("Total"), html.B(f"{lead.score} pts"))])),
        html.Div("Critical 85+ · High 60-84 · Medium 40-59 · Low 0-39 · a VIP referral is always Critical",
                 className="hint mb-3"),
        html.H6("Why this rep?" if decision.rep_id else "Why is it waiting for a manager?"),
        html.P(decision.reason, className="plain-explanation mb-2"),
        _match_table(lead, decision),
        html.H6("What we cleaned up in this lead's data"),
        fix_table,
        html.Details(className="hint", children=[
            html.Summary("Show the original row from the file"),
            html.Table(className="detail-table w-100 mt-2", children=html.Tbody(
                [row(k.replace("_", " "), html.Code("(blank)" if v in (None, "") else str(v)))
                 for k, v in lead.raw.items()])),
        ]),
    ]
    return True, body, f"{lead.id} · {lead.company_name}"


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
    return [summary, dbc.Row(cards, className="mt-3")]


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
    results = results.drop(columns=["_search"])
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
