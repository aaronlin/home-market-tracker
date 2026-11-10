from __future__ import annotations

import hashlib
import math
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from models import ComparableSale, HomeConfig, ListingObservation, RecordedSale
from market_metrics import source_dom


def normalize_comps(home: HomeConfig, payload: Dict[str, Any]) -> List[ComparableSale]:
    avm_payload = payload.get("avm") if isinstance(payload.get("avm"), dict) else payload
    if "avm" not in payload and "properties" in payload:
        avm_payload = {key: value for key, value in payload.items() if key != "properties"}
    subject_address = _normalized_address(((avm_payload.get("subjectProperty") or {}).get("formattedAddress") or home.address))
    merged: Dict[str, Dict[str, Any]] = {}
    for raw in _first_list(avm_payload, ("comparables", "comps", "saleComps", "salesComparables", "properties")):
        if isinstance(raw, dict):
            _merge_record(merged, raw, source="avm")
    for raw in _records(payload.get("sale_listings")):
        _merge_record(merged, raw, source="listing")
    for raw in _records(payload.get("properties")):
        _merge_record(merged, raw, source="property")

    subject = avm_payload.get("subjectProperty") or {}
    subject_lat, subject_lon = _float(subject.get("latitude")), _float(subject.get("longitude"))
    normalized = []
    cutoff = _cutoff_date(home.days_old)
    for raw in merged.values():
        if _normalized_address(_address(raw)) == subject_address:
            continue
        comp = _with_computed_distance(_normalize_comp(home, raw), raw, subject_lat, subject_lon)
        if comp.distance_miles is not None and comp.distance_miles > home.max_radius_miles:
            continue
        if not _within_lookback(comp, cutoff):
            continue
        normalized.append(comp)
    normalized = [comp for comp in normalized if _passes_local_filters(home, comp)]
    normalized = [_with_rank_score(home, comp) for comp in normalized]
    normalized.sort(key=lambda comp: (comp.rank_score or 0, comp.sale_date or "", comp.sale_price or 0), reverse=True)
    return normalized


def normalize_nearby_listings(home: HomeConfig, payload: Dict[str, Any]) -> List[ComparableSale]:
    if home.close_listing_radius_miles is None:
        return []
    avm_payload = payload.get("avm") if isinstance(payload.get("avm"), dict) else payload
    subject_address = _normalized_address(((avm_payload.get("subjectProperty") or {}).get("formattedAddress") or home.address))
    subject_lat = _float((avm_payload.get("subjectProperty") or {}).get("latitude")) if isinstance(avm_payload.get("subjectProperty"), dict) else None
    subject_lon = _float((avm_payload.get("subjectProperty") or {}).get("longitude")) if isinstance(avm_payload.get("subjectProperty"), dict) else None
    cutoff = _cutoff_date(home.days_old)
    listings = []
    seen_ids = set()
    for raw in _records(payload.get("sale_listings")):
        if _normalized_address(_address(raw)) == subject_address:
            continue
        listing_records = {}
        _merge_record(listing_records, raw, source="listing")
        comp = _normalize_comp(home, next(iter(listing_records.values())))
        comp = _with_computed_distance(comp, raw, subject_lat, subject_lon)
        if comp.distance_miles is None or comp.distance_miles > home.close_listing_radius_miles:
            continue
        if not _within_lookback(comp, cutoff):
            continue
        if comp.id in seen_ids:
            continue
        seen_ids.add(comp.id)
        listings.append(comp)
    listings.sort(key=lambda comp: (comp.distance_miles if comp.distance_miles is not None else 999, comp.sale_date or "", comp.sale_price or 0))
    return listings


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
    listing = raw.get("_listing_record", raw if "property" not in set(raw.get("_sources") or []) else {})
    listing_price = _int(_first_value(listing, ("price", "listPrice", "listingPrice")))
    recorded_sale = _recorded_sale(raw)
    sold_price = recorded_sale.price if recorded_sale else None
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
        listed_date=_str(_first_value(listing, ("listedDate", "dateListed"))),
        removed_date=_str(_first_value(listing, ("removedDate", "dateRemoved", "saleDate", "soldDate"))),
        last_seen_date=_str(_first_value(listing, ("lastSeenDate",))),
        status=_str(_first_value(listing, ("status",))),
        days_on_market=source_dom(_first_value(listing, ("daysOnMarket", "dom"))),
        price_per_sqft=ppsf,
        hoa_fee=hoa_fee,
        hoa_match=hoa_match,
        notes=notes,
        exclude_from_median=exclude,
        raw=raw,
        recorded_sale=recorded_sale,
        source_property_types=dict(raw.get("_source_property_types") or {}),
        listing_observations=[ListingObservation(**item) for item in raw.get("_listing_observations", [])],
        redfin_url=next((url for value in raw.get("_source_urls", []) if (url := _redfin_url(value))), None),
    )


def _with_computed_distance(comp: ComparableSale, raw: Dict[str, Any], subject_lat: Optional[float], subject_lon: Optional[float]) -> ComparableSale:
    if comp.distance_miles is not None or subject_lat is None or subject_lon is None:
        return comp
    lat = _float(raw.get("latitude"))
    lon = _float(raw.get("longitude"))
    distance = _distance_miles(subject_lat, subject_lon, lat, lon)
    if distance is None:
        return comp
    return replace(comp, distance_miles=distance)


def _distance_miles(lat1: float, lon1: float, lat2: Optional[float], lon2: Optional[float]) -> Optional[float]:
    if lat2 is None or lon2 is None:
        return None
    radius_miles = 3958.8
    lat1_rad, lon1_rad, lat2_rad, lon2_rad = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    return radius_miles * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _with_rank_score(home: HomeConfig, comp: ComparableSale) -> ComparableSale:
    return replace(comp, rank_score=_rank_score(home, comp))


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
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days)


def _within_lookback(comp: ComparableSale, cutoff: datetime) -> bool:
    sources = set(comp.raw.get("_sources") or [])
    now = datetime.now(timezone.utc)
    if comp.recorded_sale:
        transaction_date = _parse_date(comp.recorded_sale.transaction_date)
        if transaction_date is not None and cutoff <= transaction_date <= now:
            return True
    if sources == {"property"}:
        return False
    dates = []
    mixed_listing = {"listing", "property"}.issubset(sources)
    observation_dates = (comp.removed_date, comp.last_seen_date, comp.listed_date)
    for value in observation_dates:
        parsed = _parse_date(value)
        if parsed is not None:
            dates.append(parsed)
    return any(cutoff <= date <= now for date in dates) if dates else not mixed_listing


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    except (TypeError, ValueError):
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
    if comp.recorded_sale:
        return _parse_date(comp.recorded_sale.transaction_date)
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
    previous_units = _explicit_units(existing)
    incoming_units = _explicit_units(raw)
    existing["_listing_identity_conflict"] = bool(existing.get("_listing_identity_conflict")) or (
        len(previous_units) > 1 or len(incoming_units) > 1
        or bool(previous_units and incoming_units and previous_units != incoming_units)
    )
    classification = raw.get("propertyType")
    if isinstance(classification, str) and classification.strip():
        existing.setdefault("_source_property_types", {})[source] = classification.strip()
    existing.setdefault("_source_urls", []).extend(raw[key] for key in ("listingUrl", "redfinUrl", "url") if isinstance(raw.get(key), str))
    if source == "listing":
        existing.setdefault("_listing_observations", []).extend(_listing_observations(raw))
    sources = set(existing.get("_sources") or [])
    existing.update({key: value for key, value in raw.items() if not key.startswith("_") and value is not None and value != ""})
    if source in {"listing", "avm"}:
        listing_keys = ("listPrice", "listingPrice", "originalListPrice", "price", "listedDate", "dateListed", "removedDate", "dateRemoved", "saleDate", "soldDate", "lastSeenDate", "status", "daysOnMarket", "dom")
        existing["_listing_record"] = {key: raw[key] for key in listing_keys if key in raw}
    if source == "property":
        sale_keys = ("lastSalePrice", "lastSaleDate", "soldPrice", "soldDate", "salePrice", "saleDate")
        existing["_sale_record"] = {key: raw[key] for key in sale_keys if key in raw}
        if isinstance(raw.get("history"), dict):
            history_keys = ("event", "price", "salePrice", "soldPrice", "date", "saleDate", "soldDate")
            existing["_sale_record"]["history"] = {
                date: {key: event[key] for key in history_keys if key in event}
                for date, event in raw["history"].items() if isinstance(event, dict)
            }
        existing["_sale_record"]["_address"] = address
        existing["_sale_record"]["_units"] = sorted(_explicit_units(raw))
        property_units = _explicit_units(raw)
        existing["_sale_record"]["_identity_conflict"] = (
            len(previous_units) > 1 or len(property_units) > 1
            or bool(previous_units and property_units and previous_units != property_units)
        )
    sources.add(source)
    existing["_sources"] = sorted(sources)


def _explicit_units(raw: Dict[str, Any]) -> set[str]:
    return {re.sub(r"^(?:apartment|apt|unit|suite|ste|#)\s*", "", str(raw[key]).strip().lower())
            for key in ("unit", "unitNumber", "unitId", "addressLine2") if raw.get(key)}


def _recorded_sale(raw: Dict[str, Any]) -> Optional[RecordedSale]:
    if "property" not in set(raw.get("_sources") or []):
        return None
    record = raw.get("_sale_record", raw)
    if not isinstance(record, dict):
        return None
    if record.get("_identity_conflict"):
        return None
    if record.get("_address") and _normalized_address(record["_address"]) != _normalized_address(_address(raw)):
        return None
    units = _explicit_units(raw)
    recorded_units = set(record.get("_units", []))
    if len(units) > 1 or (units and recorded_units and units != recorded_units):
        return None
    candidates = []

    def add(price: Any, date: Any, source: str) -> None:
        try:
            amount = _int(price) if not isinstance(price, bool) else None
        except (TypeError, OverflowError):
            return
        parsed = _parse_date(date)
        if amount is not None and amount > 0 and parsed is not None:
            candidates.append(RecordedSale(amount, parsed.date().isoformat(), source))

    add(record.get("lastSalePrice"), record.get("lastSaleDate"), "property.lastSale")
    add(record.get("soldPrice"), record.get("soldDate") or record.get("saleDate"), "property.sold")
    add(record.get("salePrice"), record.get("saleDate") or record.get("soldDate"), "property.sale")
    history = record.get("history")
    if isinstance(history, dict):
        for date, event in history.items():
            if not isinstance(event, dict) or str(event.get("event") or "").strip().lower() not in {"sale", "sold"}:
                continue
            parsed_key = _parse_date(date)
            event_dates = [event[key] for key in ("date", "saleDate", "soldDate") if event.get(key)]
            if parsed_key is None or any(_parse_date(value) != parsed_key for value in event_dates):
                continue
            for key in ("price", "salePrice", "soldPrice"):
                add(event.get(key), date, "property.history")
    if not candidates:
        return None
    latest_date = max(sale.transaction_date for sale in candidates)
    latest = [sale for sale in candidates if sale.transaction_date == latest_date]
    if latest_date > datetime.now(timezone.utc).date().isoformat() or len({sale.price for sale in latest}) != 1:
        return None
    return latest[0]


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


def _listing_observations(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    # Only called for actual listing endpoint rows; never property or AVM prices.
    result = []
    history = raw.get("history")
    entries = list(history.values()) if isinstance(history, dict) else history if isinstance(history, list) else []
    for index, entry in enumerate([raw] + [item for item in entries if isinstance(item, dict)]):
        price = _int(_first_value(entry, ("price", "listPrice", "listingPrice")))
        listed = _str(_first_value(entry, ("listedDate", "dateListed")))
        observed = _str(entry.get("lastSeenDate"))
        removed = _str(_first_value(entry, ("removedDate", "dateRemoved")))
        if (index and not observed and listed == _str(raw.get("listedDate"))
                and removed == _str(raw.get("removedDate"))
                and price == _int(_first_value(raw, ("price", "listPrice", "listingPrice")))
                and entry.get("mlsNumber") in (None, "", raw.get("mlsNumber"))):
            continue
        mls = entry.get("mlsNumber")
        if not mls and index and listed and listed == _str(raw.get("listedDate")):
            current_removed = _str(raw.get("removedDate"))
            if removed == current_removed or (not removed and not current_removed):
                mls = raw.get("mlsNumber")
        if price is not None or listed or observed:
            result.append(dict(price=price if price is not None else 0, observed_date=observed or "", listed_date=listed or "",
                               removed_date=removed, mls_number=str(mls) if mls else None,
                               days_on_market=source_dom(_first_value(entry, ("daysOnMarket", "dom"))),
                               source="listing" if index == 0 else "listing.history"))
    return result


def _redfin_url(value: Any) -> Optional[str]:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or parsed.netloc not in {"redfin.com", "www.redfin.com"}
                or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
                or any(segment in {".", ".."} for segment in parsed.path.split("/"))
                or not re.fullmatch(r"/(?:[A-Za-z0-9_.~-]+/)*home/[0-9]+", parsed.path)):
            return None
        return value
    except ValueError:
        return None
