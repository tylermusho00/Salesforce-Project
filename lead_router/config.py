"""
config.py - finds and loads config/rules.yaml.

All business numbers (point values, tier cutoffs, aliases, capacities) live in that YAML file,
so the logic files never contain "magic numbers". Edit the YAML, rerun, done.

Two environment variables let you point somewhere else without touching code:
    LEAD_ROUTER_RULES     path to a different rules.yaml
    LEAD_ROUTER_DATA_DIR  folder containing the CSV datasets
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# backend/lead_router/config.py -> parents[1] is the backend/ folder.
BACKEND_DIR = Path(__file__).resolve().parents[1]
RULES_PATH = Path(os.environ.get("LEAD_ROUTER_RULES", BACKEND_DIR / "config" / "rules.yaml"))
DATA_DIR = Path(os.environ.get("LEAD_ROUTER_DATA_DIR", BACKEND_DIR / "data"))


@lru_cache(maxsize=1)  # read the file once, then reuse it
def load_rules() -> dict[str, Any]:
    with open(RULES_PATH, encoding="utf-8") as file:
        return yaml.safe_load(file)


def dataset_paths(name: str) -> tuple[Path, Path]:
    """Return (leads_csv, reps_csv) for a named dataset such as "sample" or "scale"."""
    datasets = load_rules()["datasets"]
    if name not in datasets:
        raise KeyError(f"Unknown dataset '{name}'. Choose one of: {', '.join(datasets)}")
    return DATA_DIR / datasets[name]["leads"], DATA_DIR / datasets[name]["reps"]
