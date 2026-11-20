"""
grid_server.py - server-side filtering, sorting and paging for the 50,000-row lead grid.

AG Grid's "infinite" row model asks the server for one page at a time:
    {"startRow": 0, "endRow": 50, "sortModel": [...], "filterModel": {...}}
We apply the filters and sort with pandas and send back only those 50 rows, so the browser
never has to hold all 50,000 leads.
"""
from __future__ import annotations

import pandas as pd

# AG Grid text filter types -> pandas
_TEXT_OPS = {
    "contains": lambda s, v: s.str.contains(v, regex=False),
    "notContains": lambda s, v: ~s.str.contains(v, regex=False),
    "equals": lambda s, v: s == v,
    "notEqual": lambda s, v: s != v,
    "startsWith": lambda s, v: s.str.startswith(v),
    "endsWith": lambda s, v: s.str.endswith(v),
}

# AG Grid number filter types -> pandas (a = filter, b = filterTo)
_NUMBER_OPS = {
    "equals": lambda s, a, b: s == a,
    "notEqual": lambda s, a, b: s != a,
    "greaterThan": lambda s, a, b: s > a,
    "greaterThanOrEqual": lambda s, a, b: s >= a,
    "lessThan": lambda s, a, b: s < a,
    "lessThanOrEqual": lambda s, a, b: s <= a,
    "inRange": lambda s, a, b: s.between(a, b),
}


def _mask(df: pd.DataFrame, column: str, spec: dict) -> pd.Series:
    """True/False per row for one column's filter."""
    if "conditions" in spec:  # e.g. "contains X OR contains Y"
        masks = [_mask(df, column, condition) for condition in spec["conditions"]]
        combined = masks[0]
        for mask in masks[1:]:
            combined = (combined & mask) if spec.get("operator", "AND") == "AND" else (combined | mask)
        return combined

    kind, op = spec.get("filterType", "text"), spec.get("type", "contains")
    values = df[column]
    is_blank = values.isna() | (values.astype(str) == "")
    if op == "blank":
        return is_blank
    if op == "notBlank":
        return ~is_blank
    if kind == "number":
        numbers = pd.to_numeric(values, errors="coerce")
        return _NUMBER_OPS.get(op, _NUMBER_OPS["equals"])(numbers, spec.get("filter"), spec.get("filterTo")).fillna(False)
    text = values.fillna("").astype(str).str.lower()
    return _TEXT_OPS.get(op, _TEXT_OPS["contains"])(text, str(spec.get("filter", "")).lower())


def apply_filter_model(df: pd.DataFrame, model: dict | None) -> pd.DataFrame:
    """Apply every column filter. Columns starting with '_run' are refresh markers, not filters."""
    for column, spec in (model or {}).items():
        if column == "_run" or column not in df.columns:
            continue
        df = df[_mask(df, column, spec)]
    return df


def rows_for_request(df: pd.DataFrame, request: dict) -> dict:
    """Answer one AG Grid getRows request: filter, sort, slice."""
    df = apply_filter_model(df, request.get("filterModel"))
    sort = request.get("sortModel") or []
    if sort:
        df = df.sort_values([s["colId"] for s in sort], ascending=[s["sort"] == "asc" for s in sort],
                            kind="stable")
    start, end = request.get("startRow", 0), request.get("endRow", 50)
    page = df.iloc[start:end].drop(columns=["_search"], errors="ignore").astype(object)
    page = page.where(page.notna(), None)  # NaN -> null for JSON
    return {"rowData": page.to_dict("records"), "rowCount": len(df)}
