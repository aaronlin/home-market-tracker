from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from models import ComparableSale, HomeConfig, ListingObservation


def source_dom(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (TypeError, ValueError, OverflowError, InvalidOperation):
        return None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _type(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def cohort_for(home: HomeConfig, comp: ComparableSale) -> str:
    classification = next((comp.source_property_types[source] for source in
                           ("property", "listing", "avm") if comp.source_property_types.get(source)), "")
    kind, subject = _type(classification), _type(home.property_type)
    if not kind or kind in {"unknown", "other", "none"}:
        return "unknown"
    if kind == subject:
        return "primary"
    if subject == "townhouse" and kind == "condo":
        return "condo_context"
    return "other"


@dataclass(frozen=True)
class AskingGap:
    observation: ListingObservation
    amount: int
    percent: float


def _date(value: Optional[str]) -> Optional[date]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def asking_gap(comp: ComparableSale) -> Optional[AskingGap]:
    sale = comp.recorded_sale
    if sale is None or sale.price <= 0 or comp.raw.get("_listing_identity_conflict"):
        return None
    sale_date = _date(sale.transaction_date)
    if sale_date is None:
        return None
    episodes = {}
    for observation in comp.listing_observations:
        start, observed, removed = map(_date, (observation.listed_date, observation.observed_date, observation.removed_date))
        # Incomplete episode evidence cannot be used to establish an unambiguous pair.
        if start is None or observed is None or removed is None or not observation.mls_number:
            return None
        if not start <= observed <= removed or observation.price <= 0:
            return None
        key = (observation.mls_number, start, removed)
        episodes.setdefault(key, []).append((observed, observation))
    candidates = []
    for key, observations in episodes.items():
        _, start, removed = key
        if removed <= sale_date:
            eligible = [(observed, observation) for observed, observation in observations
                        if 0 <= (sale_date - observed).days <= 90]
            if eligible:
                candidates.append((key, observations, eligible))
    if len(candidates) != 1:
        return None
    key, observations, eligible = candidates[0]
    # A relist, overlapping episode, or post-sale ask makes the match uncertain.
    for other_key, other_observations in episodes.items():
        if other_key != key and (other_key[1] <= sale_date and other_key[2] >= key[1]
                                 or any(observed > sale_date for observed, _ in other_observations)):
            return None
    prices_by_date = {}
    for observed, observation in observations:
        prices_by_date.setdefault(observed, set()).add(observation.price)
    if any(len(prices) != 1 for prices in prices_by_date.values()):
        return None
    observation = max(eligible, key=lambda item: item[0])[1]
    amount = sale.price - observation.price
    return AskingGap(observation, amount, amount / observation.price * 100.0)
