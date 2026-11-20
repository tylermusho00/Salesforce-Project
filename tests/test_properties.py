"""Property tests: random messy rows never crash the pipeline and the core guarantees always hold."""
import pandas as pd
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from lead_router.cleaning import LEAD_COLUMNS, REP_COLUMNS
from lead_router.models import AssignmentStatus, Priority, Seniority
from lead_router.pipeline import run_pipeline_frames

messy = st.one_of(
    st.sampled_from(["", " ", "N/A", "null", "None", "Unknown", "NA", "EUR", "apac", "EN", "es", "de",
                     "Technology ", " Finance", "sr", "MGR", "jr", "5k", "1,000", "500-1000", "-20", "0",
                     "True", "yes", "Y", "Requested Live Demo", "ASIA   PACIFICEurope", "L-1", "R-1"]),
    st.text(max_size=12),
    st.integers(-100, 100_000).map(str),
)

lead_rows = st.lists(st.fixed_dictionaries({c: messy for c in LEAD_COLUMNS}), max_size=40)
rep_rows = st.lists(st.fixed_dictionaries({c: messy for c in REP_COLUMNS}), max_size=15)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lead_rows, rep_rows)
def test_random_messy_rows_never_crash(leads, reps):
    r = run_pipeline_frames(pd.DataFrame(leads, columns=LEAD_COLUMNS), pd.DataFrame(reps, columns=REP_COLUMNS))
    m = r.metrics
    assert m["assigned"] + m["overflow"] == m["total_leads"] == len(r.leads)
    assert all(rep.current_capacity <= rep.max_capacity for rep in r.reps)
    rep_by_id = {rep.id: rep for rep in r.reps}
    for a in r.assignments:
        if a.status is AssignmentStatus.ASSIGNED:
            assert a.rep_id in rep_by_id
            if a.priority is Priority.CRITICAL:
                assert rep_by_id[a.rep_id].seniority is not Seniority.JUNIOR
        else:
            assert a.rep_id is None and a.overflow_reason is not None
