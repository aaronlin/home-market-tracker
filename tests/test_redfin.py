from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from app_config import SetupError
from market_metrics import asking_gap
from models import ComparableSale, ListingObservation, RecordedSale
from redfin import address_key, apply_redfin, load_redfin_references


class RedfinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'references.json'
        self.record = {
            'address': '2 S Example St, Apt 14, Sampletown, CA 90000',
            'matched_address': '2 South Example Street #14, Sampletown, CA 90000',
            'url': 'https://www.redfin.com/CA/Sampletown/2-S-Example-St-90000/unit-14/home/123',
            'checked_at': '2026-11-01', 'source': 'page', 'source_group': 'mls_listing',
            'field_evidence': {'hoa_fee': {'billing_period': 'monthly'}}, 'facts': ['A dated source observation.'],
            'values': {'beds': 4, 'sqft': 1710, 'hoa_fee': 0, 'property_type': 'Townhome'},
        }
        self.comp = ComparableSale('stable-id', self.record['address'], beds=3, baths=2, sqft=1310,
            recorded_sale=RecordedSale(900000, '2026-10-01', 'property.lastSale'),
            sold_price=900000, source_property_types={'property': 'Condo'},
            listing_observations=[ListingObservation(1000000, '2026-09-10', '2026-09-01', '2026-09-15', 'MLS1', 10, 'listing')],
            raw={'provider': 'original payload'})

    def load(self, *records):
        self.path.write_text(json.dumps({'version': 1, 'records': list(records or (self.record,))}))
        return load_redfin_references(self.path)

    def test_page_precedence_and_missing_field_fallback_preserve_both_sources(self):
        before = asdict(self.comp)
        reference = deepcopy(self.record)
        with patch('urllib.request.urlopen', side_effect=AssertionError('no fetching')):
            result = apply_redfin([self.comp], self.load(reference))[0]
        self.assertEqual((result.beds, result.sqft, result.baths, result.hoa_fee), (4, 1710, 2, 0))
        self.assertEqual(result.source_property_types, {'property': 'Condo', 'redfin': 'Townhouse'})
        self.assertEqual(result.id, 'stable-id')
        self.assertEqual(result.field_sources['sqft'], 'redfin')
        self.assertEqual(result.field_sources['baths'], 'rentcast')
        self.assertEqual(result.source_snapshots['rentcast']['sqft'], 1310)
        self.assertEqual(result.source_snapshots['redfin'], reference)
        self.assertEqual(asdict(self.comp), before)
        self.assertEqual(result.raw, self.comp.raw)

    def test_same_reference_is_idempotent_without_nested_snapshot_growth(self):
        references = self.load()
        once = apply_redfin([self.comp], references)
        twice = apply_redfin(once, references)
        self.assertEqual(asdict(once[0]), asdict(twice[0]))

    def test_sale_override_does_not_reuse_unrelated_rentcast_episode(self):
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-01', 'source_group': 'mls_history'}
        self.assertIsNotNone(asking_gap(self.comp))
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.recorded_sale.source, 'redfin')
        self.assertEqual(result.sold_price, 950000)
        self.assertEqual(result.listing_observations, [])
        self.assertIsNone(asking_gap(result))
        self.assertEqual(result.source_snapshots['rentcast']['recorded_sale']['price'], 900000)
        self.assertEqual(len(result.source_snapshots['rentcast']['listing_observations']), 1)

    def test_explicit_redfin_sale_episode_can_supply_asking_reference(self):
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-01', 'source_group': 'mls_history'}
        record['listing_observations'] = [{'price': 1000000, 'observed_date': '2026-09-10',
            'listed_date': '2026-09-01', 'removed_date': '2026-09-15', 'mls_number': 'RF1', 'days_on_market': 0, 'metric': 'listing_dom', 'source_group': 'mls_history'}]
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(asking_gap(result).amount, -50000)
        self.assertEqual(asking_gap(result).observation.source, 'redfin')

    def history_record(self):
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 2600000, 'transaction_date': '2026-10-23',
                                   'source_group': 'mls_history', 'mls_number': 'RF1'}
        record['sale_history'] = [
            {'event': 'listed', 'date': '2026-10-01', 'price': 2359000, 'mls_number': 'RF1'},
            {'event': 'pending', 'date': '2026-10-08', 'mls_number': 'RF1'},
            {'event': 'sold', 'date': '2026-10-23', 'price': 2600000, 'mls_number': 'RF1'},
            {'event': 'rental_listed', 'date': '2026-10-28', 'price': 5198},
        ]
        return record

    def test_structured_redfin_history_supplies_gap_without_inventing_dom(self):
        record = self.history_record()
        result = apply_redfin([self.comp], self.load(record))[0]
        gap = asking_gap(result)
        self.assertIsNotNone(gap)
        self.assertEqual(gap.amount, 241000)
        self.assertAlmostEqual(gap.percent, 10.216193302246715)
        self.assertEqual(gap.observation.source, 'redfin')
        self.assertIsNone(gap.observation.days_on_market)
        self.assertEqual(result.source_snapshots['redfin'], record)
        self.assertEqual(asdict(result), asdict(apply_redfin([result], self.load(record))[0]))

    def test_history_uses_latest_priced_change_and_deduplicates_exact_events(self):
        record = self.history_record()
        record['sale_history'].append(deepcopy(record['sale_history'][0]))
        record['sale_history'].append({**record['sale_history'][0], 'price': None})
        record['sale_history'].append({'event': 'price_changed', 'date': '2026-10-05',
                                       'price': 2500000, 'mls_number': 'RF1'})
        record['sale_history'][1]['price'] = 9999999  # Pending price is not an asking-price event.
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(asking_gap(result).amount, 100000)
        self.assertEqual(asking_gap(result).observation.observed_date, '2026-10-05')

    def test_history_gap_survives_later_unresolved_current_listing(self):
        record = self.history_record()
        comp = replace(self.comp, status='Active', last_seen_date='2026-10-29')
        result = apply_redfin([comp], self.load(record))[0]
        self.assertIsNone(result.status)
        self.assertEqual(asking_gap(result).amount, 241000)

    def test_ambiguous_redfin_history_does_not_supply_gap(self):
        modifications = [
            lambda r: r['sale_history'][2].update(mls_number='OTHER'),
            lambda r: r['sale_history'][0].update(source_group='estimate'),
            lambda r: r['sale_history'][2].update(source_group='public_record'),
            lambda r: r['sale_history'][2].update(price=2500000),
            lambda r: r['sale_history'][2].update(date='2026-10-22'),
            lambda r: r['sale_history'].append({'event': 'price_changed', 'date': '2026-10-03', 'price': None, 'mls_number': 'RF1'}),
            lambda r: r['sale_history'].append({'event': 'listed', 'date': '2026-10-09', 'price': 2400000, 'mls_number': 'RF2'}),
            lambda r: r['sale_history'].append({'event': 'delisted', 'date': '2026-10-09', 'mls_number': 'RF1'}),
            lambda r: r['sale_history'].append({'event': 'price_changed', 'date': '2026-10-01', 'price': 2400000, 'mls_number': 'RF1'}),
        ]
        for change in modifications:
            record = self.history_record()
            change(record)
            with self.subTest(history=record['sale_history']):
                result = apply_redfin([self.comp], self.load(record))[0]
                self.assertIsNone(asking_gap(result))

    def test_search_index_values_are_supplemental_not_authoritative(self):
        record = deepcopy(self.record)
        record['source'] = 'search_index'
        record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-01', 'source_group': 'mls_history'}
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.sqft, self.comp.sqft)
        self.assertEqual(result.recorded_sale, self.comp.recorded_sale)
        self.assertEqual(result.listing_observations, self.comp.listing_observations)
        self.assertEqual(result.redfin_url, record['url'])
        self.assertEqual(result.redfin_reference.facts, record['facts'])

    def test_listing_precedence_requires_coherent_bundle_and_keeps_observation_age(self):
        record = deepcopy(self.record)
        record['values'] = {'listing_price': 1100000, 'days_on_market': 0}
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.listing_price, self.comp.listing_price)
        self.assertEqual(result.days_on_market, self.comp.days_on_market)
        record['values'].update(status='Active', observed_at='2026-10-15')
        record['field_evidence']['days_on_market'] = {'metric': 'listing_dom', 'observed_at': '2026-10-15'}
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.last_seen_date, '2026-10-15')
        self.assertNotEqual(result.last_seen_date, record['checked_at'])
        self.assertEqual(result.days_on_market, 0)
        self.assertEqual(result.listing_observations, [])

    def test_conflicting_identity_gets_no_reference_or_override(self):
        comp = replace(self.comp, raw={'_listing_identity_conflict': True})
        self.assertEqual(apply_redfin([comp], self.load()), [comp])

    def test_wrong_unit_number_city_zip_and_url_are_rejected(self):
        changes = [
            {'matched_address': self.record['matched_address'].replace('#14', '#15')},
            {'matched_address': self.record['matched_address'].replace('2 South', '3 South')},
            {'matched_address': self.record['matched_address'].replace('Sampletown', 'Elsewhere')},
            {'matched_address': self.record['matched_address'].replace('90000', '90001')},
            {'matched_address': self.record['matched_address'].replace(' #14', '')},
            {'url': self.record['url'].replace('unit-14', 'unit-15')},
            {'url': self.record['url'].replace('redfin.com', 'redfin.com.evil.test')},
            {'url': self.record['url'] + '?redirect=other'},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(SetupError):
                self.load({**self.record, **change})
        wrong = replace(self.comp, address=self.comp.address.replace('Apt 14', 'Apt 15'))
        self.assertEqual(apply_redfin([wrong], self.load()), [wrong])

    def test_invalid_numbers_dates_and_unknown_schema_fields_are_rejected(self):
        for values in ({'sqft': True}, {'beds': float('nan')}, {'baths': -1}, {'sqft': 0},
                       {'listing_price': -10}, {'made_up': 1},
                       {'observed_at': '2026-11-02'}, {'listed_date': '2026-10-01', 'observed_at': '2026-09-01'}):
            with self.subTest(values=values), self.assertRaises(SetupError):
                self.load({**self.record, 'values': values})
        for sale in ({'price': 5}, {'price': 5, 'transaction_date': '2026-11-02'},
                     {'price': False, 'transaction_date': '2026-01-01'}):
            with self.subTest(sale=sale), self.assertRaises(SetupError):
                self.load({**self.record, 'recorded_sale': sale})

    def test_duplicate_addresses_and_property_id_collisions_are_rejected(self):
        with self.assertRaises(SetupError):
            self.load(self.record, self.record)
        other = {**self.record, 'address': self.record['address'].replace('2 S', '3 S'),
                 'matched_address': self.record['matched_address'].replace('2 South', '3 South')}
        with self.assertRaises(SetupError):
            self.load(self.record, other)

    def test_missing_index_is_normal_but_malformed_present_index_is_actionable(self):
        self.assertEqual(load_redfin_references(self.path), {})
        for content in ('{', '{}', '{"version": true, "records": []}', '{"version": 1, "records": {}}'):
            self.path.write_text(content)
            with self.assertRaisesRegex(SetupError, 'Invalid Redfin reference index'):
                load_redfin_references(self.path)

    def test_non_mls_groups_and_unidentified_page_fields_cannot_override(self):
        for group in ('public_record', 'estimate', 'unknown'):
            with self.subTest(group=group):
                record = deepcopy(self.record)
                record['source_group'] = group
                record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-01', 'source_group': group}
                result = apply_redfin([self.comp], self.load(record))[0]
                self.assertEqual(result.sqft, self.comp.sqft)
                self.assertEqual(result.recorded_sale, self.comp.recorded_sale)
                self.assertEqual(result.source_snapshots['redfin']['source_group'], group)
        record = deepcopy(self.record)
        record['field_evidence']['sqft'] = {'source_group': 'public_record', 'source_label': 'County public facts', 'unit': 'sqft'}
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.sqft, self.comp.sqft)
        self.assertEqual(result.beds, 4)

    def test_hoa_requires_explicit_monthly_period_and_keeps_other_units(self):
        comp = replace(self.comp, hoa_fee=300)
        for period in (None, 'quarterly', 'annual'):
            record = deepcopy(self.record)
            record['values']['hoa_fee'] = 900
            record['field_evidence']['hoa_fee'] = {'unit': 'USD', 'source_label': 'MLS HOA fee'}
            if period: record['field_evidence']['hoa_fee']['billing_period'] = period
            result = apply_redfin([comp], self.load(record))[0]
            self.assertEqual(result.hoa_fee, 300)
            self.assertEqual(result.source_snapshots['redfin']['field_evidence']['hoa_fee'], record['field_evidence']['hoa_fee'])

    def test_generic_days_and_time_on_redfin_never_become_dom(self):
        for value in (0, 10, -1, 'unknown', True, 1.5):
            for metric in (None, 'time_on_redfin'):
                record = deepcopy(self.record)
                record['values'].update(listing_price=1100000, status='Active', observed_at='2026-10-15', days_on_market=value)
                record['field_evidence']['days_on_market'] = {'observed_at': '2026-10-15'}
                if metric: record['field_evidence']['days_on_market']['metric'] = metric
                result = apply_redfin([self.comp], self.load(record))[0]
                self.assertIsNone(result.days_on_market)
                self.assertEqual(result.source_snapshots['redfin']['values']['days_on_market'], value)
        record['values']['days_on_market'] = 10
        record['field_evidence']['days_on_market'].update(metric='cumulative_dom', observed_at='2026-10-14')
        self.assertIsNone(apply_redfin([self.comp], self.load(record))[0].days_on_market)

    def test_older_redfin_sale_does_not_replace_newer_unconfirmed_rentcast_sale(self):
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 800000, 'transaction_date': '2026-09-01', 'source_group': 'mls_history'}
        comp = replace(self.comp, listing_price=1000000, market_price=1200000)
        result = apply_redfin([comp], self.load(record))[0]
        self.assertIsNone(result.recorded_sale)
        self.assertIsNone(result.sold_price)
        self.assertIsNone(result.market_price)
        self.assertIsNone(result.sale_price)
        self.assertIsNone(result.sale_date)
        self.assertIn('recorded_sale', result.unresolved_fields)
        self.assertEqual(result.source_snapshots['rentcast']['recorded_sale']['price'], 900000)
        self.assertEqual(result.source_snapshots['redfin']['recorded_sale']['price'], 800000)
        self.assertEqual(asdict(result), asdict(apply_redfin([result], self.load(record))[0]))

    def test_same_date_sale_price_conflict_uses_redfin_mls_history(self):
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-01', 'source_group': 'mls_history'}
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.recorded_sale.price, 950000)
        self.assertEqual(result.sale_date, '2026-10-01')
        self.assertNotIn('recorded_sale', result.unresolved_fields)
        self.assertTrue(any('Same-date' in note for note in result.reconciliation_notes))

    def test_distinct_sale_events_keep_both_dates_without_treating_prices_as_same_event(self):
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-15', 'source_group': 'mls_history'}
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.recorded_sale.transaction_date, '2026-10-15')
        self.assertEqual(result.source_snapshots['rentcast']['recorded_sale']['transaction_date'], '2026-10-01')
        self.assertTrue(any('different dated transactions' in note for note in result.reconciliation_notes))
        self.assertFalse(any('prices disagree' in note for note in result.reconciliation_notes))

    def test_newer_rentcast_or_same_day_conflicting_status_is_withheld(self):
        for rc_status, rc_date in (('Sold', '2026-10-25'), ('Pending', '2026-10-25'), ('Pending', '2026-10-15')):
            record = deepcopy(self.record)
            record['values'].update(status='Active', listing_price=1000000, observed_at='2026-10-15')
            comp = replace(self.comp, status=rc_status, last_seen_date=rc_date, listing_price=1200000)
            result = apply_redfin([comp], self.load(record))[0]
            self.assertIsNone(result.status)
            self.assertIsNone(result.listing_price)
            self.assertIsNone(result.last_seen_date)
            self.assertIn('status', result.unresolved_fields)
            self.assertEqual(result.source_snapshots['rentcast']['status'], rc_status)
            self.assertEqual(result.source_snapshots['redfin']['values']['status'], 'Active')

    def test_rentcast_sale_after_older_redfin_pending_is_unconfirmed_not_closed(self):
        record = deepcopy(self.record)
        record['values'].update(status='Pending', listing_price=1000000, observed_at='2026-09-15')
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertIsNone(result.status)
        self.assertIsNone(result.recorded_sale)
        self.assertIsNone(result.sale_price)
        self.assertIn('recorded_sale', result.unresolved_fields)

    def test_later_rentcast_relist_does_not_erase_historical_redfin_sale(self):
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-01', 'source_group': 'mls_history'}
        comp = replace(self.comp, status='Active', last_seen_date='2026-10-15', listing_price=1100000)
        result = apply_redfin([comp], self.load(record))[0]
        self.assertEqual(result.recorded_sale.price, 950000)
        self.assertIsNone(result.status)
        self.assertIsNone(result.listing_price)
        self.assertIn('status', result.unresolved_fields)
        self.assertNotIn('recorded_sale', result.unresolved_fields)

    def test_same_day_new_mls_status_and_sale_episode_conflicts_are_unresolved(self):
        record = deepcopy(self.record)
        record['values'].update(status='Active', listing_price=1000000, observed_at='2026-10-15')
        record['field_evidence']['status'] = {'mls_number': 'NEW1'}
        comp = replace(self.comp, status='Active', last_seen_date='2026-10-15',
            listing_observations=[ListingObservation(1100000, '2026-10-15', '2026-10-01', '2026-10-20', 'OLD1', 10, 'listing')])
        result = apply_redfin([comp], self.load(record))[0]
        self.assertIn('status', result.unresolved_fields)
        self.assertIsNone(result.listing_price)
        record = deepcopy(self.record)
        record['recorded_sale'] = {'price': 950000, 'transaction_date': '2026-10-01', 'source_group': 'mls_history', 'mls_number': 'NEW1'}
        comp = replace(self.comp, raw={'_sale_record': {'mlsNumber': 'OLD1'}}, price_per_sqft=600)
        result = apply_redfin([comp], self.load(record))[0]
        self.assertIn('recorded_sale', result.unresolved_fields)
        self.assertIsNone(result.price_per_sqft)

    def test_later_identical_same_mls_observation_is_not_a_new_event(self):
        record = deepcopy(self.record)
        record['values'].update(status='Active', listing_price=1000000, observed_at='2026-10-15', listed_date='2026-10-01')
        record['field_evidence']['status'] = {'mls_number': 'MLS-SAME'}
        comp = replace(self.comp, status='Active', last_seen_date='2026-10-25', listing_price=1000000, listed_date='2026-10-01',
            listing_observations=[ListingObservation(1000000, '2026-10-25', '2026-10-01', '2026-10-30', 'MLS-SAME', 24, 'listing')])
        result = apply_redfin([comp], self.load(record))[0]
        self.assertEqual(result.status, 'Active')
        self.assertEqual(result.listing_price, 1000000)
        self.assertEqual(result.last_seen_date, '2026-10-15')
        self.assertNotIn('status', result.unresolved_fields)
        self.assertEqual(result.source_snapshots['rentcast']['last_seen_date'], '2026-10-25')
        self.assertEqual(result.source_snapshots['redfin']['values']['observed_at'], '2026-10-15')
        self.assertTrue(any('same MLS episode' in note for note in result.reconciliation_notes))
        self.assertEqual(asdict(result), asdict(apply_redfin([result], self.load(record))[0]))

    def test_unknown_start_never_proves_same_episode_or_retains_foreign_dom(self):
        for missing in ('redfin', 'rentcast', 'observation'):
            with self.subTest(missing=missing):
                record = deepcopy(self.record)
                record['values'].update(status='Active', listing_price=1000000,
                                        observed_at='2026-10-15', listed_date='2026-10-01')
                record['field_evidence']['status'] = {'mls_number': 'MLS-SAME'}
                observation = ListingObservation(1000000, '2026-10-25', '2026-10-01', None,
                                                 'MLS-SAME', 24, 'listing')
                comp = replace(self.comp, status='Active', last_seen_date='2026-10-25',
                               listing_price=1000000, listed_date='2026-10-01', days_on_market=24,
                               listing_observations=[observation])
                if missing == 'redfin': record['values'].pop('listed_date')
                if missing == 'rentcast': comp = replace(comp, listed_date=None)
                if missing == 'observation': comp = replace(comp, listing_observations=[replace(observation, listed_date='')])
                result = apply_redfin([comp], self.load(record))[0]
                self.assertIn('status', result.unresolved_fields)
                self.assertIsNone(result.days_on_market)
                self.assertEqual(result.source_snapshots['rentcast']['days_on_market'], 24)
                # Even a newer Redfin observation cannot retain foreign DOM without the missing proof.
                record['values']['observed_at'] = '2026-10-26'
                result = apply_redfin([comp], self.load(record))[0]
                self.assertIsNone(result.days_on_market)

    def test_later_observation_with_changed_or_unknown_episode_stays_unresolved(self):
        record = deepcopy(self.record)
        record['values'].update(status='Active', listing_price=1000000, observed_at='2026-10-15', listed_date='2026-10-01')
        record['field_evidence']['status'] = {'mls_number': 'MLS-SAME'}
        base = replace(self.comp, status='Active', last_seen_date='2026-10-25', listing_price=1000000, listed_date='2026-10-01',
            listing_observations=[ListingObservation(1000000, '2026-10-25', '2026-10-01', '2026-10-30', 'MLS-SAME', 24, 'listing')])
        cases = [replace(base, listing_observations=[replace(base.listing_observations[0], mls_number='MLS-DIFFERENT')]),
                 replace(base, listing_price=1100000, listing_observations=[replace(base.listing_observations[0], price=1100000)]),
                 replace(base, listing_observations=[replace(base.listing_observations[0], mls_number=None)]),
                 replace(base, listing_observations=[]),
                 replace(base, listed_date='2026-10-02'),
                 replace(base, status='Pending'),
                 replace(base, recorded_sale=RecordedSale(1000000, '2026-10-20', 'property.lastSale'))]
        for comp in cases:
            with self.subTest(status=comp.status, price=comp.listing_price, listed_date=comp.listed_date):
                result = apply_redfin([comp], self.load(record))[0]
                self.assertIn('status', result.unresolved_fields)
                self.assertIsNone(result.status)
                self.assertIsNone(result.listing_price)

    def test_dated_pending_without_price_is_a_status_not_a_close_or_price(self):
        record = deepcopy(self.record)
        record['values'] = {'status': 'Pending', 'observed_at': '2026-10-15'}
        comp = replace(self.comp, recorded_sale=None, status='Active', listing_price=1100000, last_seen_date='2026-10-10')
        result = apply_redfin([comp], self.load(record))[0]
        self.assertEqual(result.status, 'Pending')
        self.assertEqual(result.last_seen_date, '2026-10-15')
        self.assertIsNone(result.recorded_sale)
        self.assertIsNone(result.listing_price)

    def test_ownership_and_evidence_metadata_are_retained(self):
        record = deepcopy(self.record)
        record['ownership'] = 'condominium'
        record['field_evidence']['beds'] = {'source_group': 'mls_listing', 'source_label': 'MLS listing facts', 'event_date': '2026-10-01', 'unit': 'rooms'}
        result = apply_redfin([self.comp], self.load(record))[0]
        self.assertEqual(result.redfin_reference.ownership, 'condominium')
        self.assertEqual(result.redfin_reference.field_evidence['beds'], record['field_evidence']['beds'])
        for change in ({'ownership': 'pretend'}, {'source_group': 'public'},
                       {'field_evidence': {'sqft': {'source_group': 'pretend'}}}):
            with self.assertRaises(SetupError): self.load({**record, **change})

    def test_postal_abbreviations_and_unit_markers_match_without_losing_tokens(self):
        self.assertEqual(address_key(self.record['address']), address_key(self.record['matched_address']))
        self.assertEqual(address_key('392 E Mc Kinley Ave, Sampletown, CA 90000'),
                         address_key('392 East Mckinley Avenue, Sampletown, CA 90000'))
        self.assertNotEqual(address_key('2 N Example St, Sampletown, CA 90000'),
                            address_key('2 S Example St, Sampletown, CA 90000'))


if __name__ == '__main__':
    unittest.main()
