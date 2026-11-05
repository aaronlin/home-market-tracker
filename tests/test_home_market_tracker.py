from __future__ import annotations

import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

APP_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(APP_SRC))

import app_config
from app_config import SetupError, _load_env_file, load_yaml
from models import ComparableSale, HomeConfig, HomeRun, TrackerRun
from normalize import new_comps, normalize_comps, normalize_nearby_listings, percent_change
from provider import RentCastProvider
from render import render_digest
from run_weekly import _subject, run_preview, run_send
from state import HomeState, TrackerState, load_state


class FakeProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def fetch_home_market(self, home):
        self.calls.append(home)
        return self.payload

    def fetch_home_comps(self, home):
        self.calls.append(home)
        return self.payload


def sample_payload():
    return {
        "price": 2000000,
        "comparables": [
            {
                "id": "a",
                "formattedAddress": "100 Sample St, Example City, CA",
                "distance": 0.4,
                "bedrooms": 3,
                "bathrooms": 2,
                "squareFootage": 1500,
                "lotSize": 6000,
                "yearBuilt": 1960,
                "listPrice": 1800000,
                "salePrice": 1900000,
                "removedDate": "2026-04-20T00:00:00Z",
                "daysOnMarket": 9,
            },
            {
                "id": "b",
                "formattedAddress": "200 Sample St, Example City, CA",
                "distance": 1.1,
                "bedrooms": 4,
                "bathrooms": 2.5,
                "squareFootage": 2000,
                "salePrice": 2200000,
                "saleDate": "2026-03-20",
            },
        ],
    }


def settings(tmpdir):
    root = Path(tmpdir)
    return {
        "rentcast": {"base_url": "https://api.rentcast.io/v1"},
        "tracker": {
            "subject_prefix": "[Home Market]",
            "state_path": str(root / "state.yaml"),
            "preview_root": str(root / "previews"),
            "raw_snapshot_root": str(root / "raw"),
            "comp_defaults": {
                "property_type": "Single Family",
                "max_radius_miles": 1.5,
                "days_old": 180,
                "comp_count": 10,
            },
            "homes": [{"id": "home", "name": "Home", "address": "1 Main St, Test, CA"}],
        },
    }


class HomeMarketTrackerTests(unittest.TestCase):
    def setUp(self):
        # Keep dated market fixtures inside their lookback window as time passes.
        clock = patch("normalize.datetime", wraps=datetime)
        self.addCleanup(clock.stop)
        clock.start().now.return_value = datetime(2026, 5, 6, tzinfo=timezone.utc)

    def test_settings_load(self):
        app_root = Path(__file__).resolve().parents[1]
        loaded = load_yaml(app_root / "config" / "settings.example.yaml")
        self.assertIn("tracker", loaded)
        self.assertEqual(len(loaded["tracker"]["homes"]), 2)

    def test_settings_path_env_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "override.yaml"
            settings_path.write_text("tracker:\n  homes:\n    - id: override\n", encoding="utf-8")
            with patch.dict(os.environ, {"HOME_MARKET_SETTINGS_PATH": str(settings_path)}, clear=False):
                loaded = app_config.load_settings()
            self.assertEqual(loaded["tracker"]["homes"][0]["id"], "override")

    def test_settings_path_env_missing_fails_clearly(self):
        with patch.dict(os.environ, {"HOME_MARKET_SETTINGS_PATH": "/missing/home-market-settings.yaml"}, clear=False):
            with self.assertRaises(SetupError):
                app_config.load_settings()

    def test_settings_file_is_loaded_before_example(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            config_root = app_root / "config"
            config_root.mkdir()
            (config_root / "settings.yaml").write_text("tracker:\n  homes:\n    - id: private\n", encoding="utf-8")
            (config_root / "settings.example.yaml").write_text("tracker:\n  homes:\n    - id: example\n", encoding="utf-8")
            with patch.object(app_config, "APP_ROOT", app_root):
                with patch.dict(os.environ, {}, clear=True):
                    loaded = app_config.load_settings()
            self.assertEqual(loaded["tracker"]["homes"][0]["id"], "private")

    def test_example_settings_are_not_used_as_runtime_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_root = Path(tmpdir)
            config_root = app_root / "config"
            config_root.mkdir()
            (config_root / "settings.example.yaml").write_text("tracker:\n  homes:\n    - id: example\n", encoding="utf-8")
            with patch.object(app_config, "APP_ROOT", app_root):
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaises(SetupError):
                        app_config.load_settings()

    def test_env_loader_preserves_existing_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env.local"
            env_path.write_text("HOME_MARKET_TEST_EXISTING=file\nHOME_MARKET_TEST_NEW='new value'\n", encoding="utf-8")
            with patch.dict(os.environ, {"HOME_MARKET_TEST_EXISTING": "env"}, clear=False):
                _load_env_file(env_path)
                self.assertEqual(os.environ["HOME_MARKET_TEST_EXISTING"], "env")
                self.assertEqual(os.environ["HOME_MARKET_TEST_NEW"], "new value")

    def test_normalizes_comps_and_derived_metrics(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 180, 10)
        comps = normalize_comps(home, sample_payload())
        first = comps[0]
        self.assertEqual(first.address, "100 Sample St, Example City, CA")
        self.assertEqual(first.sale_price, 1900000)
        self.assertIsNone(first.sold_price)
        self.assertEqual(first.market_price, 1900000)
        self.assertEqual(first.price_per_sqft, 1900000 / 1500)
        self.assertEqual(first.removed_date, "2026-04-20")

    def test_normalization_excludes_subject_property_from_comps(self):
        home = HomeConfig("home", "Home", "200 Example Ln, Sampletown, CA", "Townhouse", 1.5, 180, 10)
        payload = {
            "subjectProperty": {"formattedAddress": "200 Example Ln, Sampletown, CA 90000"},
            "comparables": [
                {"formattedAddress": "200 Example Ln, Sampletown, CA 90000", "price": 1998000},
                {"formattedAddress": "210 Example Ln, Sampletown, CA 90000", "price": 1995000},
            ],
        }
        comps = normalize_comps(home, payload)
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0].address, "210 Example Ln, Sampletown, CA 90000")

    def test_normalization_applies_local_comp_filters(self):
        home = HomeConfig(
            "home",
            "Home",
            "1 Main",
            "Single Family",
            1.5,
            180,
            25,
            comp_square_footage="1000:1900",
            comp_lot_size="5500:10000",
            display_count=1,
        )
        payload = {
            "comparables": [
                {"formattedAddress": "Good", "squareFootage": 1500, "lotSize": 7000, "price": 1},
                {"formattedAddress": "Too small lot", "squareFootage": 1500, "lotSize": 5000, "price": 2},
                {"formattedAddress": "Too large sqft", "squareFootage": 2200, "lotSize": 7000, "price": 3},
            ]
        }
        comps = normalize_comps(home, payload)
        self.assertEqual(len(comps), 1)
        self.assertEqual(comps[0].address, "Good")

    def test_missing_optional_fields_do_not_break_normalization(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 180, 10)
        comps = normalize_comps(home, {"comparables": [{"formattedAddress": "Sparse", "salePrice": 1000000}]})
        self.assertEqual(comps[0].address, "Sparse")
        self.assertIsNone(comps[0].days_on_market)
        self.assertIsNone(comps[0].price_per_sqft)

    def test_new_comps_and_percent_change(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 180, 10)
        comps = normalize_comps(home, sample_payload())
        self.assertEqual([comp.id for comp in new_comps(comps, [comps[0].id])], [comps[1].id])
        self.assertEqual(percent_change(110, 100), 10)
        self.assertIsNone(percent_change(110, None))
        self.assertIsNone(percent_change(110, 0))

    def test_digest_notes_first_run_baseline_and_second_run_changes(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 180, 10, close_listing_radius_miles=0.5)
        comps = normalize_comps(home, sample_payload())
        nearby_listing = comps[0]
        run = TrackerRun(
            homes=[HomeRun(home=home, comps=comps, value_estimate=2000000, nearby_listings=[nearby_listing])],
            generated_at="2026-05-06T00:00:00+00:00",
        )
        first_digest = render_digest(run, TrackerState())
        self.assertIn("尚無先前寄送紀錄", first_digest["text_body"])
        self.assertIn("相似物件排序結果", first_digest["text_body"])
        self.assertIn("RentCast 初步篩選", first_digest["text_body"])
        self.assertIn("相似物件排序篩選", first_digest["text_body"])
        self.assertIn("附近待售物件", first_digest["text_body"])
        self.assertIn("附近待售物件", first_digest["html_body"])
        self.assertNotIn("自上次寄送後新增的物件：", first_digest["text_body"])
        self.assertNotIn("AVM estimate", first_digest["text_body"])
        self.assertNotIn("AVM estimate", first_digest["html_body"])
        previous = TrackerState(homes={"home": HomeState(seen_comp_ids=[comps[0].id], median_sale_price=2000000, median_price_per_sqft=1000)})
        second_digest = render_digest(run, previous)
        self.assertIn("自上次寄送後新增： 1", second_digest["text_body"])
        self.assertIn("自上次寄送後新增的物件：", second_digest["text_body"])
        self.assertIn("納入最終比較物件", second_digest["text_body"])
        self.assertIn("比較物件價格中位數", second_digest["text_body"])
        self.assertIn("比較物件每 sqft 價格中位數", second_digest["text_body"])
        self.assertIn("排序分數", second_digest["html_body"])
        self.assertIn("最終比較物件", second_digest["html_body"])
        self.assertIn("開價", second_digest["html_body"])
        self.assertIn("參考價格", second_digest["html_body"])
        self.assertIn("最後更新", second_digest["html_body"])
        self.assertIn("100 Sample St, Example City, CA", second_digest["html_body"])
        self.assertIn("$1,900,000", second_digest["html_body"])
        self.assertIn("+2.5%", second_digest["text_body"])
        self.assertIn("追蹤房屋：", second_digest["text_body"])
        self.assertNotIn("list n/a", second_digest["text_body"])
        self.assertNotIn("Latest comparable market rows", second_digest["text_body"])
        self.assertNotIn("New comparable market rows", second_digest["text_body"])

    def test_chinese_digest_preserves_addresses_sqft_and_annotations(self):
        address = "100 Sample St & <Annex>, Example City, CA"
        home = HomeConfig("home", "Home & <North>", "1 Main St", "Townhouse", 1.5, 180, 10)
        comp = ComparableSale(
            id="sample", address=address, status="Active", sqft=1500,
            listing_price=900000, notes=["Keep original note & <detail>"],
        )
        digest = render_digest(TrackerRun([HomeRun(home, [comp])], "2026-05-06T00:00:00+00:00"), TrackerState())
        self.assertEqual(digest["subject"], "房市週報：1 間房屋，1 筆新增物件")
        for body in (digest["text_body"], digest["html_body"]):
            self.assertIn("連棟住宅", body)
            self.assertIn("待售", body)
            self.assertIn("無資料", body)
            self.assertIn("sqft", body)
            self.assertIn("1500", body)
            self.assertNotIn("平方英尺", body)
            self.assertNotIn("Active", body)
        self.assertIn(address, digest["text_body"])
        self.assertIn("Keep original note & <detail>", digest["text_body"])
        self.assertIn("100 Sample St &amp; &lt;Annex&gt;, Example City, CA", digest["html_body"])
        self.assertIn("Keep original note &amp; &lt;detail&gt;", digest["html_body"])
        self.assertIn('lang="zh-Hant"', digest["html_body"])
        self.assertIn('charset="utf-8"', digest["html_body"])

    def test_subject_uses_chinese_default_and_preserves_custom_prefix(self):
        self.assertEqual(_subject({}, "房市週報"), "[房市週報] 房市週報")
        self.assertEqual(_subject({"tracker": {"subject_prefix": "[Custom]"}}, "房市週報"), "[Custom] 房市週報")

    def test_nearby_listings_are_distance_filtered_from_listing_rows(self):
        home = HomeConfig(
            "home",
            "Home",
            "1 Main",
            "Single Family",
            1.5,
            365,
            10,
            close_listing_radius_miles=0.5,
        )
        payload = {
            "avm": {"subjectProperty": {"formattedAddress": "1 Main", "latitude": 37.0, "longitude": -122.0}},
            "sale_listings": [
                {
                    "id": "near",
                    "formattedAddress": "Nearby Listing, Example City, CA",
                    "latitude": 37.001,
                    "longitude": -122.001,
                    "price": 1000000,
                    "listedDate": "2026-05-01T00:00:00Z",
                    "lastSeenDate": "2026-05-02T00:00:00Z",
                },
                {
                    "id": "far",
                    "formattedAddress": "Far Listing, Example City, CA",
                    "latitude": 37.02,
                    "longitude": -122.02,
                    "price": 1100000,
                    "listedDate": "2026-05-01T00:00:00Z",
                    "lastSeenDate": "2026-05-02T00:00:00Z",
                },
            ],
        }
        listings = normalize_nearby_listings(home, payload)
        self.assertEqual([listing.address for listing in listings], ["Nearby Listing, Example City, CA"])
        self.assertLess(listings[0].distance_miles, 0.5)

    def test_ranked_comp_filters_are_separate_from_broad_candidates(self):
        home = HomeConfig(
            "home",
            "Home",
            "1 Main",
            "Single Family",
            1.5,
            365,
            25,
            comp_square_footage="1000:2500",
            ranked_comp_bedrooms="3",
            ranked_comp_bathrooms="1:2",
            ranked_comp_square_footage="1000:1900",
            ranked_comp_lot_size="5500:11000",
        )
        comps = normalize_comps(home, sample_payload())
        run = HomeRun(home=home, comps=comps)
        self.assertEqual(len(run.comps), 2)
        self.assertEqual([comp.address for comp in run.selected_comps], ["100 Sample St, Example City, CA"])

    def test_avm_candidates_survive_when_listing_source_is_sparse(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 365, 25)
        payload = {
            "avm": {
                "subjectProperty": {"formattedAddress": "1 Main"},
                "comparables": [
                    {"formattedAddress": "AVM Only One", "price": 1000000},
                    {"formattedAddress": "AVM Only Two", "price": 1100000},
                ],
            },
            "sale_listings": [],
            "properties": [],
        }
        comps = normalize_comps(home, payload)
        self.assertEqual({comp.address for comp in comps}, {"AVM Only One", "AVM Only Two"})

    def test_subject_property_context_still_renders_from_avm(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 365, 25)
        run = TrackerRun(
            homes=[
                HomeRun(
                    home=home,
                    comps=[],
                    value_estimate=999999,
                    subject_property={"propertyType": "Single Family", "bedrooms": 3, "bathrooms": 1, "squareFootage": 1335, "lotSize": 7750, "yearBuilt": 1951},
                )
            ],
            generated_at="2026-05-06T00:00:00+00:00",
        )
        digest = render_digest(run, TrackerState())
        self.assertIn("追蹤房屋： 獨棟住宅，房間／衛浴 3/1，sqft 1335，土地面積（sqft） 7750，建造年份 1951", digest["text_body"])
        self.assertNotIn("999999", digest["text_body"])

    def test_larger_lot_sfh_ranks_higher_than_small_lot(self):
        home = HomeConfig(
            "home",
            "Home",
            "1 Main",
            "Single Family",
            1.5,
            365,
            25,
            square_footage="1335",
            lot_size="7750",
            comp_square_footage="1000:1900",
            comp_lot_size="5500:11000",
            display_count=2,
        )
        payload = {
            "comparables": [
                {"formattedAddress": "Small Lot", "squareFootage": 1335, "lotSize": 5600, "price": 2000000, "distance": 0.5},
                {"formattedAddress": "Better Lot", "squareFootage": 1335, "lotSize": 7600, "price": 2000000, "distance": 0.5},
            ]
        }
        comps = normalize_comps(home, payload)
        self.assertEqual(comps[0].address, "Better Lot")

    def test_same_hoa_townhouse_ranks_before_non_hoa(self):
        home = HomeConfig(
            "home",
            "Home",
            "200 Example Ln, Sampletown, CA",
            "Townhouse",
            1.5,
            365,
            25,
            square_footage="2510",
            comp_square_footage="1700:3000",
            same_hoa_patterns=["example ln sampletown ca"],
            display_count=2,
        )
        payload = {
            "comparables": [
                {"formattedAddress": "Far Non HOA, Sampletown, CA 90000", "squareFootage": 2510, "price": 2200000, "distance": 0.1},
                {"formattedAddress": "210 Example Ln, Sampletown, CA 90000", "squareFootage": 1990, "price": 1995000, "distance": 0.2},
            ]
        }
        comps = normalize_comps(home, payload)
        self.assertEqual(comps[0].address, "210 Example Ln, Sampletown, CA 90000")

    def test_newer_same_hoa_townhouse_ranks_before_older_physical_match(self):
        home = HomeConfig(
            "home",
            "Home",
            "200 Example Ln, Sampletown, CA",
            "Townhouse",
            1.5,
            365,
            25,
            square_footage="2510",
            comp_square_footage="1700:3000",
            same_hoa_patterns=["example ln sampletown ca", "sample hoa sampletown ca"],
            display_count=2,
        )
        payload = {
            "comparables": [
                {
                    "formattedAddress": "Old Sample HOA Ln, Sampletown, CA 90000",
                    "squareFootage": 2510,
                    "price": 2000000,
                    "distance": 0.2,
                    "lastSeenDate": "2025-05-15T00:00:00Z",
                },
                {
                    "formattedAddress": "210 Example Ln, Sampletown, CA 90000",
                    "squareFootage": 1990,
                    "price": 1995000,
                    "distance": 0.2,
                    "lastSeenDate": "2026-05-06T00:00:00Z",
                },
            ]
        }
        comps = normalize_comps(home, payload)
        self.assertEqual(comps[0].address, "210 Example Ln, Sampletown, CA 90000")

    def test_local_lookback_filters_old_listing_rows(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 365, 25)
        payload = {
            "comparables": [
                {"formattedAddress": "Recent", "price": 1000000, "lastSeenDate": "2026-05-01T00:00:00Z"},
                {"formattedAddress": "Old", "price": 1000000, "lastSeenDate": "2022-05-01T00:00:00Z"},
            ]
        }
        comps = normalize_comps(home, payload)
        self.assertEqual([comp.address for comp in comps], ["Recent"])

    def test_local_lookback_filters_undated_rows(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 365, 25)
        payload = {
            "avm": {"comparables": [{"formattedAddress": "Recent", "price": 1000000, "lastSeenDate": "2026-05-01T00:00:00Z"}]},
            "properties": [{"formattedAddress": "Undated", "price": 1000000}],
        }
        comps = normalize_comps(home, payload)
        self.assertEqual([comp.address for comp in comps], ["Recent"])

    def test_merges_listing_dates_and_property_history_sale(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 180, 10)
        payload = {
            "avm": {"comparables": [{"id": "852", "formattedAddress": "852 Orchard Ave, Example City, CA 90000", "price": 2580000}]},
            "sale_listings": [
                {
                    "id": "852",
                    "formattedAddress": "852 Orchard Ave, Example City, CA 90000",
                    "status": "Active",
                    "price": 2580000,
                    "listedDate": "2026-04-29T00:00:00.000Z",
                    "lastSeenDate": "2026-05-06T08:48:28.472Z",
                }
            ],
            "properties": [
                {
                    "id": "852",
                    "formattedAddress": "852 Orchard Ave, Example City, CA 90000",
                    "history": {"2026-05-10": {"event": "Sale", "price": 2600000}},
                }
            ],
        }
        comp = normalize_comps(home, payload)[0]
        self.assertEqual(comp.status, "Active")
        self.assertEqual(comp.listed_date, "2026-04-29")
        self.assertEqual(comp.last_seen_date, "2026-05-06")
        self.assertEqual(comp.sold_price, 2600000)

    def test_annotations_exclude_busy_road_from_median(self):
        home = HomeConfig(
            "home",
            "Home",
            "1 Main",
            "Single Family",
            1.5,
            180,
            10,
            comp_annotations={
                "400 busy road ave example city ca 90000": {
                    "tags": ["busy-road"],
                    "note": "On Busy Road Ave.",
                    "exclude_from_median": True,
                }
            },
        )
        payload = {
            "comparables": [
                {"formattedAddress": "400 Busy Road Ave, Example City, CA 90000", "price": 1000000, "squareFootage": 1000},
                {"formattedAddress": "Good Comp, Example City, CA 90000", "price": 2000000, "squareFootage": 1000},
            ]
        }
        comps = normalize_comps(home, payload)
        run = TrackerRun(homes=[HomeRun(home=home, comps=comps)], generated_at="2026-05-06T00:00:00+00:00")
        self.assertEqual(run.homes[0].median_sale_price, 2000000)
        digest = render_digest(run, TrackerState())
        self.assertIn("busy-road", digest["text_body"])

    def test_final_comp_set_uses_top_five_selected_eligible_rows(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 365, 25, display_count=6)
        comps = []
        for index, price in enumerate([100, 200, 300, 400, 500, 600], start=1):
            comps.append(
                normalize_comps(
                    home,
                    {
                        "comparables": [
                            {
                                "formattedAddress": f"{index} Comp St",
                                "price": price,
                                "squareFootage": 100,
                                "distance": index / 10,
                            }
                        ]
                    },
                )[0]
            )
        run = TrackerRun(homes=[HomeRun(home=home, comps=comps)], generated_at="2026-05-06T00:00:00+00:00")
        self.assertEqual([comp.sale_price for comp in run.homes[0].final_comps], [100, 200, 300, 400, 500])
        self.assertEqual(run.homes[0].median_sale_price, 300)

    def test_new_row_recommendation_marks_only_final_comp_rows(self):
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 365, 25, display_count=6)
        comps = [
            normalize_comps(
                home,
                {
                    "comparables": [
                        {
                            "id": str(index),
                            "formattedAddress": f"{index} Comp St",
                            "price": index * 100,
                            "squareFootage": 100,
                            "distance": index / 10,
                        }
                    ]
                },
            )[0]
            for index in range(1, 7)
        ]
        previous = TrackerState(homes={"home": HomeState(seen_comp_ids=[comp.id for comp in comps[:4]])})
        digest = render_digest(TrackerRun([HomeRun(home, comps)], "2026-05-06T00:00:00+00:00"), previous)
        self.assertIn("5 Comp St | 納入最終比較物件", digest["text_body"])
        self.assertIn("6 Comp St | 僅供觀察", digest["text_body"])

    def test_same_hoa_and_hoa_fee_display(self):
        home = HomeConfig(
            "home",
            "Home",
            "200 Example Ln, Sampletown, CA",
            "Townhouse",
            1.5,
            180,
            10,
            same_hoa_addresses=["210 Example Ln, Sampletown, CA 90000"],
        )
        payload = {
            "comparables": [{"formattedAddress": "210 Example Ln, Sampletown, CA 90000", "price": 1995000}],
            "properties": [{"formattedAddress": "210 Example Ln, Sampletown, CA 90000", "hoa": {"fee": 815}}],
        }
        comp = normalize_comps(home, payload)[0]
        self.assertTrue(comp.hoa_match)
        self.assertEqual(comp.hoa_fee, 815)
        digest = render_digest(TrackerRun([HomeRun(home, [comp])], "2026-05-06T00:00:00+00:00"), TrackerState())
        self.assertIn("相同 HOA 是", digest["text_body"])
        self.assertIn("相同 HOA", digest["html_body"])
        self.assertIn("$815", digest["html_body"])

    def test_build_run_uses_three_source_provider_once_per_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = FakeProvider({"avm": sample_payload(), "sale_listings": [], "properties": []})
            run = __import__("run_weekly").build_run(settings(tmpdir), provider)
            self.assertEqual(len(run.homes), 1)
            self.assertEqual(len(provider.calls), 1)

    def test_provider_paginates_listing_searches(self):
        class PagingProvider(RentCastProvider):
            def __init__(self):
                super().__init__("https://api.example.test", api_key="key")
                self.requests = []

            def _get_json(self, path, params):
                self.requests.append((path, dict(params)))
                if params["offset"] == 0:
                    return [{"formattedAddress": f"{index} Page One"} for index in range(500)]
                if params["offset"] == 500:
                    return [{"formattedAddress": "Page Two"}]
                return []

        provider = PagingProvider()
        home = HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 365, 25)
        listings = provider.fetch_sale_listings(home)
        self.assertEqual(len(listings), 501)
        self.assertEqual([request[1]["offset"] for request in provider.requests], [0, 500])
        self.assertTrue(all(request[1]["limit"] == 500 for request in provider.requests))

    def test_preview_writes_files_without_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with redirect_stdout(StringIO()):
                code = run_preview(settings(tmpdir), FakeProvider(sample_payload()))
            self.assertEqual(code, 0)
            self.assertTrue(any((Path(tmpdir) / "previews").glob("*.html")))
            self.assertFalse((Path(tmpdir) / "state.yaml").exists())

    def test_send_updates_state_only_after_successful_email(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = settings(tmpdir)
            with patch("run_weekly.send_email", return_value="failed"):
                with self.assertRaises(SetupError):
                    run_send(cfg, FakeProvider(sample_payload()))
            self.assertFalse((Path(tmpdir) / "state.yaml").exists())
            with patch("run_weekly.send_email", return_value=None):
                with redirect_stdout(StringIO()):
                    self.assertEqual(run_send(cfg, FakeProvider(sample_payload())), 0)
            loaded = load_state(Path(tmpdir) / "state.yaml")
            self.assertTrue(loaded.homes["home"].seen_comp_ids)

    def test_rentcast_auth_and_quota_errors_are_setup_errors(self):
        provider = RentCastProvider("https://api.example.test", api_key="key")
        from urllib.error import HTTPError

        for code in (401, 429):
            with patch("provider.urlopen", side_effect=HTTPError("url", code, "bad", {}, None)):
                with self.assertRaises(SetupError):
                    provider.fetch_home_comps(HomeConfig("home", "Home", "1 Main", "Single Family", 1.5, 180, 10))


if __name__ == "__main__":
    unittest.main()
