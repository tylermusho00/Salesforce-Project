"""Freeze or compare routing decisions, so changes can prove they did not alter any outcome.

    python scripts/baseline.py save  <dir>      write <dir>/<dataset>.csv for sample and scale
    python scripts/baseline.py check <dir>      re-run and compare; exit 1 on any difference

Compared per lead: status, rep_id, match_level, overflow_reason, priority, score, reason.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lead_router.pipeline import run_dataset  # noqa: E402

FIELDS = ["lead_id", "status", "rep_id", "match_level", "overflow_reason", "priority", "score", "reason"]


def decisions(name: str) -> list[dict[str, str]]:
    result = run_dataset(name)
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
        })
    return sorted(rows, key=lambda r: r["lead_id"])


def main() -> int:
    mode, folder = sys.argv[1], Path(sys.argv[2])
    failed = False
    for name in ("sample", "scale"):
        rows = decisions(name)
        path = folder / f"{name}.csv"
        if mode == "save":
            folder.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            print(f"saved {len(rows)} decisions -> {path}")
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
