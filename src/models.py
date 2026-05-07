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
    display_count: Optional[int] = None
    comp_annotations: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    same_hoa_addresses: List[str] = field(default_factory=list)
    same_hoa_patterns: List[str] = field(default_factory=list)


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

    @property
    def sale_price(self) -> Optional[int]:
        return self.sold_price or self.market_price or self.listing_price

    @property
    def sale_date(self) -> Optional[str]:
        return self.removed_date or self.last_seen_date or self.listed_date


@dataclass(frozen=True)
class HomeRun:
    home: HomeConfig
    comps: List[ComparableSale]
    value_estimate: Optional[int] = None
    subject_property: Dict[str, Any] = field(default_factory=dict)

    @property
    def selected_comps(self) -> List[ComparableSale]:
        if self.home.display_count and self.home.display_count > 0:
            return self.comps[: self.home.display_count]
        return list(self.comps)

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


@dataclass(frozen=True)
class TrackerRun:
    homes: List[HomeRun]
    generated_at: str

    @staticmethod
    def now(homes: List[HomeRun]) -> "TrackerRun":
        return TrackerRun(homes=homes, generated_at=datetime.now(timezone.utc).isoformat())
