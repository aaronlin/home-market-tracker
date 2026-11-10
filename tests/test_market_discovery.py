from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from models import HomeConfig
from provider import ProviderError, RentCastProvider
from run_weekly import build_run, run_preview
from test_home_market_tracker import settings


class MarketDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.home = HomeConfig('a', 'Synthetic townhouse', '1 Synthetic St', 'Townhouse', 1.5, 365, 25,
                               bedrooms='3', bathrooms='2.5', square_footage='2200', lot_size='1800',
                               year_built='1990', comp_square_footage='1600:2800')
        self.provider = RentCastProvider('https://unused.invalid', api_key='offline-test-key')

    def test_discovery_does_not_turn_subject_attributes_into_comp_constraints(self):
        with patch.object(self.provider, '_get_json', return_value=[]) as request:
            self.provider.fetch_properties(self.home)
            params = request.call_args.args[1]
            self.assertEqual(set(params), {'address', 'radius', 'saleDateRange', 'limit', 'offset'})
        params = self.provider._search_params(self.home)
        for key in ('bedrooms', 'bathrooms', 'yearBuilt', 'lotSize'):
            self.assertNotIn(key, params)
        self.assertEqual(params['squareFootage'], '1600:2800')
        self.assertEqual(params['propertyType'], 'Townhouse')

    def test_explicit_comparable_lot_bounds_are_preserved(self):
        from dataclasses import replace
        home = replace(self.home, comp_lot_size='1000:4000')
        self.assertEqual(self.provider._search_params(home)['lotSize'], '1000:4000')

    def test_active_and_inactive_searches_are_explicit_and_independent(self):
        with patch.object(self.provider, '_get_json', side_effect=lambda path, params: [{'id': params['status']}]) as request:
            response = self.provider.fetch_sale_listings(self.home)
        self.assertEqual([r['id'] for r in response['records']], ['Inactive', 'Active'])
        self.assertEqual([m['source'] for m in response['searches']], ['listings_inactive', 'listings_active'])
        self.assertTrue(all(m['complete'] for m in response['searches']))
        self.assertEqual(request.call_count, 2)
        self.assertNotIn('address', response['searches'][0]['filters'])

    def test_full_last_page_is_incomplete_at_request_budget(self):
        provider = RentCastProvider('https://unused.invalid', api_key='offline-test-key', max_pages=2)
        with patch.object(provider, '_get_json', return_value=[{'id': 'x'}] * 500) as request:
            response = provider.fetch_properties(self.home)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(response['searches'][0]['pages'], 2)
        self.assertFalse(response['searches'][0]['complete'])
        self.assertEqual(len(response['records']), 1000)

    def test_malformed_search_response_is_not_reported_as_empty_success(self):
        for malformed in ({"unexpected": []}, ["not a property record"]):
            with self.subTest(malformed=malformed):
                with patch.object(self.provider, '_get_json', return_value=malformed):
                    with self.assertRaises(ProviderError):
                        self.provider.fetch_properties(self.home)

    def test_preview_fetch_failure_is_not_empty_inventory_or_state_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(tmp)
            with patch.object(self.provider, 'fetch_home_market', side_effect=ProviderError('offline failure')):
                with self.assertRaises(ProviderError):
                    with redirect_stdout(StringIO()):
                        run_preview(config, self.provider)
            self.assertFalse(Path(config['tracker']['state_path']).exists())
            self.assertFalse(Path(config['tracker']['preview_root']).exists())

    def test_build_run_retains_complete_empty_source_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(tmp)
            with patch.object(self.provider, '_get_json', return_value=[]):
                run = build_run(config, self.provider)
            self.assertEqual(len(run.homes[0].retrieval), 3)
            self.assertTrue(all(row.complete and row.rows == 0 for row in run.homes[0].retrieval))
            self.assertFalse(Path(config['tracker']['state_path']).exists())
