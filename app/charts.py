"""
charts.py - every Plotly figure on the dashboard, styled like Salesforce Lightning report charts.

Each function takes plain data (counts, DataFrames) and returns a figure. No Dash code here,
so you can try any chart in a notebook:  charts.tier_bar({"Critical": 10, ...}).show()
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

# Colors sampled from the Salesforce "Sales Team Dream Dashboard" screenshot.
BLUE, LIGHT_BLUE = "#4E97E7", "#85B9ED"
PURPLE, LAVENDER = "#935CEB", "#BD9DEF"
TEAL, LIGHT_TEAL = "#52A9A3", "#75D1CD"
ORANGE, PEACH = "#EDA760", "#F3CBA1"
GREEN = "#2E844A"
GREY, LIGHT_GREY = "#706E6B", "#ECEBEA"
PALETTE = [BLUE, LIGHT_BLUE, PURPLE, LAVENDER, TEAL, LIGHT_TEAL, ORANGE, PEACH]

TIERS = ["Critical", "High", "Medium", "Low"]
# Same colors as the tier badges in the lead table, so red always means Critical, etc.
TIER_COLORS = {"Critical": "#BA0517", "High": "#DD7A01", "Medium": "#0176D3", "Low": "#A0A0A0"}

FONT = "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif"
AXIS_TEXT = "#6B6A6B"


def _base(height: int = 270) -> go.Figure:
    """An empty figure with the shared Lightning look: white, light gridlines, small grey text."""
    fig = go.Figure()
    fig.update_layout(
        height=height, margin={"l": 8, "r": 8, "t": 28, "b": 8},
        paper_bgcolor="white", plot_bgcolor="white",
        font={"family": FONT, "size": 12, "color": "#3E3E3C"},
        showlegend=False, hoverlabel={"font": {"family": FONT}},
    )
    fig.update_xaxes(gridcolor="#E5E5E5", zeroline=False, tickfont={"color": AXIS_TEXT, "size": 11},
                     linecolor="#E5E5E5")
    fig.update_yaxes(gridcolor="#E5E5E5", zeroline=False, tickfont={"color": AXIS_TEXT, "size": 11},
                     linecolor="#E5E5E5")
    return fig


def _caption(fig: go.Figure, text: str) -> None:
    """Small grey caption above the plot, like 'Sum of Amount' in Salesforce charts."""
    fig.add_annotation(text=text, x=0.5, y=1.0, xref="paper", yref="paper", yshift=18,
                       showarrow=False, font={"size": 11, "color": "#3E3E3C"})


def _legend_below(fig: go.Figure, y: float = -0.04) -> None:
    """Horizontal legend centered under the plot, so the chart itself sits in the middle of the card.
    Bar/column charts pass a lower `y` so the legend clears the axis labels."""
    fig.update_layout(showlegend=True, legend={
        "orientation": "h", "x": 0.5, "xanchor": "center", "y": y, "yanchor": "top",
        "font": {"size": 11}, "itemclick": False, "itemdoubleclick": False,
        "traceorder": "normal"})


def empty(height: int = 270) -> go.Figure:
    fig = _base(height)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


# ======================================================================================
# Row 1
# ======================================================================================
def tier_bar(tier_counts: dict[str, int]) -> go.Figure:
    """Horizontal bar: how many leads are in each priority tier. Most urgent on top."""
    fig = _base()
    tiers = TIERS[::-1]  # Critical on top
    counts = [tier_counts.get(t, 0) for t in tiers]
    fig.add_bar(y=tiers, x=counts, orientation="h", width=0.7,
                marker_color=[TIER_COLORS[t] for t in tiers],
                text=[f"{c:,} leads" for c in counts], textposition="outside", cliponaxis=False,
                textfont={"size": 12, "color": "#3E3E3C"},
                hovertemplate="%{y}: %{x:,} leads<extra></extra>")
    fig.update_xaxes(visible=False, range=[0, max(max(counts), 1) * 1.3])
    fig.update_yaxes(showgrid=False, tickfont={"size": 13, "color": "#181818"})
    fig.update_layout(margin={"l": 8, "r": 12, "t": 12, "b": 8})
    return fig


def routing_funnel(stages: list[tuple[str, int]]) -> go.Figure:
    """Three plain steps: leads uploaded -> leads kept after cleaning -> leads given to a rep."""
    fig = _base()
    names = [name for name, _ in stages]
    values = [v for _, v in stages]
    first = max(values[0], 1)
    fig.add_trace(go.Funnel(
        y=names, x=values, textinfo="none",
        text=[f"{v:,}" + ("" if i == 0 else f"  ({100 * v / first:.1f}%)") for i, v in enumerate(values)],
        texttemplate="%{text}", textposition="inside", textfont={"color": "white", "size": 13},
        marker={"color": [PURPLE, TEAL, GREEN][:len(stages)]}, connector={"fillcolor": "#ECEBEA"},
        hovertemplate="%{y}: %{x:,}<extra></extra>",
    ))
    fig.update_yaxes(tickfont={"size": 12, "color": "#181818"}, showgrid=False)
    fig.update_layout(margin={"l": 8, "r": 12, "t": 12, "b": 8})
    return fig


# ======================================================================================
# Row 2
# ======================================================================================
def donut(labels: list[str], values: list[int], center: str, legend_title: str = "",
          colors: list[str] | None = None, caption: str | None = None) -> go.Figure:
    """Donut with the total in the middle and a legend underneath. Slices show their share."""
    fig = _base()
    fig.add_pie(labels=labels, values=values, hole=0.62, sort=False, direction="clockwise",
                marker={"colors": colors or PALETTE, "line": {"color": "white", "width": 2}},
                textinfo="percent", textposition="outside", textfont={"size": 12, "color": "#3E3E3C"},
                domain={"x": [0.0, 1.0], "y": [0.14, 1.0]},
                hovertemplate="%{label}: %{value:,} (%{percent})<extra></extra>")
    fig.add_annotation(text=center, x=0.5, y=0.57, xref="paper", yref="paper",
                       showarrow=False, font={"size": 20, "color": "#181818"})
    _legend_below(fig)
    if caption:
        _caption(fig, caption)
    return fig


def assigned_vs_review(assigned: int, review: int) -> go.Figure:
    """Two slices: leads already given to a rep vs. leads waiting for a manager."""
    total = max(assigned + review, 1)
    fig = donut(["Assigned to a rep", "Waiting in Manager Review"], [assigned, review],
                f"<b>{100 * assigned / total:.1f}%</b><br><span style='font-size:12px'>assigned</span>",
                colors=[GREEN, ORANGE])
    fig.update_traces(texttemplate="%{value:,}")
    return fig


def destination_columns(results: pd.DataFrame) -> go.Figure:
    """Stacked columns: who got the leads (Junior / Senior / Manager / nobody yet), split by priority.
    Shows at a glance that Juniors never get Critical leads."""
    fig = _base()
    dest = results["rep_seniority"].fillna("Manager Review")
    order = ["Junior", "Senior", "Manager", "Manager Review"]
    labels = ["Junior reps", "Senior reps", "Managers", "Manager Review"]
    table = pd.crosstab(dest, results["priority"]).reindex(index=order, columns=TIERS, fill_value=0)
    for tier in TIERS:
        fig.add_bar(x=labels, y=table[tier], name=tier, marker_color=TIER_COLORS[tier],
                    hovertemplate=f"{tier}: " + "%{y:,} leads<extra>%{x}</extra>")
    totals = table.sum(axis=1)
    fig.add_scatter(x=labels, y=totals, mode="text", text=[f"{t:,}" for t in totals],
                    textposition="top center", textfont={"size": 12, "color": "#3E3E3C"},
                    showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack", bargap=0.35)
    fig.update_yaxes(title={"text": "Leads", "font": {"size": 11, "color": AXIS_TEXT}}, tickformat="~s",
                     range=[0, max(totals.max(), 1) * 1.18])
    fig.update_xaxes(showgrid=False, tickfont={"size": 12, "color": "#181818"})
    _legend_below(fig, y=-0.14)
    return fig


# ======================================================================================
# Row 4: capacity
# ======================================================================================
def capacity_by_seniority(reps: pd.DataFrame) -> go.Figure:
    """For Juniors, Seniors and Managers: how much of their combined capacity is already used."""
    order = ["Manager", "Senior", "Junior"]  # bottom to top, so Junior is on top
    team = reps.groupby("seniority")[["load", "max_capacity"]].sum().reindex(order, fill_value=0)
    used_pct = (100 * team["load"] / team["max_capacity"].where(team["max_capacity"] > 0)).fillna(0)
    labels = [f"{s}s ({(reps['seniority'] == s).sum():,})" for s in order]
    fig = _base(300)
    fig.add_bar(y=labels, x=used_pct, orientation="h", name="Used", marker_color=BLUE, width=0.6,
                hovertemplate="%{y}: %{x:.0f}% used<extra></extra>")
    fig.add_bar(y=labels, x=100 - used_pct, orientation="h", name="Room left", marker_color=LIGHT_GREY, width=0.6,
                hovertemplate="%{y}: %{x:.0f}% room left<extra></extra>")
    fig.add_scatter(y=labels, x=[100] * len(order), mode="text", showlegend=False, hoverinfo="skip",
                    text=[f"  <b>{p:.0f}% used</b>  ({u:,} of {c:,} leads)"
                          for p, u, c in zip(used_pct, team["load"], team["max_capacity"])],
                    textposition="middle right", textfont={"size": 12, "color": "#3E3E3C"})
    fig.update_layout(barmode="stack", margin={"l": 8, "r": 12, "t": 36, "b": 8})
    fig.update_xaxes(visible=False, range=[0, 165])
    fig.update_yaxes(showgrid=False, tickfont={"size": 13, "color": "#181818"})
    fig.update_layout(showlegend=True, legend={"orientation": "h", "x": 0.5, "y": 1.02, "xanchor": "center",
                                               "yanchor": "bottom", "font": {"size": 11}})
    return fig


FULLNESS_GROUPS = [  # (label, lowest %, highest %, color) - checked top to bottom
    ("Full (100%)", 100, 100, PURPLE),
    ("Almost full (75-99%)", 75, 99.999, BLUE),
    ("Half to 3/4 full (50-74%)", 50, 74.999, LIGHT_BLUE),
    ("Under half full (1-49%)", 0.001, 49.999, LIGHT_TEAL),
    ("No leads yet (0%)", 0, 0, LIGHT_GREY),
]


def rep_fullness(reps: pd.DataFrame) -> go.Figure:
    """How many reps are full, almost full, half full... in plain groups instead of a histogram."""
    utilization = reps["utilization"].clip(0, 100)
    counts = [int(((utilization >= lo) & (utilization <= hi)).sum()) for _, lo, hi, _ in FULLNESS_GROUPS]
    total = max(len(reps), 1)
    labels = [name for name, *_ in FULLNESS_GROUPS][::-1]  # Full on top
    counts_top_down = counts[::-1]
    fig = _base(300)
    fig.add_bar(y=labels, x=counts_top_down, orientation="h", width=0.65,
                marker_color=[c for *_, c in FULLNESS_GROUPS][::-1],
                text=[f"{n:,} reps ({100 * n / total:.0f}%)" for n in counts_top_down],
                textposition="outside", cliponaxis=False, textfont={"size": 12, "color": "#3E3E3C"},
                hovertemplate="%{y}: %{x:,} reps<extra></extra>")
    fig.update_xaxes(visible=False, range=[0, max(max(counts), 1) * 1.45])
    fig.update_yaxes(showgrid=False, tickfont={"size": 12, "color": "#181818"})
    fig.update_layout(margin={"l": 8, "r": 12, "t": 12, "b": 8})
    return fig


# ======================================================================================
# Test results panel ("Run Tests" button): one small chart per scenario
# ======================================================================================
TEST_COLORS = {"green": "#2E844A", "orange": "#F2A33A", "red": "#BA0517", "blue": "#0176D3", "grey": "#A0A0A0"}


def test_bars(labels: list[str], values: list[float], colors: list[str], unit: str, maximum: float | None = None) -> go.Figure:
    """Small horizontal bars with the number written at the end of each bar."""
    fig = _base(60 + 46 * len(labels))
    top = maximum or max(max(values), 1)
    fig.add_bar(y=labels[::-1], x=values[::-1], orientation="h", width=0.62,
                marker_color=[TEST_COLORS.get(c, c) for c in colors][::-1],
                text=[f"{v:,}" + (f" {unit}" if unit else "") for v in values][::-1],
                textposition="outside", cliponaxis=False, textfont={"size": 13, "color": "#181818"},
                hovertemplate="%{y}: %{x:,}<extra></extra>")
    fig.update_xaxes(visible=False, range=[0, top * 1.55])
    fig.update_yaxes(showgrid=False, tickfont={"size": 13, "color": "#181818"})
    fig.update_layout(margin={"l": 8, "r": 8, "t": 6, "b": 6})
    return fig


def test_grouped(labels: list[str], series: list[tuple[str, list[float], str]], unit: str) -> go.Figure:
    """Side-by-side columns comparing two ways of doing the same thing."""
    fig = _base(210)
    top = max(max(values) for _, values, _ in series) or 1
    for name, values, color in series:
        fig.add_bar(x=labels, y=values, name=name, marker_color=TEST_COLORS.get(color, color),
                    text=[f"{v:,}" for v in values], textposition="outside", cliponaxis=False,
                    textfont={"size": 13, "color": "#181818"},
                    hovertemplate=f"{name}: " + "%{y:,} " + unit + "<extra>%{x}</extra>")
    fig.update_layout(barmode="group", bargap=0.35, margin={"l": 8, "r": 8, "t": 6, "b": 6})
    fig.update_yaxes(visible=False, range=[0, top * 1.3])
    fig.update_xaxes(showgrid=False, tickfont={"size": 13, "color": "#181818"})
    _legend_below(fig, y=-0.12)
    return fig
