from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from app_config import SetupError
from market_metrics import cohort_for
from models import ComparableSale, HomeConfig, HomeRun, RecordedSale
from normalize import qualify_comps
from provider import RentCastProvider
from run_weekly import _home_configs


class ComparableTypesTests(unittest.TestCase):
    def config(self, value=None, provided=True):
        home = dict(id='sample', name='Synthetic home', address='1 Example St', property_type='Townhouse')
        if provided:
            home['comparable_property_types'] = value
        return {'tracker': {'homes': [home]}}

    def home(self):
        return _home_configs(self.config(['Townhouse', 'Single Family']))[0]

    def comp(self, kind, price=1000000):
        return ComparableSale(kind, '2 Example St', sqft=2200, beds=3, baths=2.5,
                              distance_miles=0.5, source_property_types={'property': kind},
                              recorded_sale=RecordedSale(price, '2026-10-15', 'property.lastSale'),
                              sold_price=price)

    def test_config_preserves_canonical_deduplicated_types(self):
        home = _home_configs(self.config(['Townhouse', 'Single Family', 'Townhouse']))[0]
        self.assertEqual(home.comparable_property_types, ['Townhouse', 'Single Family'])
        self.assertEqual(home.property_type, 'Townhouse')
        self.assertEqual(_home_configs(self.config(['Single Family']))[0].comparable_property_types,
                         ['Townhouse', 'Single Family'])
        self.assertEqual(_home_configs(self.config(provided=False))[0].comparable_property_types, [])

    def test_config_rejects_empty_invalid_and_non_list_types(self):
        for value in ([], None, 'Townhouse', ['Condo'], ['Single Family', 3], ['townhouse'], {}):
            with self.subTest(value=value), self.assertRaises(SetupError):
                _home_configs(self.config(value))

    def test_secondary_sfh_passes_but_condo_and_hard_bounds_do_not(self):
        home = replace(self.home(), ranked_comp_bedrooms='3:4', ranked_comp_square_footage='1700:3000')
        town, sfh, condo = (self.comp(kind) for kind in ('Townhouse', 'Single Family', 'Condo'))
        self.assertEqual([cohort_for(home, row) for row in (town, sfh, condo)], ['primary', 'secondary', 'excluded_condo'])
        with patch('normalize.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 10, tzinfo=timezone.utc)
            qualified = qualify_comps(home, [town, sfh, condo, replace(sfh, id='small', sqft=1300),
                                             replace(sfh, id='far', distance_miles=2),
                                             replace(sfh, id='beds', beds=2)])
        self.assertEqual({row.id for row in qualified}, {'Townhouse', 'Single Family'})
        conflict = replace(sfh, source_property_types={'redfin': 'Single Family', 'property': 'Condo'})
        self.assertEqual(cohort_for(home, conflict), 'excluded_condo')
        default = replace(home, comparable_property_types=[])
        self.assertEqual(cohort_for(default, sfh), 'other')

    def test_independent_type_status_pagination_and_metadata(self):
        provider = RentCastProvider('https://unused.invalid', api_key='offline', max_pages=2)
        def response(path, params):
            if params['propertyType'] == 'Townhouse' and params['status'] == 'Inactive':
                return [{'id': f"{params['offset']}-{i}"} for i in range(500)]
            return [{'id': params['propertyType'] + params['status']}]
        with patch.object(provider, '_get_json', side_effect=response) as request:
            result = provider.fetch_sale_listings(self.home())
        self.assertEqual(request.call_count, 5)
        metadata = {(m['filters']['propertyType'], m['filters']['status']): m for m in result['searches']}
        self.assertEqual(len(metadata), 4)
        self.assertFalse(metadata['Townhouse', 'Inactive']['complete'])
        self.assertEqual(metadata['Townhouse', 'Inactive']['pages'], 2)
        self.assertTrue(all(m['complete'] for k, m in metadata.items() if k != ('Townhouse', 'Inactive')))
        with patch.object(provider, '_get_json', return_value={}) as request:
            provider.fetch_home_comps(self.home())
        self.assertEqual(request.call_args.args[1]['propertyType'], 'Townhouse')
        with patch.object(provider, '_get_json', return_value=[]) as request:
            provider.fetch_properties(self.home())
        self.assertEqual(request.call_count, 1)

    def test_primary_only_medians_ignore_secondary_even_when_display_cap_is_small(self):
        home = replace(self.home(), display_count=1)
        secondary = self.comp('Single Family', 5000000)
        primary = self.comp('Townhouse', 1000000)
        run = HomeRun(home, [secondary, primary])
        self.assertEqual(run.final_comps, [primary])
        self.assertEqual(run.median_sale_price, 1000000)
        self.assertEqual(run.median_price_per_sqft, None)
        self.assertEqual(HomeRun(home, [secondary]).final_comps, [])
        self.assertIsNone(HomeRun(home, [secondary]).median_sale_price)
