"""Freeze or compare routing decisions, so changes can prove they did not alter any outcome.

    python scripts/baseline.py save  <dir>      write <dir>/<dataset>.csv for sample and scale
    python scripts/baseline.py check <dir>      re-run and compare; exit 1 on any difference

Compared per lead: status, rep_id, match_level, overflow_reason, priority, score, reason,
rep name/seniority, suggested action/rep. Also compared per dataset (<dataset>.json): the cleaned
leads and reps, both data-quality reports (counts, examples, value audit) and all metrics except
the timing-based throughput.
"""
from __future__ import annotations

import csv
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lead_router.pipeline import run_dataset  # noqa: E402

FIELDS = ["lead_id", "status", "rep_id", "match_level", "overflow_reason", "priority", "score", "reason",
          "rep_name", "rep_seniority", "suggested_action", "suggested_rep_id"]


def decisions(result) -> list[dict[str, str]]:
    rows = []
    for a in result.assignments:
        rows.append({
            "lead_id": a.lead_id,
            "status": a.status.value,
            "rep_id": a.rep_id or "",
            "match_level": a.match_level.value if a.match_level else "",
            "overflow_reason": a.overflow_reason.value if a.overflow_reason else "",
            "priority": a.priority.value,
            "score": str(a.score),
            "reason": a.reason,
            "rep_name": a.rep_name or "",
            "rep_seniority": a.rep_seniority.value if a.rep_seniority else "",
            "suggested_action": a.suggested_action or "",
            "suggested_rep_id": a.suggested_rep_id or "",
        })
    return sorted(rows, key=lambda r: r["lead_id"])


def snapshot(result) -> dict:
    """Everything except the assignments: cleaned data, data-quality reports, metrics."""
    metrics = {k: v for k, v in result.metrics.items() if k != "throughput_lps"}
    return json.loads(json.dumps({
        "leads": [dataclasses.asdict(lead) for lead in result.leads],
        "reps": [dataclasses.asdict(rep) for rep in result.reps],
        "lead_quality": dataclasses.asdict(result.lead_quality),
        "rep_quality": dataclasses.asdict(result.rep_quality),
        "metrics": metrics,
    }, default=str, sort_keys=True))


def main() -> int:
    mode, folder = sys.argv[1], Path(sys.argv[2])
    failed = False
    for name in ("sample", "scale"):
        result = run_dataset(name)
        rows = decisions(result)
        snap = snapshot(result)
        path = folder / f"{name}.csv"
        snap_path = folder / f"{name}.json"
        if mode == "save":
            folder.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            snap_path.write_text(json.dumps(snap, sort_keys=True), encoding="utf-8")
            print(f"saved {len(rows)} decisions -> {path} (+ {snap_path.name})")
            continue
        with path.open(newline="", encoding="utf-8") as f:
            expected = list(csv.DictReader(f))
        diffs = [(e, r) for e, r in zip(expected, rows) if e != r]
        if len(expected) != len(rows) or diffs:
            failed = True
            print(f"{name}: {len(diffs)} differing leads (rows {len(expected)} -> {len(rows)})")
            for e, r in diffs[:5]:
                print("  expected:", e, "\n  got:     ", r)
        else:
            print(f"{name}: identical ({len(rows)} leads)")
        expected_snap = json.loads(snap_path.read_text(encoding="utf-8"))
        bad = [k for k in expected_snap if expected_snap[k] != snap.get(k)]
        if bad:
            failed = True
            print(f"{name}: cleaned data / reports / metrics differ in: {', '.join(bad)}")
        else:
            print(f"{name}: cleaned data, data-quality reports and metrics identical")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
