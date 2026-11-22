"""Capacity what-if UI. Results belong to a run ID; replacing/resetting a run hides them."""
from dataclasses import asdict

import dash_bootstrap_components as dbc
from dash import Input, Output, State, callback, dcc, html

from lead_router.capacity import CapacityError, compare_capacity
from lead_router.pipeline import STATE


def capacity_simulator():
    return html.Section(className="capacity-sim", children=[
        html.Div(className="sim-heading", children=[html.H3("Capacity what-if", className="component-title"),
            html.Span("Simulation only", className="sim-label")]),
        html.P("Each rep is tested separately. Active run unchanged.", className="hint"),
        dbc.Row(className="g-3 align-items-end", children=[
            dbc.Col([html.Label("Reps (up to 3)", htmlFor="sim-reps"),
                     dcc.Dropdown(id="sim-reps", multi=True, placeholder="Search reps or skills")], xs=12, lg=8),
            dbc.Col([html.Label("Extra slots", htmlFor="sim-slots"),
                     dbc.Input(id="sim-slots", type="number", min=1, max=50, step=1, value=5)], xs=6, lg=2),
            dbc.Col(html.Button("Compare capacity", id="sim-run", className="slds-btn brand"), xs=6, lg=2),
        ]),
        html.Details(className="sim-help hint", children=[html.Summary("How comparisons work"),
            html.P("Each selected rep gets the same extra slots in a separate scenario. Scores, lead order, "
                   "strategy and matching minimum stay fixed; current routing rules apply."),
            html.P("Ranked by Critical leads resolved, net assignments, then fewest existing owners changed. "
                   "Rep ID breaks ties. Results compare selected reps, not every possible allocation. "
                   "No new hires or policy exceptions are simulated.")]),
        dcc.Store(id="sim-result"),
        dcc.Loading(html.Div(id="sim-report", className="mt-3", **{"aria-live": "polite"}), type="dot"),
    ])


@callback(Output("sim-reps", "options"), Output("sim-reps", "value"), Input("run-store", "data"))
def simulator_options(_store):
    result = STATE.result
    if result is None:
        return [], []
    return [{"value": r.id, "label": f"{r.name} [{r.id}] · {r.seniority.value} · {r.region or 'Unknown region'} · "
             f"{r.language or 'Unknown language'} · {r.industry or 'No expertise'} · max {r.max_capacity}"}
            for r in result.reps], []


@callback(Output("sim-result", "data"), Input("sim-run", "n_clicks"),
          State("sim-reps", "value"), State("sim-slots", "value"), prevent_initial_call=True,
          running=[(Output("sim-run", "disabled"), True, False),
                   (Output("sim-run", "children"), "Comparing…", "Compare capacity")])
def simulate_capacity(_clicks, rep_ids, slots):
    result, run_id = STATE.result, STATE.run_id
    if result is None:
        return {"run_id": run_id, "error": "Run a dataset or upload first."}
    try:
        # Browser number inputs may send 5.0. Reject fractions and booleans without truncation.
        if isinstance(slots, bool) or not isinstance(slots, (int, float)) or not 1 <= slots <= 50 or int(slots) != slots:
            raise CapacityError("Extra slots must be a whole number from 1 to 50.")
        comparison = compare_capacity(result, rep_ids or [], int(slots))
    except CapacityError as error:
        return {"run_id": run_id, "error": str(error)}
    if STATE.result is not result or STATE.run_id != run_id:
        return {"run_id": run_id, "error": "The active run changed. Compare again on the new run."}
    return {"run_id": run_id, "comparison": asdict(comparison),
            "inputs": {"rep_ids": sorted(rep_ids), "slots": int(slots)}}


@callback(Output("sim-report", "children"), Input("sim-result", "data"), Input("run-store", "data"),
          Input("sim-reps", "value"), Input("sim-slots", "value"))
def simulator_report(data, _store, rep_ids=None, slots=None):
    if not data or data.get("run_id") != STATE.run_id:
        return html.P("Choose representatives to compare.", className="hint")
    if "inputs" in data and rep_ids is not None and data["inputs"] != {"rep_ids": sorted(rep_ids), "slots": slots}:
        return html.P("Selection changed. Compare again for updated results.", className="hint")
    if data.get("error"):
        return dbc.Alert(data["error"], color="warning")
    c = data["comparison"]
    rows = []
    for rank, scenario in enumerate(c["scenarios"], 1):
        s = scenario
        verdict = "Backlog reduced" if s["critical_resolved"] > 0 or s["net_assigned"] > 0 else "No backlog reduction"
        examples = [html.Tr([html.Td(e[k]) for k in ("lead_id", "priority", "before", "after", "match")]) for e in s["examples"]]
        rows.append(html.Article(className="sim-case", children=[
            html.Div(className="sim-case-heading", children=[
                html.H4(f"{rank}. {s['rep_name']} [{s['rep_id']}]"), html.Span(verdict, className="sim-outcome")]),
            html.P(f"{s['seniority']} · Capacity {s['old_capacity']} → {s['new_capacity']} (+{s['extra_slots']})", className="hint"),
            html.Div(className="sim-numbers", children=[
                html.Div([html.Strong(f"{c['baseline_critical_waiting']:,} → {s['critical_waiting']:,}"), html.Span("Critical waiting")]),
                html.Div([html.Strong(f"{c['baseline_review']:,} → {s['review']:,}"), html.Span("Manager Review")]),
                html.Div([html.Strong(f"{s['net_assigned']:+,}"), html.Span("Net assignments")]),
                html.Div([html.Strong(f"{s['moved_owners']:,}"), html.Span("Existing owners changed")]),
            ]),
            html.Details(children=[
                html.Summary(f"{s['changed_decisions']:,} changed decisions"),
                html.P(f"{s['newly_assigned']:,} leave review; {s['newly_waiting']:,} enter review. "
                       f"{s['net_assigned_per_slot']:+.3f} net assignments per extra slot.", className="hint"),
                dbc.Table([html.Thead(html.Tr([html.Th(t) for t in ("Lead", "Priority", "Before", "After", "Match after")])),
                           html.Tbody(examples)], responsive=True, size="sm"),
                html.Small(f"Showing {len(examples)} of {s['changed_decisions']:,} changes.", className="hint"),
            ]) if examples else None,
        ]))
    return [html.P(f"Baseline · {c['baseline_assigned']:,} assigned · {c['baseline_review']:,} in review", className="sim-baseline"),
            *rows]
