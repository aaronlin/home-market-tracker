from dataclasses import replace
from datetime import datetime, timezone
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from app_config import SetupError
from market_metrics import cohort_for
from models import ComparableSale, HomeConfig, HomeRun, RecordedSale, RetrievalInfo, TrackerRun
from normalize import normalize_candidates, normalize_comps, qualify_comps
from provider import RentCastProvider
from redfin import load_redfin_references, apply_redfin
from render import render_digest
from run_weekly import build_run, run_preview
from state import TrackerState
from test_home_market_tracker import FakeProvider, settings


class SourceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.home = HomeConfig('home', 'Synthetic home', '1 Example St, Sampletown, CA 90000',
                               'Townhouse', 1.5, 365, 25, comp_square_footage='1700:3000',
                               ranked_comp_bedrooms='3:4', ranked_comp_bathrooms='2:3.5')
        self.row = ComparableSale('a', '2 Example St, Sampletown, CA 90000', beds=3, baths=2.5,
                                 sqft=2000, distance_miles=.5,
                                 recorded_sale=RecordedSale(900000, '2026-10-01', 'property.lastSale'),
                                 source_property_types={'property': 'Townhouse'})

    def test_known_condo_cannot_be_reclassified_into_primary_or_medians(self):
        for types in ({'redfin': 'Townhome', 'property': 'Condo'}, {'property': 'Condo'},
                      {'redfin': 'Condo', 'listing': 'Townhouse'}):
            row = replace(self.row, source_property_types=types)
            self.assertEqual(cohort_for(self.home, row), 'excluded_condo')
            self.assertEqual(qualify_comps(self.home, [row]), [])
            self.assertEqual(HomeRun(self.home, [row]).final_comps, [])
        self.assertEqual(cohort_for(self.home, self.row), 'primary')
        self.assertEqual(len(qualify_comps(self.home, [self.row])), 1)

    def test_redfin_correction_precedes_filters_and_rejections_remain_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(tmp)
            config['tracker']['homes'][0].update(property_type='Townhouse', comp_square_footage='1700:3000',
                                                 ranked_comp_bedrooms='3:4', ranked_comp_bathrooms='2:3.5')
            reference = dict(address=self.row.address, matched_address=self.row.address,
                url='https://www.redfin.com/CA/Sampletown/2-Example-St-90000/home/123',
                checked_at='2026-11-10', source='page', source_group='mls_listing',
                values={'sqft': 2000, 'beds': 3, 'baths': 2.5, 'property_type': 'Townhouse'})
            Path(config['tracker']['redfin_reference_path']).write_text(json.dumps({'version': 1, 'records': [reference]}))
            payload = {'properties': [{'formattedAddress': self.row.address, 'propertyType': 'Townhouse',
                        'bedrooms': 2, 'bathrooms': 1, 'squareFootage': 1200, 'distance': .5,
                        'lastSalePrice': 900000, 'lastSaleDate': '2026-10-01'},
                       {'formattedAddress': '3 Example St, Sampletown, CA 90000', 'propertyType': 'Condo',
                        'bedrooms': 3, 'bathrooms': 2.5, 'squareFootage': 2000, 'distance': .5,
                        'lastSalePrice': 800000, 'lastSaleDate': '2026-10-01'},
                       {'formattedAddress': '4 Example St, Sampletown, CA 90000', 'propertyType': 'Townhouse',
                        'bedrooms': 3, 'bathrooms': 2.5, 'squareFootage': 2000, 'distance': 3,
                        'lastSalePrice': 850000, 'lastSaleDate': '2026-10-01'}]}
            run = build_run(config, FakeProvider(payload))
            self.assertEqual([row.address for row in run.homes[0].comps], [self.row.address])
            evidence = json.loads(next(Path(config['tracker']['evidence_root']).glob('*.json')).read_text())
            self.assertEqual(len(evidence['candidates']), 3)
            by_address = {entry['effective']['address']: entry for entry in evidence['candidates']}
            accepted = by_address[self.row.address]
            self.assertEqual(accepted['effective']['source_snapshots']['rentcast']['sqft'], 1200)
            self.assertEqual(accepted['effective']['source_snapshots']['redfin'], reference)
            self.assertEqual(accepted['source_differences']['sqft']['effective'], 2000)
            self.assertIn('excluded_condo', by_address['3 Example St, Sampletown, CA 90000']['exclusion_reasons'])
            self.assertIn('outside_radius', by_address['4 Example St, Sampletown, CA 90000']['exclusion_reasons'])
            self.assertEqual(evidence['rentcast_payload'], payload)
            self.assertFalse(Path(config['tracker']['state_path']).exists())

    def test_reference_failure_happens_before_provider_or_smtp(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = settings(tmp)
            Path(config['tracker']['redfin_reference_path']).write_text('{bad')
            provider = FakeProvider({})
            with patch('run_weekly.send_email', side_effect=AssertionError('no SMTP')):
                with self.assertRaises(SetupError):
                    run_preview(config, provider)
            self.assertEqual(provider.calls, [])
            self.assertFalse(Path(config['tracker']['state_path']).exists())

    def test_third_page_candidates_are_not_lost_and_full_cap_is_partial(self):
        provider = RentCastProvider('https://unused.invalid', api_key='synthetic', max_pages=3)
        with patch.object(provider, '_get_json', side_effect=[[{'id': 'a'}] * 500, [{'id': 'b'}] * 500, [{'id': 'important'}]]) as request:
            data = provider.fetch_properties(self.home)
        self.assertEqual([call.args[1]['offset'] for call in request.call_args_list], [0, 500, 1000])
        self.assertEqual(data['records'][-1]['id'], 'important')
        self.assertTrue(data['searches'][0]['complete'])
        with patch.object(provider, '_get_json', return_value=[{'id': 'x'}] * 500):
            data = provider.fetch_properties(self.home)
        self.assertFalse(data['searches'][0]['complete'])
        self.assertEqual(data['searches'][0]['pages'], 3)

    def test_explicit_discovery_bands_do_not_narrow_property_recovery(self):
        home = replace(self.home, listing_bedrooms='2:4', listing_bathrooms='2:3.5')
        provider = RentCastProvider('https://unused.invalid', api_key='synthetic')
        params = provider._search_params(home)
        self.assertEqual(params['bedrooms'], '2:4')
        self.assertEqual(params['bathrooms'], '2:3.5')
        with patch.object(provider, '_get_json', return_value=[]) as request:
            provider.fetch_properties(home)
        self.assertNotIn('bedrooms', request.call_args.args[1])
        self.assertNotIn('propertyType', request.call_args.args[1])

    def test_summary_zero_match_partial_and_type_exclusion_are_explicit(self):
        rejected = replace(self.row, id='condo', source_property_types={'property': 'Condo'})
        run = TrackerRun([HomeRun(self.home, [self.row], candidates=[self.row, rejected],
                        retrieval=[RetrievalInfo('properties', 1500, 3, False)])], '2026-11-10T22:00:00+00:00')
        digest = render_digest(run, TrackerState())
        for key in ('html_body', 'text_body'):
            body = digest[key]
            summary = body.split('同房型成交紀錄')[0]
            self.assertIn('【部分搜尋】', summary)
            self.assertIn('Redfin 已核對連結 0/2', summary)
            self.assertIn('Condo／房型衝突 1 筆', summary)
            self.assertNotIn('較廣分類參考', body)
            self.assertNotIn('成交紀錄來自 RentCast 房屋資料', body)

    def test_index_only_match_and_time_on_redfin_never_become_dom(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'references.json'
            reference = dict(address=self.row.address, matched_address=self.row.address,
                url='https://www.redfin.com/CA/Sampletown/2-Example-St-90000/home/123',
                checked_at='2026-11-10', source='search_index', source_group='mls_listing',
                values={'sqft': 9999, 'days_on_market': 7},
                field_evidence={'days_on_market': {'source_group': 'mls_listing', 'metric': 'time_on_redfin'}})
            path.write_text(json.dumps({'version': 1, 'records': [reference]}))
            row = replace(self.row, recorded_sale=None, status='Active', listing_price=900000,
                          last_seen_date='2026-11-10', days_on_market=0)
            matched = apply_redfin([row], load_redfin_references(path))[0]
            self.assertEqual(matched.sqft, 2000)
            self.assertEqual(matched.days_on_market, 0)
            run = TrackerRun([HomeRun(self.home, [matched], candidates=[matched])], '2026-11-10T22:00:00Z')
            for key in ('html_body', 'text_body'):
                body = render_digest(run, TrackerState())[key]
                self.assertIn('Redfin 已核對連結 1/1', body)
                self.assertIn('其中 0 筆採用 Redfin 欄位', body)
                self.assertIn('來源 DOM：0 天', body)
                self.assertIn('Time on Redfin：7 天', body)
                self.assertIn('來源日期 未提供', body)
                self.assertIn('搜尋索引（僅導覽）', body)

    def test_known_same_episode_can_keep_rentcast_dom_at_its_own_date(self):
        from models import ListingObservation
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'references.json'
            reference = dict(address=self.row.address, matched_address=self.row.address,
                url='https://www.redfin.com/CA/Sampletown/2-Example-St-90000/home/123',
                checked_at='2026-11-10', source='page', source_group='mls_listing',
                values={'listing_price': 900000, 'status': 'Active', 'observed_at': '2026-11-07', 'listed_date': '2026-10-01'},
                field_evidence={'status': {'source_group': 'mls_listing', 'mls_number': 'MLS1'}})
            path.write_text(json.dumps({'version': 1, 'records': [reference]}))
            row = replace(self.row, recorded_sale=None, status='Active', listing_price=900000,
                          listed_date='2026-10-01', last_seen_date='2026-11-10', days_on_market=40,
                          listing_observations=[ListingObservation(900000,'2026-11-10','2026-10-01',None,'MLS1',40,'listing')])
            matched = apply_redfin([row], load_redfin_references(path))[0]
            self.assertEqual(matched.last_seen_date, '2026-11-07')
            self.assertEqual(matched.days_on_market, 40)
            self.assertEqual(matched.field_sources['days_on_market'], 'rentcast')
            body = render_digest(TrackerRun([HomeRun(self.home,[matched])], '2026-11-10T22:00:00Z'), TrackerState())['text_body']
            self.assertIn('觀察日期 2026-11-10；非成交速度', body)
            self.assertIn('DOM 來源：RentCast', body)

    def test_url_with_wrong_street_or_city_is_not_a_precise_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'references.json'
            reference = dict(address=self.row.address, matched_address=self.row.address,
                checked_at='2026-11-10', source='page')
            for slug in ('CA/Sampletown/3-Example-St-90000/home/123',
                         'CA/Othercity/2-Example-St-90000/home/123',
                         'CA/Sampletown/2-Example-St-90001/home/123'):
                reference['url'] = 'https://www.redfin.com/' + slug
                path.write_text(json.dumps({'version': 1, 'records': [reference]}))
                with self.subTest(slug=slug), self.assertRaises(SetupError):
                    load_redfin_references(path)


if __name__ == '__main__':
    unittest.main()
