from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from provider import RentCastProvider
from regional import load_regional, parse_regional


def payload():
    return {'zipCode': '90000', 'saleData': {'lastUpdatedDate': '2026-11-10T00:00:00Z', 'history': {
        '2025-10': {'dataByPropertyType': [{'propertyType': 'Townhouse', 'medianPrice': 900000,
            'medianDaysOnMarket': 30, 'newListings': 5, 'totalListings': 20}]},
        '2026-10': {'dataByPropertyType': [{'propertyType': 'Townhouse', 'medianPrice': 1000000,
            'medianDaysOnMarket': 0, 'newListings': 0, 'totalListings': 18}]},
        '2026-11': {'dataByPropertyType': [{'propertyType': 'Townhouse', 'medianPrice': 1100000,
            'medianDaysOnMarket': 12, 'newListings': 2, 'totalListings': 10}]}}}}


class Provider:
    def __init__(self): self.calls = []
    def fetch_market_history(self, zipcode, months):
        self.calls.append((zipcode, months))
        return payload()


class RegionalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 11, 10, tzinfo=timezone.utc)

    def test_type_groups_missing_month_and_zero_are_preserved(self):
        data = parse_regional(payload(), '90000', self.now.isoformat(), self.now)
        self.assertEqual(data.status, 'ok')
        self.assertEqual(len(data.months), 3)
        self.assertTrue(data.months[1].complete)
        self.assertEqual((data.months[1].median_dom, data.months[1].new_listings), (0, 0))
        self.assertFalse(data.months[2].complete)
        self.assertFalse(any(m.property_type == 'Single Family' for m in data.months))

    def test_cached_partial_month_never_matures_when_refresh_fails(self):
        provider = Provider()
        load_regional(provider, '90000', self.root, self.now)
        with patch.object(provider, 'fetch_market_history', side_effect=RuntimeError('offline')):
            data = load_regional(provider, '90000', self.root, datetime(2026, 12, 1, tzinfo=timezone.utc))
        self.assertEqual(data.status, 'stale')
        self.assertTrue(data.from_cache)
        self.assertFalse(next(m for m in data.months if m.month == '2026-11').complete)
        self.assertEqual(data.fetched_at, self.now.isoformat())

    def test_monthly_cache_is_shared_by_zip_and_tracks_history_range(self):
        provider = Provider()
        one = load_regional(provider, '90000', self.root, self.now)
        two = load_regional(provider, '90000', self.root, self.now)
        self.assertEqual(provider.calls, [('90000', 36)])
        self.assertFalse(one.from_cache);self.assertTrue(two.from_cache)
        load_regional(provider, '90000', self.root, self.now, history_months=12)
        self.assertEqual(provider.calls[-1], ('90000', 12))
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(list(self.root.glob('90000/responses/*.json'))), 2)

    def test_corrupt_cache_schema_boolean_is_not_trusted(self):
        provider = Provider()
        load_regional(provider, '90000', self.root, self.now)
        path = self.root / '90000/latest-36.json'
        envelope = json.loads(path.read_text());envelope['schema_version'] = True
        path.write_text(json.dumps(envelope))
        load_regional(provider, '90000', self.root, self.now)
        self.assertEqual(len(provider.calls), 2)

    def test_malformed_cache_timestamp_refetches_without_breaking_report(self):
        for value in (None, [], 17):
            with self.subTest(value=value):
                provider = Provider()
                load_regional(provider, '90000', self.root, self.now)
                path = self.root / '90000/latest-36.json'
                envelope = json.loads(path.read_text());envelope['fetched_at'] = value
                path.write_text(json.dumps(envelope))
                result = load_regional(provider, '90000', self.root, self.now)
                self.assertEqual(result.status, 'ok')
                self.assertFalse(result.from_cache)

    def test_wrong_zip_and_future_dates_are_rejected(self):
        for change in (lambda d: d.update(zipCode='90001'),
                       lambda d: d['saleData'].update(lastUpdatedDate='2026-12-01T00:00:00Z')):
            data = payload();change(data)
            with self.assertRaises(ValueError):parse_regional(data, '90000', self.now.isoformat(), self.now)
        with self.assertRaises(ValueError):
            parse_regional(payload(), '90000', '2026-12-01T00:00:00Z', self.now)

    def test_invalid_numbers_become_missing_and_duplicate_group_is_rejected(self):
        data = payload();row=data['saleData']['history']['2026-10']['dataByPropertyType'][0]
        row.update(medianPrice=True, medianDaysOnMarket=float('nan'), newListings=-1,totalListings=3.5)
        month=parse_regional(data, '90000', self.now.isoformat(), self.now).months[1]
        self.assertEqual((month.median_asking_price,month.median_dom,month.new_listings,month.total_listings),
                         (None,None,None,None))
        row['propertyType']='Condo'
        self.assertEqual(len(parse_regional(data,'90000',self.now.isoformat(),self.now).months),2)
        data=payload();groups=data['saleData']['history']['2026-10']['dataByPropertyType'];groups.append(deepcopy(groups[0]))
        with self.assertRaises(ValueError):parse_regional(data,'90000',self.now.isoformat(),self.now)

    def test_optional_provider_or_fetch_failure_is_unavailable(self):
        self.assertEqual(load_regional(object(),'90000',self.root,self.now).issue,'regional_provider_unavailable')
        self.assertEqual(load_regional(Provider(),None,self.root,self.now).issue,'subject_zip_unavailable')
        provider=Provider()
        with patch.object(provider,'fetch_market_history',side_effect=RuntimeError('secret-value')):
            result=load_regional(provider,'90000',self.root,self.now)
        self.assertEqual(result.status,'unavailable')
        self.assertNotIn('secret-value',str(result))

    def test_provider_uses_sale_history_endpoint_once(self):
        provider=RentCastProvider('https://unused.invalid',api_key='synthetic')
        with patch.object(provider,'_get_json',return_value=payload()) as request:
            provider.fetch_market_history('90000',36)
        request.assert_called_once_with('/markets',{'zipCode':'90000','dataType':'Sale','historyRange':36})
