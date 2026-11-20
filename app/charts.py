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
GREY, LIGHT_GREY = "#706E6B", "#ECEBEA"
PALETTE = [BLUE, LIGHT_BLUE, PURPLE, LAVENDER, TEAL, LIGHT_TEAL, ORANGE, PEACH]

TIERS = ["Critical", "High", "Medium", "Low"]
TIER_COLORS = {"Critical": PURPLE, "High": BLUE, "Medium": LIGHT_BLUE, "Low": "#C3D7F2"}

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
    """Horizontal bar: how many leads landed in each priority tier (like 'Sales by Quarter')."""
    fig = _base()
    tiers = TIERS[::-1]  # Critical on top
    counts = [tier_counts.get(t, 0) for t in tiers]
    fig.add_bar(y=tiers, x=counts, orientation="h", marker_color=BLUE, width=0.75,
                text=[f"{c:,}" for c in counts], textposition="inside", insidetextanchor="end",
                textfont={"color": "white", "size": 12},
                hovertemplate="%{y}: %{x:,} leads<extra></extra>")
    fig.update_xaxes(side="top", tickformat="~s",
                     title={"text": "Record Count", "font": {"size": 11, "color": "#3E3E3C"}, "standoff": 4})
    fig.update_yaxes(title={"text": "Priority Tier", "font": {"size": 11, "color": AXIS_TEXT}}, showgrid=False)
    fig.update_layout(margin={"l": 8, "r": 12, "t": 52, "b": 8})
    return fig


def routing_funnel(stages: list[tuple[str, int]]) -> go.Figure:
    """Funnel from rows uploaded down to leads assigned (like 'Potential Sales by Stage')."""
    fig = _base()
    labels = [name for name, _ in stages]
    values = [max(v, 1) for _, v in stages]  # funnelarea can't draw a 0-area band
    fig.add_trace(go.Funnelarea(
        labels=labels, values=values, text=[f"{v:,}" for _, v in stages], textinfo="text",
        marker={"colors": [PURPLE, LAVENDER, TEAL, LIGHT_TEAL, ORANGE, PEACH][:len(stages)],
                "line": {"color": "white", "width": 1}},
        textfont={"color": "white", "size": 12}, aspectratio=1.1, baseratio=0.45,
        domain={"x": [0.15, 0.85], "y": [0.1, 1.0]},
        hovertemplate="%{label}: %{text}<extra></extra>",
    ))
    _legend_below(fig)
    _caption(fig, f"Leads uploaded: {stages[0][1]:,}")
    return fig


# ======================================================================================
# Row 2
# ======================================================================================
def donut(labels: list[str], values: list[int], center: str, legend_title: str,
          colors: list[str] | None = None, caption: str = "Record Count") -> go.Figure:
    """Donut with the total in the middle and a legend underneath (like 'Sales by Region')."""
    fig = _base()
    fig.add_pie(labels=labels, values=values, hole=0.62, sort=False, direction="clockwise",
                marker={"colors": colors or PALETTE, "line": {"color": "white", "width": 2}},
                textinfo="value", texttemplate="%{value:,}", textposition="outside",
                textfont={"size": 11, "color": "#3E3E3C"}, domain={"x": [0.0, 1.0], "y": [0.14, 1.0]},
                hovertemplate="%{label}: %{value:,} (%{percent})<extra></extra>")
    fig.add_annotation(text=f"<b>{center}</b>", x=0.5, y=0.57, xref="paper", yref="paper",
                       showarrow=False, font={"size": 24, "color": "#181818"})
    _legend_below(fig)
    _caption(fig, caption)
    return fig


def destination_columns(results: pd.DataFrame) -> go.Figure:
    """Stacked columns: where leads went (Junior/Senior/Manager/Manager Review), split by tier.
    Makes the 'no Critical leads on Juniors' rule visible at a glance."""
    fig = _base()
    dest = results["rep_seniority"].fillna("Manager Review")
    order = ["Junior", "Senior", "Manager", "Manager Review"]
    table = pd.crosstab(dest, results["priority"]).reindex(index=order, columns=TIERS, fill_value=0)
    for tier in TIERS:
        fig.add_bar(x=order, y=table[tier], name=tier, marker_color=TIER_COLORS[tier],
                    hovertemplate=f"{tier}: " + "%{y:,}<extra>%{x}</extra>")
    totals = table.sum(axis=1)
    fig.add_scatter(x=order, y=totals, mode="text", text=[f"{t:,}" for t in totals],
                    textposition="top center", textfont={"size": 12, "color": "#3E3E3C"},
                    showlegend=False, hoverinfo="skip")
    fig.update_layout(barmode="stack", bargap=0.35)
    fig.update_yaxes(title={"text": "Leads", "font": {"size": 11, "color": AXIS_TEXT}}, tickformat="~s",
                     range=[0, max(totals.max(), 1) * 1.18])
    fig.update_xaxes(showgrid=False)
    _legend_below(fig, y=-0.14)
    _caption(fig, "Record Count")
    return fig


# ======================================================================================
# Row 4: capacity
# ======================================================================================
def capacity_by_seniority(reps: pd.DataFrame) -> go.Figure:
    """Horizontal stacked bars: lead slots used vs. still open for Juniors, Seniors and Managers.
    Shows the design choice: Managers are kept as a reserve for Critical overflow."""
    order = ["Manager", "Senior", "Junior"]  # bottom to top, so Junior is on top
    team = reps.groupby("seniority")[["load", "max_capacity"]].sum().reindex(order, fill_value=0)
    used_pct = (100 * team["load"] / team["max_capacity"].where(team["max_capacity"] > 0)).fillna(0)
    labels = [f"{s} ({(reps['seniority'] == s).sum():,} reps)" for s in order]
    fig = _base(300)
    fig.add_bar(y=labels, x=team["load"], orientation="h", name="Slots used", marker_color=BLUE,
                hovertemplate="%{y}: %{x:,} slots used<extra></extra>")
    fig.add_bar(y=labels, x=team["max_capacity"] - team["load"], orientation="h", name="Slots open",
                marker_color=LIGHT_GREY, hovertemplate="%{y}: %{x:,} slots open<extra></extra>")
    # Label after each bar: "95% · 34,064 / 35,984"
    fig.add_scatter(y=labels, x=team["max_capacity"], mode="text", showlegend=False, hoverinfo="skip",
                    text=[f"  <b>{p:.0f}%</b> · {u:,} / {c:,}"
                          for p, u, c in zip(used_pct, team["load"], team["max_capacity"])],
                    textposition="middle right", textfont={"size": 12, "color": "#3E3E3C"})
    fig.update_layout(barmode="stack", bargap=0.35, margin={"l": 8, "r": 12, "t": 36, "b": 8})
    fig.update_xaxes(range=[0, max(team["max_capacity"].max(), 1) * 1.45], tickformat="~s")
    fig.update_yaxes(showgrid=False, tickfont={"size": 12})
    fig.update_layout(showlegend=True, legend={"orientation": "h", "x": 0.5, "y": 1.02, "xanchor": "center",
                                               "yanchor": "bottom", "font": {"size": 11}})
    return fig


def utilization_histogram(reps: pd.DataFrame) -> go.Figure:
    """How full the team is: number of reps in each 10% utilization band."""
    fig = _base(300)
    bins = list(range(0, 101, 10))
    labels = [f"{b}-{b + 10}%" for b in bins[:-1]]
    bands = pd.cut(reps["utilization"].clip(0, 100), bins=bins, labels=labels, include_lowest=True)
    counts = bands.value_counts().reindex(labels, fill_value=0)
    colors = [BLUE if i < 8 else (ORANGE if i == 8 else PURPLE) for i in range(len(labels))]
    fig.add_bar(x=labels, y=counts.values, marker_color=colors, text=counts.values, textposition="outside",
                textfont={"size": 11}, hovertemplate="%{x}: %{y} reps<extra></extra>")
    fig.update_yaxes(title={"text": "Reps", "font": {"size": 11, "color": AXIS_TEXT}},
                     range=[0, max(counts.max(), 1) * 1.2])
    fig.update_xaxes(showgrid=False, tickangle=-35)
    _caption(fig, "Reps by % of max capacity used")
    return fig
