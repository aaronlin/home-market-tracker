from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from models import HomeRun, TrackerRun


@dataclass(frozen=True)
class HomeState:
    seen_comp_ids: List[str] = field(default_factory=list)
    median_sale_price: Optional[float] = None
    median_price_per_sqft: Optional[float] = None


@dataclass(frozen=True)
class TrackerState:
    last_successful_run: Optional[str] = None
    homes: Dict[str, HomeState] = field(default_factory=dict)


def load_state(path: Path) -> TrackerState:
    if not path.exists():
        return TrackerState()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    homes = {}
    for home_id, raw_home in (loaded.get("homes") or {}).items():
        raw_home = raw_home or {}
        homes[str(home_id)] = HomeState(
            seen_comp_ids=list(raw_home.get("seen_comp_ids") or []),
            median_sale_price=_float_or_none(raw_home.get("median_sale_price")),
            median_price_per_sqft=_float_or_none(raw_home.get("median_price_per_sqft")),
        )
    return TrackerState(last_successful_run=loaded.get("last_successful_run"), homes=homes)


def save_state(path: Path, run: TrackerRun, previous: TrackerState | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = previous or TrackerState()
    homes = {}
    for home_run in run.homes:
        prior_ids = set((previous.homes.get(home_run.home.id) or HomeState()).seen_comp_ids)
        current_ids = {comp.id for comp in home_run.selected_comps}
        homes[home_run.home.id] = {
            "seen_comp_ids": sorted(prior_ids | current_ids),
            "median_sale_price": home_run.median_sale_price,
            "median_price_per_sqft": home_run.median_price_per_sqft,
        }
    payload = {
        "last_successful_run": datetime.now(timezone.utc).isoformat(),
        "homes": homes,
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")


def _float_or_none(value) -> Optional[float]:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
