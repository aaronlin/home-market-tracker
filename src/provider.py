from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app_config import SetupError, require_env
from models import HomeConfig


class ProviderError(RuntimeError):
    """Raised when a property data provider call fails."""


class PropertyDataProvider(Protocol):
    def fetch_home_market(self, home: HomeConfig) -> Dict[str, Any]:
        ...


class RentCastProvider:
    """RentCast-backed provider using the valuation endpoint with sale comps."""

    def __init__(self, base_url: str, api_key: str | None = None, timeout_seconds: int = 30, max_pages: int = 3):
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self.max_pages = max_pages
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or require_env("HOME_MARKET_RENTCAST_API_KEY")
        self.timeout_seconds = timeout_seconds

    def fetch_home_market(self, home: HomeConfig) -> Dict[str, Any]:
        avm = self.fetch_home_comps(home)
        listings = self.fetch_sale_listings(home)
        properties = self.fetch_properties(home)
        return {"avm": avm, "sale_listings": listings, "properties": properties,
                "_retrieval": listings["searches"] + properties["searches"]}

    def fetch_home_comps(self, home: HomeConfig) -> Dict[str, Any]:
        params = {
            "address": home.address,
            "propertyType": home.property_type,
            "maxRadius": home.max_radius_miles,
            "daysOld": home.days_old,
            "compCount": home.comp_count,
        }
        if home.bedrooms:
            params["bedrooms"] = home.bedrooms
        if home.bathrooms:
            params["bathrooms"] = home.bathrooms
        if home.square_footage:
            params["squareFootage"] = home.square_footage
        if home.lot_size:
            params["lotSize"] = home.lot_size
        if home.year_built:
            params["yearBuilt"] = home.year_built
        return self._get_json("/avm/value", params)

    def fetch_sale_listings(self, home: HomeConfig) -> Dict[str, Any]:
        searches = []
        records = []
        for property_type in home.comparable_property_types or [home.property_type]:
            for status in ("Inactive", "Active"):
                params = {**self._search_params(home), "propertyType": property_type, "status": status}
                result = self._get_paginated_json("/listings/sale", params, f"listings_{status.lower()}")
                records.extend(result["records"])
                searches.extend(result["searches"])
        return {"records": records, "searches": searches}

    def fetch_properties(self, home: HomeConfig) -> Dict[str, Any]:
        params = {"address": home.address, "radius": home.max_radius_miles,
                  "saleDateRange": home.days_old if home.days_old > 0 else 365}
        return self._get_paginated_json("/properties", params, "properties")

    def _search_params(self, home: HomeConfig) -> Dict[str, Any]:
        types = home.property_type
        params: Dict[str, Any] = {"address": home.address, "radius": home.max_radius_miles,
                                  "propertyType": types}
        if home.listing_bedrooms:
            params["bedrooms"] = home.listing_bedrooms
        if home.listing_bathrooms:
            params["bathrooms"] = home.listing_bathrooms
        if home.comp_square_footage:
            params["squareFootage"] = home.comp_square_footage
        if home.comp_lot_size:
            params["lotSize"] = home.comp_lot_size
        return params

    def _get_json(self, path: str, params: Dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(url, headers={"X-Api-Key": self.api_key, "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = _http_error_detail(exc)
            if exc.code in {401, 403}:
                raise SetupError(f"RentCast authorization failed ({exc.code}). Check HOME_MARKET_RENTCAST_API_KEY.") from exc
            if exc.code == 429:
                raise SetupError("RentCast quota/rate limit reached. Wait for quota reset or upgrade the API plan.") from exc
            raise ProviderError(f"RentCast request failed ({exc.code}): {detail}") from exc
        except URLError as exc:
            raise ProviderError(f"RentCast request failed: {exc.reason}") from exc
        try:
            loaded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProviderError("RentCast returned invalid JSON.") from exc
        if not isinstance(loaded, (dict, list)):
            raise ProviderError("RentCast returned an unexpected response.")
        return loaded

    def _get_paginated_json(self, path: str, params: Dict[str, Any], source: str) -> Dict[str, Any]:
        page_size = 500
        records = []
        complete = False
        pages = 0
        for offset in range(0, page_size * self.max_pages, page_size):
            loaded = self._get_json(path, {**params, "limit": page_size, "offset": offset})
            page = _response_records(loaded)
            if any(not isinstance(record, dict) for record in page):
                raise ProviderError("RentCast search returned an invalid property record.")
            records.extend(page)
            pages += 1
            if len(page) < page_size:
                complete = True
                break
        filters = {key: value for key, value in params.items() if key != "address"}
        metadata = {"source": source, "rows": len(records), "pages": pages, "complete": complete,
                    "filters": filters, "fetched_at": datetime.now(timezone.utc).isoformat()}
        return {"records": records, "searches": [metadata]}


def _response_records(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("listings", "properties", "records", "data", "results"):
            maybe = value.get(key)
            if isinstance(maybe, list):
                return maybe
    raise ProviderError("RentCast search returned an unexpected records envelope.")


def _http_error_detail(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return str(exc)


def write_raw_snapshot(root: Path, home_id: str, payload: Dict[str, Any], generated_at: str) -> Path:
    safe_time = generated_at.replace(":", "").replace("+", "Z")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe_time}-{home_id}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path
