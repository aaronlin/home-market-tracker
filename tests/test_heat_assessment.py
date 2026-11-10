from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from models import RegionalHistory, RegionalMonth
from heat import assess_regional


class RegionalAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 20, tzinfo=timezone.utc)
        self.current = RegionalMonth('2026-10', 'Single Family', 1000000, 20, 8, 32, True)
        self.previous = RegionalMonth('2025-10', 'Single Family', 900000, 30, 10, 40, True)
        self.history = RegionalHistory('90000', '2026-11-10T20:00:00Z', '2026-11-10T00:00:00Z',
                                       [self.current, self.previous], 'ok')

    def test_complete_same_type_yoy_reports_facts_without_claiming_recovery(self):
        result = assess_regional(self.history, 'Single Family', self.now)
        self.assertEqual(result.status, 'descriptive_change')
        self.assertIn('縮短', result.headline)
        self.assertEqual(result.period, '2026-10')
        self.assertEqual(result.comparison_period, '2025-10')
        self.assertEqual(len(result.evidence), 3)
        body = ' '.join((result.headline, *result.evidence, result.sufficiency, *result.limitations))
        self.assertIn('20 天', body)
        self.assertIn('30 天', body)
        self.assertIn('當月曾待售', body)
        self.assertIn('有效樣本數未提供', body)
        self.assertIn('不是月底庫存', body)
        self.assertNotIn('房市回升', body)
        self.assertNotIn('成交價中位數', body)

    def test_current_month_and_other_property_types_do_not_replace_expected_month(self):
        wrong = [RegionalMonth('2026-11', 'Single Family', 2000000, 1, 100, 200, True),
                 RegionalMonth('2026-10', 'Townhouse', 500000, 1, 40, 50, True), self.previous]
        result = assess_regional(replace(self.history, months=wrong), 'Single Family', self.now)
        self.assertEqual(result.status, 'insufficient')
        self.assertIn('最近完成月', result.headline)
        self.assertEqual(result.evidence, ())

    def test_missing_yoy_does_not_substitute_zero_or_other_month(self):
        result = assess_regional(replace(self.history, months=[self.current,
            replace(self.previous, month='2025-09')]), 'Single Family', self.now)
        self.assertEqual(result.status, 'insufficient')
        self.assertIn('去年同月', result.headline)
        self.assertTrue(all('去年同月未提供' in line for line in result.evidence))
        self.assertNotIn('去年同月 0', ' '.join(result.evidence))

    def test_partial_cached_month_cannot_become_complete_after_clock_rollover(self):
        history = replace(self.history, months=[replace(self.current, month='2026-11', complete=False),
                                                 replace(self.previous, month='2025-11')])
        result = assess_regional(history, 'Single Family', self.now.replace(month=12))
        self.assertEqual(result.status, 'insufficient')
        self.assertEqual(result.evidence, ())

    def test_stale_or_unavailable_data_never_generates_current_comparison(self):
        for status in ['stale', 'unavailable']:
            result = assess_regional(replace(self.history, status=status), 'Single Family', self.now)
            self.assertEqual(result.status, 'stale_or_partial' if status == 'stale' else 'unavailable')
            self.assertEqual(result.evidence, ())
        self.assertEqual(assess_regional(None, 'Townhouse', self.now).status, 'unavailable')

    def test_zero_values_stay_valid_and_missing_metrics_stay_unknown(self):
        current = replace(self.current, median_dom=0, new_listings=0, total_listings=0,
                          median_asking_price=None)
        result = assess_regional(replace(self.history, months=[current, self.previous]), 'Single Family', self.now)
        self.assertEqual(result.status, 'descriptive_change')
        self.assertIn('0 天', result.evidence[0])
        self.assertIn('新增掛牌：0 筆', result.evidence[1])
        self.assertIn('開價中位數：未提供', result.evidence[2])
        self.assertNotIn('$0', result.evidence[2])

    def test_no_shared_metrics_does_not_claim_a_meaningful_comparison(self):
        current = RegionalMonth('2026-10', 'Single Family', complete=True)
        result = assess_regional(replace(self.history, months=[current, self.previous]), 'Single Family', self.now)
        self.assertEqual(result.status, 'insufficient')
        self.assertIn('可比指標不足', result.headline)


if __name__ == '__main__':
    unittest.main()
