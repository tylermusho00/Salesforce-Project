"""Downloads provide real CSV attachments with usable filenames and clean export columns."""
import io

import pandas as pd
import pytest

from app.main import server
from lead_router.pipeline import STATE, run_dataset


@pytest.mark.parametrize("kind,prefix,rows", [
    ("results", "lead_routing", 100), ("salesforce", "salesforce_mapping_template", 100),
    ("review", "manager_review", 1), ("reps", "rep_capacity", 10),
])
def test_csv_attachment_headers_names_and_contents(kind, prefix, rows):
    STATE.set(run_dataset("sample"))
    response = server.test_client().get(f"/exports/{kind}.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert f"attachment; filename={prefix}_sample.csv" == response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"
    frame = pd.read_csv(io.BytesIO(response.data))
    assert len(frame) == rows
    assert not any(col.startswith("_") or col == "ml_flag" for col in frame.columns)
    if kind == "salesforce":
        assert {"Source_Lead_ID__c", "Source_Rep_ID__c", "OwnerId"} <= set(frame.columns)


def test_export_without_run_or_unknown_format_is_rejected():
    client = server.test_client()
    assert client.get("/exports/results.csv").status_code == 409
    assert client.get("/exports/unknown.csv").status_code == 404
