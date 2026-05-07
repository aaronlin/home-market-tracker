from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from models import ComparableSale, HomeConfig


def normalize_comps(home: HomeConfig, payload: Dict[str, Any]) -> List[ComparableSale]:
    avm_payload = payload.get("avm") if isinstance(payload.get("avm"), dict) else payload
    subject_address = _normalized_address(((avm_payload.get("subjectProperty") or {}).get("formattedAddress") or home.address))
    merged: Dict[str, Dict[str, Any]] = {}
    for raw in _first_list(avm_payload, ("comparables", "comps", "saleComps", "salesComparables", "properties")):
        if isinstance(raw, dict):
            _merge_record(merged, raw, source="avm")
    for raw in _records(payload.get("sale_listings")):
        _merge_record(merged, raw, source="listing")
    for raw in _records(payload.get("properties")):
        _merge_record(merged, raw, source="property")

    normalized = []
    cutoff = _cutoff_date(home.days_old)
    for raw in merged.values():
        if _normalized_address(_address(raw)) == subject_address:
            continue
        comp = _normalize_comp(home, raw)
        if not _within_lookback(comp, cutoff):
            continue
        normalized.append(comp)
    normalized = [comp for comp in normalized if _passes_local_filters(home, comp)]
    normalized = [_with_rank_score(home, comp) for comp in normalized]
    normalized.sort(key=lambda comp: (comp.rank_score or 0, comp.sale_date or "", comp.sale_price or 0), reverse=True)
    return normalized


def extract_value_estimate(payload: Dict[str, Any]) -> Optional[int]:
    avm_payload = payload.get("avm") if isinstance(payload.get("avm"), dict) else payload
    return _int(_first_value(avm_payload, ("price", "value", "valueEstimate", "valuation", "estimate")))


def new_comps(comps: Iterable[ComparableSale], seen_ids: Iterable[str]) -> List[ComparableSale]:
    seen = set(seen_ids)
    return [comp for comp in comps if comp.id not in seen]


def percent_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in {None, 0}:
        return None
    return ((current - previous) / previous) * 100.0


def _passes_local_filters(home: HomeConfig, comp: ComparableSale) -> bool:
    if not _value_in_range(comp.sqft, home.comp_square_footage):
        return False
    if not _value_in_range(comp.lot_size, home.comp_lot_size):
        return False
    return True


def _value_in_range(value: Optional[int], raw_range: Optional[str]) -> bool:
    if not raw_range:
        return True
    if value is None:
        return False
    raw_range = str(raw_range)
    if ":" not in raw_range:
        return value == _int(raw_range)
    low_raw, high_raw = raw_range.split(":", 1)
    low = _int(low_raw) if low_raw else None
    high = _int(high_raw) if high_raw else None
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def _normalize_comp(home: HomeConfig, raw: Dict[str, Any]) -> ComparableSale:
    address = _address(raw)
    sqft = _int(_first_value(raw, ("squareFootage", "sqft", "livingArea", "livingAreaSqFt")))
    listing_price = _int(_first_value(raw, ("listPrice", "listingPrice", "originalListPrice", "price")))
    sold_price = _sold_price(raw)
    market_price = sold_price or _int(_first_value(raw, ("salePrice", "lastSalePrice", "soldPrice", "price"))) or listing_price
    ppsf = _float(_first_value(raw, ("pricePerSquareFoot", "pricePerSqft", "salePricePerSqft")))
    if ppsf is None and market_price is not None and sqft:
        ppsf = market_price / sqft
    notes, exclude = _annotation(home, address)
    hoa_fee = _hoa_fee(raw)
    hoa_match = _same_hoa(home, address)
    return ComparableSale(
        id=_comp_id(home, raw, address),
        address=address,
        distance_miles=_float(_first_value(raw, ("distance", "distanceMiles", "distanceInMiles"))),
        beds=_float(_first_value(raw, ("bedrooms", "beds"))),
        baths=_float(_first_value(raw, ("bathrooms", "baths"))),
        sqft=sqft,
        lot_size=_int(_first_value(raw, ("lotSize", "lotSizeSqFt", "lotSquareFootage"))),
        year_built=_int(_first_value(raw, ("yearBuilt",))),
        listing_price=listing_price,
        sold_price=sold_price,
        market_price=market_price,
        listed_date=_str(_first_value(raw, ("listedDate", "dateListed"))),
        removed_date=_str(_first_value(raw, ("removedDate", "dateRemoved", "saleDate", "soldDate"))),
        last_seen_date=_str(_first_value(raw, ("lastSeenDate",))),
        status=_str(_first_value(raw, ("status",))),
        days_on_market=_int(_first_value(raw, ("daysOnMarket", "dom"))),
        price_per_sqft=ppsf,
        hoa_fee=hoa_fee,
        hoa_match=hoa_match,
        notes=notes,
        exclude_from_median=exclude,
        raw=raw,
    )


def _with_rank_score(home: HomeConfig, comp: ComparableSale) -> ComparableSale:
    score = _rank_score(home, comp)
    return ComparableSale(
        id=comp.id,
        address=comp.address,
        distance_miles=comp.distance_miles,
        beds=comp.beds,
        baths=comp.baths,
        sqft=comp.sqft,
        lot_size=comp.lot_size,
        year_built=comp.year_built,
        listing_price=comp.listing_price,
        sold_price=comp.sold_price,
        market_price=comp.market_price,
        listed_date=comp.listed_date,
        removed_date=comp.removed_date,
        last_seen_date=comp.last_seen_date,
        status=comp.status,
        days_on_market=comp.days_on_market,
        price_per_sqft=comp.price_per_sqft,
        hoa_fee=comp.hoa_fee,
        hoa_match=comp.hoa_match,
        notes=comp.notes,
        exclude_from_median=comp.exclude_from_median,
        rank_score=score,
        raw=comp.raw,
    )


def _rank_score(home: HomeConfig, comp: ComparableSale) -> float:
    score = 100.0
    if comp.distance_miles is not None:
        score -= min(comp.distance_miles * 8, 20)
    score -= _closeness_penalty(comp.sqft, _int(home.square_footage), scale=600, max_penalty=20)
    if home.property_type.lower() == "single family":
        score -= _closeness_penalty(comp.lot_size, _int(home.lot_size), scale=2500, max_penalty=25)
        if _value_in_range(comp.lot_size, "6500:10000"):
            score += 8
        score += _recency_bonus(comp, home.days_old, max_bonus=8)
    if home.property_type.lower() == "townhouse":
        if comp.hoa_match:
            score += 30
            score += _recency_bonus(comp, home.days_old, max_bonus=30)
        elif any(pattern.lower() in _normalized_address(comp.address) for pattern in home.same_hoa_patterns):
            score += 20
            score += _recency_bonus(comp, home.days_old, max_bonus=20)
        else:
            score += _recency_bonus(comp, home.days_old, max_bonus=8)
    if comp.exclude_from_median:
        score -= 40
    if (comp.status or "").lower() == "active":
        score += 3
    return round(score, 2)


def _cutoff_date(days_old: int) -> datetime:
    days = days_old if days_old > 0 else 365
    return datetime.now(timezone.utc) - timedelta(days=days)


def _within_lookback(comp: ComparableSale, cutoff: datetime) -> bool:
    sources = set(comp.raw.get("_sources") or [])
    if sources == {"property"} and not comp.listed_date and not comp.removed_date and not comp.last_seen_date and not (comp.status or ""):
        return False
    for value in (comp.removed_date, comp.last_seen_date, comp.listed_date):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed >= cutoff
    return True


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _closeness_penalty(value: Optional[int], target: Optional[int], scale: int, max_penalty: float) -> float:
    if value is None or target is None or scale <= 0:
        return max_penalty / 2
    return min(abs(value - target) / scale * max_penalty, max_penalty)


def _recency_bonus(comp: ComparableSale, days_old: int, max_bonus: float) -> float:
    event_date = _event_date(comp)
    if event_date is None:
        return 0.0
    window = days_old if days_old > 0 else 365
    age_days = max((datetime.now(timezone.utc) - event_date).days, 0)
    freshness = max(0.0, 1.0 - (age_days / window))
    return max_bonus * freshness


def _event_date(comp: ComparableSale) -> Optional[datetime]:
    for value in (comp.removed_date, comp.last_seen_date, comp.listed_date):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _records(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("listings", "properties", "records", "data", "results"):
            maybe = value.get(key)
            if isinstance(maybe, list):
                return [item for item in maybe if isinstance(item, dict)]
    return []


def _merge_record(merged: Dict[str, Dict[str, Any]], raw: Dict[str, Any], source: str) -> None:
    address = _address(raw)
    if address == "Unknown address":
        return
    key = _normalized_address(address)
    existing = merged.setdefault(key, {})
    existing.update({key: value for key, value in raw.items() if value is not None and value != ""})
    sources = set(existing.get("_sources") or [])
    sources.add(source)
    existing["_sources"] = sorted(sources)


def _sold_price(raw: Dict[str, Any]) -> Optional[int]:
    direct = _int(_first_value(raw, ("soldPrice", "salePrice")))
    if direct is not None and "property" in set(raw.get("_sources") or []):
        return direct
    history = raw.get("history")
    if not isinstance(history, dict):
        return None
    sale_events = []
    for date, event in history.items():
        if not isinstance(event, dict):
            continue
        event_name = str(event.get("event") or "").lower()
        if "sale listing" in event_name:
            continue
        if "sale" not in event_name and "sold" not in event_name:
            continue
        price = _int(_first_value(event, ("price", "salePrice", "soldPrice")))
        if price is not None:
            sale_events.append((str(date), price))
    if not sale_events:
        return None
    sale_events.sort(reverse=True)
    return sale_events[0][1]


def _hoa_fee(raw: Dict[str, Any]) -> Optional[int]:
    hoa = raw.get("hoa")
    if isinstance(hoa, dict):
        return _int(hoa.get("fee"))
    return None


def _annotation(home: HomeConfig, address: str) -> tuple[List[str], bool]:
    annotation = home.comp_annotations.get(_normalized_address(address), {})
    notes = []
    tags = annotation.get("tags") or []
    if tags:
        notes.append(", ".join(str(tag) for tag in tags))
    if annotation.get("note"):
        notes.append(str(annotation["note"]))
    return notes, bool(annotation.get("exclude_from_median"))


def _same_hoa(home: HomeConfig, address: str) -> bool:
    normalized = _normalized_address(address)
    if normalized in {_normalized_address(value) for value in home.same_hoa_addresses}:
        return True
    lowered = normalized.lower()
    return any(pattern.lower() in lowered for pattern in home.same_hoa_patterns)


def _first_list(payload: Dict[str, Any], keys: Iterable[str]) -> List[Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _first_value(raw: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in {None, ""}:
            return value
    return None


def _address(raw: Dict[str, Any]) -> str:
    formatted = _first_value(raw, ("formattedAddress", "address", "streetAddress"))
    if isinstance(formatted, str):
        return formatted
    parts = [raw.get("addressLine1"), raw.get("city"), raw.get("state"), raw.get("zipCode")]
    joined = ", ".join(str(part) for part in parts if part)
    return joined or "Unknown address"


def _normalized_address(value: str) -> str:
    return " ".join(value.lower().replace(",", " ").split())


def _comp_id(home: HomeConfig, raw: Dict[str, Any], address: str) -> str:
    provider_id = _first_value(raw, ("id", "propertyId", "listingId", "mlsNumber"))
    sale_date = _str(_first_value(raw, ("saleDate", "lastSaleDate", "soldDate", "removedDate", "lastSeenDate", "listedDate"))) or ""
    sale_price = str(_first_value(raw, ("salePrice", "lastSalePrice", "soldPrice", "price")) or "")
    if provider_id:
        return f"{home.id}:{provider_id}:{sale_date}:{sale_price}"
    digest = hashlib.sha1(f"{home.id}|{address}|{sale_date}|{sale_price}".encode("utf-8")).hexdigest()[:16]
    return f"{home.id}:derived:{digest}"


def _int(value: Any) -> Optional[int]:
    if value in {None, ""}:
        return None
    try:
        return int(round(float(str(value).replace(",", "").replace("$", ""))))
    except ValueError:
        return None


def _float(value: Any) -> Optional[float]:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _str(value: Any) -> Optional[str]:
    if value in {None, ""}:
        return None
    return str(value)[:10]
