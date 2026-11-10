from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from market_metrics import asking_gap, cohort_for, source_dom
from models import ComparableSale, HomeConfig, ListingObservation, RecordedSale
from normalize import normalize_comps, normalize_nearby_listings


class MarketMetricsTests(unittest.TestCase):
    def setUp(self):
        self.home = HomeConfig('home', 'Home', '1 Example St', 'Townhouse', 1.5, 3650, 10)
        self.observation = ListingObservation(1000000, '2026-09-10', '2026-08-01', '2026-09-15', 'MLS1', 0, 'listing.history')
        self.comp = ComparableSale('c', '2 Example St', recorded_sale=RecordedSale(900000, '2026-09-20', 'property.lastSale'), listing_observations=[self.observation])

    def test_classification_uses_property_even_when_listing_disagrees(self):
        self.assertEqual(cohort_for(self.home, replace(self.comp, source_property_types={'property': 'Condo', 'listing': 'Townhouse'})), 'condo_context')
        self.assertEqual(cohort_for(self.home, self.comp), 'unknown')
        self.assertEqual(cohort_for(self.home, replace(self.comp, source_property_types={'listing': 'Townhouse'})), 'primary')
        self.assertEqual(cohort_for(self.home, replace(self.comp, source_property_types={'property': 'Single Family'})), 'other')

    def test_dom_preserves_zero_and_rejects_invalid_values(self):
        for value in (0, '0', 12, '12.0'):
            self.assertEqual(source_dom(value), int(float(value)))
        for value in (None, True, False, -1, 1.5, '1.0000000000000001', float('inf'), float('nan'), 'bad'):
            self.assertIsNone(source_dom(value), repr(value))

    def test_dated_reference_preserves_observation(self):
        gap = asking_gap(self.comp)
        self.assertEqual((gap.amount, gap.percent), (-100000, -10))
        self.assertIs(gap.observation, self.observation)
        later = replace(self.observation, observed_date='2026-09-12', price=950000)
        gap = asking_gap(replace(self.comp, listing_observations=[self.observation, later]))
        self.assertIs(gap.observation, later)

    def test_ambiguous_or_invalid_episodes_are_unavailable(self):
        invalid = [replace(self.observation, observed_date='2026-09-21'),
                   replace(self.observation, mls_number=None),
                   replace(self.observation, observed_date='2026-05-01', listed_date='2026-04-01'),
                   replace(self.observation, removed_date=None),
                   replace(self.observation, listed_date='2026-09-11')]
        for observation in invalid:
            self.assertIsNone(asking_gap(replace(self.comp, listing_observations=[observation])))
        conflicts = [replace(self.observation, mls_number='MLS2'),
                     replace(self.observation, price=1100000),
                     replace(self.observation, mls_number='MLS2', listed_date='2026-09-16', observed_date='2026-09-17', removed_date='2026-09-19'),
                     replace(self.observation, mls_number='MLS2', listed_date='2026-09-21', observed_date='2026-09-22', removed_date='2026-09-23')]
        for observation in conflicts:
            self.assertIsNone(asking_gap(replace(self.comp, listing_observations=[self.observation, observation])))
        self.assertIsNone(asking_gap(replace(self.comp, recorded_sale=None)))
        self.assertIsNone(asking_gap(replace(self.comp, raw={'_listing_identity_conflict': True})))

    def listing(self, **changes):
        return dict(formattedAddress='2 Example St', propertyType='Townhouse', price=1000000,
                    listedDate='2026-08-01', lastSeenDate='2026-09-10', removedDate='2026-09-15',
                    mlsNumber='MLS1', daysOnMarket=0, **changes)

    def test_merges_preserve_source_episode_and_safe_metadata(self):
        listing = self.listing()
        listing['owner'] = {'name': 'Private Person'}
        payload = {'sale_listings': {'records': [listing], 'searches': []},
                   'properties': {'records': [dict(formattedAddress='2 Example St', propertyType='Condo', price=5,
                                                  lastSalePrice=900000, lastSaleDate='2026-09-20')]}}
        comp = normalize_comps(self.home, payload)[0]
        self.assertEqual(comp.source_property_types, {'listing': 'Townhouse', 'property': 'Condo'})
        self.assertEqual(comp.listing_observations, [replace(self.observation, source='listing')])
        self.assertEqual(asking_gap(comp).amount, -100000)
        self.assertEqual(comp.days_on_market, 0)
        metadata = json.dumps(comp.raw['_listing_observations'])
        self.assertNotIn('Private Person', metadata)
        json.dumps(comp.raw)

    def test_only_listing_endpoint_supplies_observations(self):
        payload = {'comparables': [self.listing()]}
        self.assertEqual(normalize_comps(self.home, payload)[0].listing_observations, [])

    def test_redundant_current_history_does_not_invalidate_dated_direct_observation(self):
        listing = self.listing()
        listing['history'] = {'2026-08-01': {'event': 'Sale Listing', 'price': 1000000,
                                            'listedDate': '2026-08-01', 'removedDate': '2026-09-15'}}
        payload = {'sale_listings': [listing], 'properties': [dict(formattedAddress='2 Example St', lastSalePrice=900000, lastSaleDate='2026-09-20')]}
        comp = normalize_comps(self.home, payload)[0]
        self.assertEqual(len(comp.listing_observations), 1)
        self.assertEqual(asking_gap(comp).amount, -100000)
        listing['history']['2026-08-01']['mlsNumber'] = 'MLS2'
        comp = normalize_comps(self.home, payload)[0]
        self.assertIsNone(asking_gap(comp))
        listing['history']['2026-08-01']['mlsNumber'] = 'MLS1'
        comp = normalize_comps(self.home, payload)[0]
        self.assertEqual(asking_gap(comp).amount, -100000)
        listing['history']['2026-08-01']['price'] = 1100000
        comp = normalize_comps(self.home, payload)[0]
        self.assertIsNone(asking_gap(comp))

    def test_history_cannot_borrow_unrelated_current_mls_or_created_date(self):
        listing = self.listing()
        listing.update(listedDate='2026-10-01', lastSeenDate='2026-10-02', removedDate=None,
                       history={'old': dict(price=1000000, listedDate='2026-08-01', lastSeenDate='2026-09-10', removedDate='2026-09-15')})
        comp = normalize_comps(self.home, {'sale_listings': [listing]})[0]
        self.assertIsNone(comp.listing_observations[1].mls_number)
        listing.pop('lastSeenDate')
        listing['createdDate'] = '2026-10-02'
        comp = normalize_comps(self.home, {'sale_listings': [listing]})[0]
        self.assertFalse(any(item.observed_date == '2026-10-02' for item in comp.listing_observations))

    def test_redfin_url_safety(self):
        valid = 'https://www.redfin.com/CA/Example/2-Example-St-90000/home/123'
        invalid = ['http://redfin.com/home/123', 'https://redfin.com.evil.test/home/123',
                   'https://user@redfin.com/home/123', 'https://redfin.com:443/home/123',
                   'https://redfin.com/home/123?redirect=evil', 'https://redfin.com/home/123#evil',
                   'https://redfin.com/home/not-numeric', 'https://redfin.com/a/../home/123',
                   'https://redfin.com/home/123/extra']
        for url in [valid] + invalid:
            comp = normalize_comps(self.home, {'sale_listings': [self.listing(listingUrl=url)]})[0]
            self.assertEqual(comp.redfin_url, valid if url == valid else None, url)

    def test_radius_computed_from_subject_and_explicit_bound_retained(self):
        payload = {'avm': {'subjectProperty': {'latitude': 37, 'longitude': -122}},
                   'sale_listings': [self.listing(latitude=38, longitude=-122)]}
        self.assertEqual(normalize_comps(self.home, payload), [])
        payload['sale_listings'][0]['latitude'] = 37.001
        comp = normalize_comps(self.home, payload)[0]
        self.assertGreater(comp.distance_miles, 0)
        self.assertEqual(normalize_comps(replace(self.home, comp_square_footage='1700:3000'), payload), [])
        self.assertTrue(normalize_comps(replace(self.home, bedrooms='10', lot_size='999999'), payload))

    def test_unit_conflict_blocks_pairing(self):
        payload = {'sale_listings': [self.listing(unit='1')], 'properties': [dict(formattedAddress='2 Example St', unit='2', lastSalePrice=900000, lastSaleDate='2026-09-20')]}
        comp = normalize_comps(self.home, payload)[0]
        self.assertIsNone(asking_gap(comp))


if __name__ == '__main__':
    unittest.main()
