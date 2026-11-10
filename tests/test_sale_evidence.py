from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from models import HomeConfig, RecordedSale
from normalize import _event_date, _merge_record, _normalize_comp, _recorded_sale, normalize_comps


class SaleEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.home = HomeConfig("home", "Home", "1 Subject St", "Single Family", 2, 180, 10)
        clock = patch("normalize.datetime", wraps=datetime)
        mocked = clock.start()
        mocked.now.return_value = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.addCleanup(clock.stop)
        self.address = "100 Example St, Sample City, CA"

    def property_record(self, **fields):
        return {"formattedAddress": self.address, "_sources": ["property"], **fields}

    def test_property_only_paired_sale_survives_and_ranks_by_transaction(self):
        payload = {"properties": [self.property_record(lastSalePrice=900000, lastSaleDate="2026-10-01T12:00:00Z")]}
        comp = normalize_comps(self.home, payload)[0]
        self.assertEqual(comp.recorded_sale, RecordedSale(900000, "2026-10-01", "property.lastSale"))
        self.assertEqual(comp.sold_price, 900000)
        self.assertEqual(_event_date(comp), datetime(2026, 10, 1, tzinfo=timezone.utc))

    def test_transaction_on_lookback_boundary_remains_eligible_all_day(self):
        with patch("normalize.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 9, 23, 59, tzinfo=timezone.utc)
            payload = {"properties": [self.property_record(lastSalePrice=900000, lastSaleDate="2026-05-13")]}
            self.assertEqual(len(normalize_comps(self.home, payload)), 1)

    def test_missing_invalid_nonpositive_and_future_pairs_are_unknown(self):
        for fields in (
            {"lastSalePrice": 900000},
            {"lastSaleDate": "2026-10-01"},
            {"lastSalePrice": 900000, "lastSaleDate": "2026-02-30"},
            {"lastSalePrice": 900000, "lastSaleDate": "2026-12-01"},
            {"lastSalePrice": -1, "lastSaleDate": "2026-10-01"},
            {"lastSalePrice": 0, "lastSaleDate": "2026-10-01"},
        ):
            with self.subTest(fields=fields):
                raw = self.property_record(**fields)
                self.assertIsNone(_recorded_sale(raw))
                self.assertEqual(normalize_comps(self.home, {"properties": [raw]}), [])

    def test_price_and_date_must_come_from_same_property_source(self):
        merged = {}
        _merge_record(merged, {"formattedAddress": self.address, "salePrice": 950000, "soldDate": "2026-10-01"}, "listing")
        _merge_record(merged, {"formattedAddress": self.address, "lastSalePrice": 900000}, "property")
        raw = next(iter(merged.values()))
        self.assertIsNone(_recorded_sale(raw))
        self.assertIsNone(_normalize_comp(self.home, raw).sold_price)

    def test_listing_history_and_avm_prices_cannot_be_recorded_sales(self):
        for source in ("listing", "avm"):
            raw = {"_sources": [source], "salePrice": 900000, "saleDate": "2026-10-01", "history": {"2026-10-01": {"event": "Sale", "price": 900000}}}
            self.assertIsNone(_recorded_sale(raw))
        raw = self.property_record(history={"2026-10-01": {"event": "Sale Listing", "price": 900000}})
        self.assertIsNone(_recorded_sale(raw))

    def test_property_provenance_survives_later_listing_overwrite(self):
        merged = {}
        _merge_record(merged, self.property_record(lastSalePrice=900000, lastSaleDate="2026-10-01"), "property")
        _merge_record(merged, {"formattedAddress": self.address, "lastSalePrice": 999000, "lastSaleDate": "2026-10-15", "history": {"2026-10-15": {"event": "Sale", "price": 999000}}}, "listing")
        self.assertEqual(_recorded_sale(next(iter(merged.values()))), RecordedSale(900000, "2026-10-01", "property.lastSale"))

    def test_conflicting_latest_transaction_is_unknown_without_older_fallback(self):
        raw = self.property_record(lastSalePrice=900000, lastSaleDate="2026-10-01", history={
            "2026-10-01": {"event": "Sold", "price": 950000},
            "2025-01-01": {"event": "Sale", "price": 800000},
        })
        self.assertIsNone(_recorded_sale(raw))

    def test_history_date_must_match_its_key_and_event_is_exact(self):
        for event in (
            {"event": "Sale", "price": 900000, "date": "2026-09-01"},
            {"event": "Sale", "price": 900000, "soldDate": "invalid"},
            {"event": "Sale Pending", "price": 900000},
        ):
            self.assertIsNone(_recorded_sale(self.property_record(history={"2026-10-01": event})))
        raw = self.property_record(history={"2026-10-01": {"event": "Sold", "price": 900000, "date": "2026-10-01T12:00:00Z"}})
        self.assertEqual(_recorded_sale(raw), RecordedSale(900000, "2026-10-01", "property.history"))

    def test_old_sale_stays_old_when_recent_listing_keeps_mixed_row(self):
        payload = {
            "sale_listings": [{"formattedAddress": self.address, "price": 950000, "listedDate": "2026-10-15"}],
            "properties": [self.property_record(history={"2020-01-01": {"event": "Sale", "price": 700000}})],
        }
        comp = normalize_comps(self.home, payload)[0]
        self.assertEqual(comp.recorded_sale.transaction_date, "2020-01-01")
        self.assertEqual(_event_date(comp), datetime(2020, 1, 1, tzinfo=timezone.utc))
        payload.pop("sale_listings")
        self.assertEqual(normalize_comps(self.home, payload), [])

    def test_recent_sale_survives_stale_listing_observation(self):
        payload = {
            "sale_listings": [{"formattedAddress": self.address, "listedDate": "2020-01-01"}],
            "properties": [self.property_record(soldPrice=900000, soldDate="2026-10-01")],
        }
        self.assertEqual(len(normalize_comps(self.home, payload)), 1)

    def test_unpaired_property_date_cannot_refresh_stale_listing(self):
        payload = {
            "sale_listings": [{"formattedAddress": self.address, "listedDate": "2020-01-01"}],
            "properties": [self.property_record(saleDate="2026-10-01")],
        }
        self.assertEqual(normalize_comps(self.home, payload), [])

    def test_conflicting_explicit_unit_or_address_cannot_grant_sale(self):
        merged = {}
        _merge_record(merged, {"formattedAddress": self.address, "unitNumber": "1"}, "listing")
        _merge_record(merged, self.property_record(unitNumber="2", lastSalePrice=900000, lastSaleDate="2026-10-01"), "property")
        self.assertIsNone(_recorded_sale(next(iter(merged.values()))))
        merged = {}
        _merge_record(merged, self.property_record(lastSalePrice=900000, lastSaleDate="2026-10-01"), "property")
        raw = next(iter(merged.values()))
        raw["formattedAddress"] = "100 Example St Unit 2, Sample City, CA"
        self.assertIsNone(_recorded_sale(raw))

    def test_unit_alias_conflicts_are_rejected_but_equivalent_aliases_match(self):
        for property_unit, expected in (("2", False), ("Unit 1", True)):
            with self.subTest(property_unit=property_unit):
                merged = {}
                _merge_record(merged, {"formattedAddress": self.address, "unitNumber": "1", "status": "Active", "price": 950000, "lastSeenDate": "2026-11-09"}, "listing")
                _merge_record(merged, self.property_record(unit=property_unit, lastSalePrice=900000, lastSaleDate="2026-10-01"), "property")
                self.assertEqual(_recorded_sale(next(iter(merged.values()))) is not None, expected)
                if not expected:
                    from models import HomeRun, TrackerRun
                    from render import render_digest
                    from state import TrackerState
                    comp = _normalize_comp(self.home, next(iter(merged.values())))
                    body = render_digest(TrackerRun([HomeRun(self.home, [comp])], "2026-11-09T16:00:00+00:00"), TrackerState())["text_body"]
                    self.assertIn("待售開價 0 筆", body)
                    self.assertNotIn("$950,000", body)

    def test_original_asking_price_is_not_current_asking_evidence(self):
        from models import HomeRun, TrackerRun
        from render import render_digest
        from state import TrackerState
        rows = [{"formattedAddress": self.address, "status": "Active", "lastSeenDate": "2026-11-09", "originalListPrice": 999000, "price": 950000},
                {"formattedAddress": "200 Synthetic St", "status": "Active", "lastSeenDate": "2026-11-09", "originalListPrice": 850000}]
        comps = normalize_comps(self.home, {"sale_listings": rows})
        body = render_digest(TrackerRun([HomeRun(self.home, comps)], "2026-11-09T16:00:00+00:00"), TrackerState())["text_body"]
        self.assertIn("開價 $950,000", body)
        self.assertIn("待售開價 1 筆", body)
        self.assertNotIn("$999,000", body)
        self.assertNotIn("$850,000", body)

    def test_property_observation_cannot_refresh_listing_or_avm_inventory(self):
        from models import HomeRun, TrackerRun
        from render import render_digest
        from state import TrackerState
        for source in ("sale_listings", "avm"):
            with self.subTest(source=source):
                listing = {"formattedAddress": self.address, "status": "Active", "price": 950000, "lastSeenDate": "2020-01-01"}
                payload = {source: [listing] if source == "sale_listings" else {"comparables": [listing]},
                           "properties": [self.property_record(lastSalePrice=800000, lastSaleDate="2026-10-01", lastSeenDate="2026-11-09", status="Active", price=999999)]}
                comps = normalize_comps(self.home, payload)
                self.assertEqual(comps[0].last_seen_date, "2020-01-01")
                self.assertEqual(comps[0].listing_price, 950000)
                body = render_digest(TrackerRun([HomeRun(self.home, comps)], "2026-11-09T16:00:00+00:00"), TrackerState())["text_body"]
                self.assertIn("待售開價 0 筆", body)
                self.assertIn("成交價 $800,000", body)
                self.assertNotIn("開價 $950,000", body)

    def test_internal_evidence_does_not_copy_owner_or_contact_fields(self):
        merged = {}
        _merge_record(merged, self.property_record(owner={"name": "Private"}, history={"2026-10-01": {"event": "Sale", "price": 900000, "contact": "Private"}}), "property")
        evidence = next(iter(merged.values()))["_sale_record"]
        self.assertNotIn("owner", evidence)
        self.assertNotIn("contact", evidence["history"]["2026-10-01"])


if __name__ == "__main__":
    unittest.main()
