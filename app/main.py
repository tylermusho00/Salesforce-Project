"""
Lead Router dashboard: a Salesforce Lightning-style "dream dashboard" for the routing engine.

Run (from the repository root):   python -m app.main   ->   http://127.0.0.1:8050

Five pages, each with its own address (the nav bar switches pages; nothing scrolls):
    /         Dashboard       the at-a-glance view: 4 big numbers + 4 charts
    /leads    Leads & Reps    lead queue and rep capacity tables
    /review   Manager Review  leads waiting for a manager: closest reps + assign by hand
    /tests    Tests           runs the real pytest suite and says what each test proves
    /about    About           match quality, speed, how it works, data quality

Every page shares the same top: global header (logo, search), the nav bar, and the page header
(title, "As of ...", Run Sample / Run Scale / Reset, upload, export, Run Tests).

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

from app import charts, test_runner
from app.grid_server import rows_for_request
from lead_router.cleaning import LEAD_COLUMNS, LEAD_REQUIRED, REP_COLUMNS, REP_REQUIRED
from lead_router.config import dataset_paths
from lead_router.explain import FIELD_LABELS, lead_fixes
from lead_router.export import reps_frame, results_frame, salesforce_frame
from lead_router import scenarios
from lead_router.models import AssignmentStatus, MatchLevel, OverflowReason
from lead_router.review import ReviewError, closest_reps, manual_assign, rep_choices
from lead_router.pipeline import STATE, PipelineError, read_csv, reset_state, run_dataset, \
    run_pipeline_frames

TIERS = charts.TIERS
REGIONS = ["North America", "Europe", "Asia-Pacific", "Unknown"]

# address -> (nav label, page title)
PAGES = {
    "/": ("Dashboard", "Lead Routing Dashboard"),
    "/leads": ("Leads & Reps", "Leads & Reps"),
    "/review": ("Manager Review", "Manager Review"),
    "/tests": ("Tests", "Tests: Proof It Works"),
    "/about": ("About", "About Lead Router"),
}
# which page each "View Report" target lives on
TAB_PAGE = {"queue": "/leads", "capacity": "/leads", "review": "/review", "tests": "/tests", "about": "/about"}

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
            results["ml_flag"] = results["unusual_record"].map(lambda note: "Unusual" if note else "")
            reps = reps_frame(STATE.result)
            reps["ml_flag"] = reps["unusual_record"].map(lambda note: "Unusual" if note else "")
            _CACHE.update(run_id=STATE.run_id, results=results, reps=reps)
        return _CACHE["results"], _CACHE["reps"]


# ======================================================================================
# Layout building blocks
# ======================================================================================
def report_link(text: str, tab: str, key: str) -> html.A:
    """A link that opens the page and tab with the details behind a number (like 'View Report')."""
    return html.A(text, id={"type": "open-tab", "tab": tab, "key": key})


def component(title: str, body, tab: str | None, report: str | None, key: str) -> html.Div:
    """One dashboard component: title (+ expand icon), body, and a 'View Report' footer when it has one."""
    head = [html.H2(title, className="component-title")]
    if tab:
        head.append(html.A("⤢", className="component-expand", title="Open details",
                           id={"type": "open-tab", "tab": tab, "key": f"{key}-x"}))
    children = [html.Div(className="component-head", children=head), html.Div(body, className="component-body")]
    if tab and report:
        children.append(html.Div(report_link(f"View Report ({report})", tab, key), className="component-foot"))
    return html.Div(className="component", children=children)


def chart(graph_id: str, height: int = 270) -> dcc.Graph:
    # responsive=True: redraw to the card's width (the canvas is hidden until the first run,
    # so a fixed size measured at page load would be wrong).
    return dcc.Graph(id=graph_id, figure=charts.empty(height), config={"displayModeBar": False},
                     responsive=True, style={"height": f"{height}px", "width": "100%"})


def metric(key: str, title: str, tab: str | None, report: str | None, small: bool = False) -> html.Div:
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
        html.Div(DASHBOARD_ICON, className="page-icon"),
        html.Div(className="page-titles", children=[
            html.Div("Dashboard", id="page-eyebrow", className="page-eyebrow"),
            html.H1("Lead Routing Dashboard", id="page-title", className="page-title"),
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
                        title="Open the Tests page and run the full pytest suite"),
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
    """The Dashboard page: four numbers a manager checks first, then four charts.
    All boxes sit in ONE grid row, so the gap between them is the same everywhere (--gap in style.css)."""
    col = lambda child, lg, md=12: dbc.Col(child, xs=12, md=md, lg=lg)
    return html.Div(id="canvas", style={"display": "none"}, children=dbc.Row(className="canvas-grid", children=[
        # Row 1: the big numbers
        col(metric("assigned", "Leads Assigned to Reps", "queue", "Lead Queue"), 3, 6),
        col(metric("review", "Waiting in Manager Review", "review", "Manager Review"), 3, 6),
        col(metric("critical", "Critical Leads to Seniors", "queue", "Lead Queue"), 3, 6),
        col(metric("util", "Team Capacity Used", "capacity", "Rep Capacity"), 3, 6),
        # Row 2: the leads
        col(component("Leads by Priority", chart("ch-tiers"), "queue", "Lead Queue", "tiers"), 6),
        col(component("Leads by Region", chart("ch-region"), "queue", "Lead Queue", "region"), 6),
        # Row 3: the team
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
    {"field": "ml_flag", "headerName": "ML check", "width": 125, "cellRenderer": "MlFlag",
     "tooltipField": "unusual_record"},
    {"field": "rep_name", "headerName": "Assigned Rep", "flex": 1.4, "minWidth": 170},
    {"field": "rep_seniority", "headerName": "Seniority", "width": 110},
    {"field": "region", "headerName": "Region", "width": 135},
    {"field": "language", "headerName": "Language", "width": 110},
    {"field": "industry", "headerName": "Industry", "width": 130},
    {"field": "notes", "headerName": "Notes", "flex": 1.4, "minWidth": 170, "tooltipField": "notes"},
    {"field": "reason", "headerName": "Why this rep?", "flex": 3, "minWidth": 320, "tooltipField": "reason"},
    {"field": "unusual_record", "hide": True},                            # the ML reason (tooltip)
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
            dbc.Col(dbc.InputGroup([dbc.InputGroupText("ML check"), dbc.Select(id="q-ml", value="all", options=[
                {"label": "All leads", "value": "all"},
                {"label": "⚠ Flagged as unusual", "value": "Unusual"}])], size="sm", className="ml-filter"),
                    xs=12, md=4),
        ]),
        html.Div(["Click any row to see its score breakdown and routing explanation. ",
                  html.Span("⚠ Unusual", className="ml-pill"),
                  " = flagged by our machine-learning check (hover it to see why)."], className="hint mb-2"),
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


def review_page() -> html.Div:
    """Leads waiting for a manager (left) and the assign panel for the selected lead (right)."""
    cols = [
        {"field": "lead_id", "headerName": "Lead", "width": 110, "pinned": "left"},
        {"field": "company_name", "headerName": "Company", "flex": 2, "minWidth": 170},
        {"field": "priority", "headerName": "Tier", "width": 105, "cellRenderer": "TierBadge"},
        {"field": "score", "headerName": "Score", "width": 85, "filter": "agNumberColumnFilter"},
        {"field": "overflow_reason", "headerName": "Why it's waiting", "flex": 1.6, "minWidth": 200},
        {"field": "region", "headerName": "Region", "width": 130},
        {"field": "language", "headerName": "Language", "width": 105},
        {"field": "industry", "headerName": "Industry", "width": 125},
    ]
    queue = html.Div(className="component", children=[
        html.Div(className="component-head", children=html.H2(
            ["Leads waiting for a manager ", html.Span(id="review-count")], className="component-title")),
        html.Div(className="component-body review-body", children=[
            html.P("No allowed rep had room for these leads, so the router stopped and asked. Nothing is dropped. "
                   "Pick a lead to see the closest reps who can still take it.", className="hint mb-2"),
            dag.AgGrid(id="review-grid", columnDefs=cols, rowData=[], className=GRID_THEME,
                       defaultColDef={"sortable": True, "filter": True, "floatingFilter": True, "resizable": True,
                                      "suppressHeaderMenuButton": True},
                       dashGridOptions={"pagination": True, "paginationPageSize": 20,
                                        "rowSelection": {"mode": "singleRow", "checkboxes": False,
                                                         "enableClickSelection": True}},
                       style={"height": "560px"}),
        ]),
    ])
    assign = html.Div(className="component", children=[
        html.Div(className="component-head", children=html.H2("Assign this lead", className="component-title")),
        html.Div(className="component-body review-panel", children=[html.Div(id="review-msg"),
                                                                   html.Div(id="review-panel")]),
    ])
    return html.Div(className="page-stack", children=[
        dbc.Row(className="canvas-grid", children=[
            dbc.Col(queue, xs=12, xl=7),
            dbc.Col(assign, xs=12, xl=5),
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
        dbc.Row(className="g-2 mb-2 toolbar", children=[
            dbc.Col(_select("c-region", "Region", REGIONS), xs=6, md=3),
            dbc.Col(_select("c-seniority", "Seniority", ["Junior", "Senior", "Manager"]), xs=6, md=3),
            dbc.Col(dbc.InputGroup([dbc.InputGroupText("Sort"), dbc.Select(id="c-sort", value="most", options=[
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


def about_page() -> html.Div:
    """The About page: how good the matches are, how fast it runs, how it works, and the data clean-up."""
    step = lambda n, title, text: html.Div(className="about-step", children=[
        html.P(n, className="about-step-n"), html.Div([html.H3(title, className="about-step-title"),
                                                      html.P(text, className="about-step-text")])])
    how = html.Div(className="component about-how", children=[
        html.Div(className="component-head", children=html.H2("How Lead Router works", className="component-title")),
        html.Div(className="about-steps", children=[
            step("1", "Clean", "Fixes typos, short forms and blanks. Never drops a lead."),
            step("2", "Score", "Points for company size, activity and industry → Critical, High, Medium or Low. "
                               "VIP referrals are always Critical."),
            step("3", "Route", "Most urgent first, to the best-matching rep with room. Critical → Senior → Manager, "
                               "never Junior. Equal reps share work evenly."),
            step("4", "Double-check (ML)", "A machine-learning model flags records that look unusual, like a company "
                                           "with 10 billion employees, so a person can check them. It never changes a route."),
        ]),
        html.P("Want proof? Open the Tests tab: it runs our full pytest suite live.", className="hint about-proof"),
    ])
    quality = html.Div(className="component", children=[
        html.Div(className="component-head", children=html.H2("Data Quality: what we cleaned up", className="component-title")),
        html.Div(className="component-body about-quality", children=dbc.Row([
            dbc.Col([html.H3("Leads", className="about-sub mb-2"), html.Div(id="dq-leads")], lg=6),
            dbc.Col([html.H3("Representatives", className="about-sub mb-2"), html.Div(id="dq-reps")], lg=6),
        ])),
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
                dbc.Col(metric("matches", "Perfect Matches", "queue", "Lead Queue"), xs=12, md=6),
                dbc.Col(metric("speed", "Leads Routed per Second", None, None), xs=12, md=6),
            ]),
            unusual,
            quality,
        ]),
    ])


def details() -> html.Div:
    return html.Div(id="details", className="component details", style={"display": "none"}, children=
        dbc.Tabs(id="tabs", active_tab="queue", children=[
            dbc.Tab(queue_tab(), label="Prioritized Lead Queue", tab_id="queue"),
            dbc.Tab(capacity_tab(), label="Rep Capacity", tab_id="capacity"),
        ]))


def tests_page() -> html.Div:
    """Runs the real pytest suite on demand. Results are grouped by what they prove (app/test_runner.py)."""
    return html.Div(className="page-stack", children=[
        html.Div(className="component", children=[
            html.Div(className="component-head", children=html.H2("Is Lead Router really working?", className="component-title")),
            html.Div(className="component-body tests-intro", children=[
                html.P(["This button runs our ", html.B("real pytest suite"), ": the same automated tests we run before "
                        "every change. Each test feeds the program an input where we already know the right answer "
                        "and checks the program gets it. If any rule broke, a test would turn red here."],
                       className="mb-2"),
                html.Button("▶ Run all tests", id="tests-run", className="slds-btn brand"),
                html.Span("Takes about 20-30 seconds: several tests run the full 50,000-lead file.", className="hint ms-2"),
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
            html.Div(id="page-review", style={"display": "none"}, children=review_page()),
            html.Div(id="page-tests", style={"display": "none"}, children=tests_page()),
            html.Div(id="page-about", style={"display": "none"}, children=about_page()),
        ]),
        dbc.Offcanvas(id="detail", title="Lead detail", placement="end", is_open=False, style={"width": "460px"}),
        dcc.Store(id="run-store", data={"run_id": 0}),
        dcc.Store(id="up-leads-store"),
        dcc.Store(id="up-reps-store"),
        dcc.Store(id="scroll-done"),  # dummy output for the scroll-to-top callback
        dcc.Store(id="tests-trigger"),  # bumps when the tests should (re)run
    ])


app = Dash(__name__, title="Lead Routing Dashboard", external_stylesheets=[dbc.themes.BOOTSTRAP],
           suppress_callback_exceptions=True)  # some components (assign buttons) are created later
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
METRIC_KEYS = ["assigned", "review", "critical", "util", "matches", "speed"]
CHART_IDS = ["ch-tiers", "ch-region", "ch-capacity", "ch-fullness"]


@callback(
    Output("empty-state", "style"), Output("canvas", "style"), Output("details", "style"),
    Output("about-data", "style"), Output("as-of", "children"),
    *[Output(f"m-{k}", "children") for k in METRIC_KEYS], *[Output(f"m-{k}-cap", "children") for k in METRIC_KEYS],
    *[Output(c, "figure") for c in CHART_IDS],
    Output("review-count", "children"), Output("review-grid", "rowData"), Output("dq-leads", "children"), Output("dq-reps", "children"),
    Output("ml-summary", "children"), Output("ml-leads", "children"), Output("ml-reps", "children"),
    Input("run-store", "data"),
)
def render(_store):
    r = STATE.result
    if r is None:
        blank = charts.empty()
        return ({}, {"display": "none"}, {"display": "none"}, {"display": "none"},
                "No data yet · Viewing as Sales Operations",
                *["—"] * len(METRIC_KEYS), *[""] * len(METRIC_KEYS), *[blank] * len(CHART_IDS),
                "", [], None, None, None, None, None)

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
        # Share of assigned leads whose rep matches region, language AND industry
        "matches": (f"{100 * by_traits[3] / max(m['assigned'], 1):.1f}%",
                    f"rep has the same region, language & industry"),
    }

    # --- charts
    region_counts = results["region"].value_counts().reindex(REGIONS, fill_value=0)
    region_counts = region_counts[region_counts > 0]
    figures = [
        charts.tier_bar(m["tiers"]),
        charts.donut(list(region_counts.index), list(region_counts.values),
                     f"<b>{m['total_leads']:,}</b><br><span style='font-size:12px'>leads</span>"),
        charts.capacity_by_seniority(reps),
        charts.rep_fullness(reps),
    ]

    review_columns = ["lead_id", "company_name", "priority", "score", "overflow_reason", "region", "language",
                      "industry"]  # only what the table shows: keeps the page light
    review_rows = results.loc[results["status"] == "Manager Review", review_columns].to_dict("records")
    return ({"display": "none"}, {}, {}, {}, as_of,
            *[metrics[k][0] for k in METRIC_KEYS], *[metrics[k][1] for k in METRIC_KEYS],
            *figures,
            f"({m['overflow']:,})", review_rows, _quality_table(lq), _quality_table(rq), *_unusual_view(r))


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
          Input({"type": "open-tab", "tab": ALL, "key": ALL}, "n_clicks"), prevent_initial_call=True)
def open_tab(clicks):
    """'View Report' links and expand icons: go to the page (and tab) with the details."""
    if not ctx.triggered_id or not any(clicks):
        return no_update, no_update
    tab = ctx.triggered_id["tab"]
    return TAB_PAGE.get(tab, "/"), (tab if tab in ("queue", "capacity") else no_update)


@callback(Output("q-search", "value"), Output("tabs", "active_tab", allow_duplicate=True),
          Output("url", "pathname", allow_duplicate=True),
          Input("global-search", "value"), prevent_initial_call=True)
def global_search(text):
    """The search box in the header filters the lead queue and opens the Leads & Reps page."""
    return text or "", "queue", "/leads"


# Runs in the browser: every time the page changes, start at the top, like opening a new page.
app.clientside_callback(
    """
    function(pathname) {
        window.scrollTo({top: 0, behavior: 'instant'});
        return window.dash_clientside.no_update;
    }
    """,
    Output("scroll-done", "data"),
    Input("url", "pathname"),
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
    for column, value, op in (("_search", (search or "").strip().lower(), "contains"), ("priority", tier, "equals"),
                              ("region", region, "equals"), ("status", status, "equals"),
                              ("ml_flag", ml, "equals")):
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
          Input("queue-grid", "selectedRows"), prevent_initial_call=True)
def lead_detail(selection):
    """Clicking a row explains the score, the routing decision, and how the raw row was cleaned."""
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
        _anomaly_note(lead),
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
    waiting = _waiting_leads()
    if STATE.result is None:
        return html.P("Run Sample or Run Scale first: leads that need a manager will show up here.", className="hint")
    if not waiting:
        return html.Div(className="review-done", children=[
            html.Div("✓", className="review-done-mark"),
            html.P("No leads are waiting. Every lead has a rep.", className="mb-0"),
        ])
    picked = selection[0]["lead_id"] if selection and selection[0]["lead_id"] in waiting else None
    lead_id = picked or waiting[0]
    r = STATE.result
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
          Input({"type": "assign-rep", "rep": ALL}, "n_clicks"), Input("review-assign-any", "n_clicks"),
          State("review-any", "value"), State("review-grid", "selectedRows"), prevent_initial_call=True)
def assign_from_review(_card_clicks, _any_click, any_rep, selection):
    """A manager clicked Assign: give the lead to that rep (same safety rules as automatic routing)."""
    if not ctx.triggered or not ctx.triggered[0]["value"] or STATE.result is None:
        return no_update, no_update
    waiting = _waiting_leads()
    picked = selection[0]["lead_id"] if selection and selection[0]["lead_id"] in waiting else None
    lead_id = picked or (waiting[0] if waiting else None)
    rep_id = ctx.triggered_id["rep"] if isinstance(ctx.triggered_id, dict) else any_rep
    if lead_id is None:
        return no_update, no_update
    if not rep_id:
        return no_update, dbc.Alert("Choose a rep from the list first.", color="warning", dismissable=True)
    try:
        decision = manual_assign(STATE.result, lead_id, rep_id)
    except ReviewError as error:
        return no_update, dbc.Alert(str(error), color="danger", dismissable=True)
    run_id = STATE.set(STATE.result)  # redraws every page with the new numbers
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


@callback(Output("tests-body", "children"), Input("tests-trigger", "data"), prevent_initial_call=True,
          running=[(Output("tests-run", "disabled"), True, False)])
def run_tests(_trigger):
    """Run pytest (in its own process) and the illustrated business scenarios side by side."""
    started = time.perf_counter()
    process, report = test_runner.start_suite()
    with ThreadPoolExecutor(1) as pool:
        scenario_job = pool.submit(scenarios.run_all)
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
