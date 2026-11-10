from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class HomeConfig:
    id: str
    name: str
    address: str
    property_type: str
    max_radius_miles: float
    days_old: int
    comp_count: int
    purchase_price: Optional[int] = None
    bedrooms: Optional[str] = None
    bathrooms: Optional[str] = None
    square_footage: Optional[str] = None
    lot_size: Optional[str] = None
    year_built: Optional[str] = None
    comp_square_footage: Optional[str] = None
    comp_lot_size: Optional[str] = None
    listing_bedrooms: Optional[str] = None
    listing_bathrooms: Optional[str] = None
    display_count: Optional[int] = None
    close_listing_radius_miles: Optional[float] = None
    ranked_comp_bedrooms: Optional[str] = None
    ranked_comp_bathrooms: Optional[str] = None
    ranked_comp_square_footage: Optional[str] = None
    ranked_comp_lot_size: Optional[str] = None
    comp_annotations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    same_hoa_addresses: List[str] = field(default_factory=list)
    same_hoa_patterns: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RecordedSale:
    price: int
    transaction_date: str
    source: str


@dataclass(frozen=True)
class ListingObservation:
    price: int
    observed_date: str
    listed_date: str
    removed_date: Optional[str]
    mls_number: Optional[str]
    days_on_market: Optional[int]
    source: str


@dataclass(frozen=True)
class RedfinReference:
    address: str
    matched_address: str
    url: str
    checked_at: str
    source: str
    facts: List[str] = field(default_factory=list)
    values: Dict[str, Any] = field(default_factory=dict)
    recorded_sale: Optional[RecordedSale] = None
    listing_observations: List[ListingObservation] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    source_group: str = "unknown"
    field_evidence: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    ownership: str = "unknown"


@dataclass(frozen=True)
class RetrievalInfo:
    source: str
    rows: int
    pages: int
    complete: bool
    filters: Dict[str, Any] = field(default_factory=dict)
    fetched_at: Optional[str] = None


@dataclass(frozen=True)
class ComparableSale:
    id: str
    address: str
    distance_miles: Optional[float] = None
    beds: Optional[float] = None
    baths: Optional[float] = None
    sqft: Optional[int] = None
    lot_size: Optional[int] = None
    year_built: Optional[int] = None
    listing_price: Optional[int] = None
    sold_price: Optional[int] = None
    market_price: Optional[int] = None
    listed_date: Optional[str] = None
    removed_date: Optional[str] = None
    last_seen_date: Optional[str] = None
    status: Optional[str] = None
    days_on_market: Optional[int] = None
    price_per_sqft: Optional[float] = None
    hoa_fee: Optional[int] = None
    hoa_match: bool = False
    notes: List[str] = field(default_factory=list)
    exclude_from_median: bool = False
    rank_score: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    recorded_sale: Optional[RecordedSale] = None
    source_property_types: Dict[str, str] = field(default_factory=dict)
    listing_observations: List[ListingObservation] = field(default_factory=list)
    redfin_url: Optional[str] = None
    redfin_reference: Optional[RedfinReference] = None
    field_sources: Dict[str, str] = field(default_factory=dict)
    source_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    unresolved_fields: List[str] = field(default_factory=list)
    reconciliation_notes: List[str] = field(default_factory=list)

    @property
    def sale_price(self) -> Optional[int]:
        if "recorded_sale" in self.unresolved_fields or "sale_price" in self.unresolved_fields:
            return None
        return self.sold_price or self.market_price or self.listing_price

    @property
    def sale_date(self) -> Optional[str]:
        if "recorded_sale" in self.unresolved_fields or "sale_date" in self.unresolved_fields:
            return None
        if self.recorded_sale and self.field_sources.get("recorded_sale") == "redfin":
            return self.recorded_sale.transaction_date
        return self.removed_date or self.last_seen_date or self.listed_date


@dataclass(frozen=True)
class HomeRun:
    home: HomeConfig
    comps: List[ComparableSale]
    value_estimate: Optional[int] = None
    subject_property: Dict[str, Any] = field(default_factory=dict)
    nearby_listings: List[ComparableSale] = field(default_factory=list)
    retrieval: List[RetrievalInfo] = field(default_factory=list)
    candidates: List[ComparableSale] = field(default_factory=list)

    @property
    def ranked_comps(self) -> List[ComparableSale]:
        from market_metrics import cohort_for
        return [comp for comp in self.comps if cohort_for(self.home, comp) != "excluded_condo"
                and self._passes_ranked_comp_filters(comp)]

    @property
    def selected_comps(self) -> List[ComparableSale]:
        comps = self.ranked_comps
        if self.home.display_count and self.home.display_count > 0:
            return comps[: self.home.display_count]
        return comps

    @property
    def final_comps(self) -> List[ComparableSale]:
        return [comp for comp in self.selected_comps if not comp.exclude_from_median][:5]

    @property
    def median_sale_price(self) -> Optional[float]:
        values = [comp.sale_price for comp in self.final_comps if comp.sale_price is not None]
        return float(median(values)) if values else None

    @property
    def median_price_per_sqft(self) -> Optional[float]:
        values = [comp.price_per_sqft for comp in self.final_comps if comp.price_per_sqft is not None]
        return float(median(values)) if values else None

    def _passes_ranked_comp_filters(self, comp: ComparableSale) -> bool:
        if not _number_in_range(comp.beds, self.home.ranked_comp_bedrooms):
            return False
        if not _number_in_range(comp.baths, self.home.ranked_comp_bathrooms):
            return False
        if not _number_in_range(comp.sqft, self.home.ranked_comp_square_footage):
            return False
        if not _number_in_range(comp.lot_size, self.home.ranked_comp_lot_size):
            return False
        return True


def _number_in_range(value: Optional[float], raw_range: Optional[str]) -> bool:
    if not raw_range:
        return True
    if value is None:
        return False
    raw_range = str(raw_range)
    if ":" not in raw_range:
        target = _optional_float(raw_range)
        return target is not None and value == target
    low_raw, high_raw = raw_range.split(":", 1)
    low = _optional_float(low_raw) if low_raw else None
    high = _optional_float(high_raw) if high_raw else None
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def _optional_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TrackerRun:
    homes: List[HomeRun]
    generated_at: str

    @staticmethod
    def now(homes: List[HomeRun]) -> "TrackerRun":
        return TrackerRun(homes=homes, generated_at=datetime.now(timezone.utc).isoformat())
