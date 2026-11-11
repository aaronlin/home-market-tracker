from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from models import (CaptureBaseline, ComparableSale, HomeConfig, HomeRun, RecordedSale,
                    RegionalHistory, RegionalMonth, RetrievalInfo, TrackerRun)
from render import render_digest
from state import TrackerState


class MarketSummaryRenderTests(unittest.TestCase):
    def setUp(self):
        self.home = HomeConfig('h', 'Synthetic home <North>', '1 Example St, Example City, CA 90000',
                               'Townhouse', 1.5, 365, 25, comparable_property_types=['Townhouse', 'Single Family'])
        self.sfh = ComparableSale('sfh', '2 Example St', source_property_types={'property': 'Single Family'},
                                  status='Active', listing_price=1000000, last_seen_date='2026-11-10', days_on_market=12)
        self.town = replace(self.sfh, id='town', address='3 Example St',
                            source_property_types={'property': 'Townhouse'}, days_on_market=20)
        self.retrieval = [RetrievalInfo('properties', 2, 1, True)]
        self.history = RegionalHistory('90000', '2026-11-10T20:00:00Z', '2026-11-10T00:00:00Z', [
            RegionalMonth('2026-10', 'Townhouse', 750000, 40, 3, 8, True),
            RegionalMonth('2025-10', 'Townhouse', 700000, 30, 5, 12, True),
            RegionalMonth('2026-10', 'Single Family', 1000000, 10, 12, 30, True),
            RegionalMonth('2025-10', 'Single Family', 1100000, 20, 8, 25, True)], 'ok')

    def digest(self, baseline=None, history=True, retrieval=None):
        home_run = HomeRun(self.home, [self.town, self.sfh], retrieval=self.retrieval if retrieval is None else retrieval,
                           regional_history=self.history if history else None, capture_baseline=baseline)
        with patch('urllib.request.urlopen', side_effect=AssertionError('render must not fetch')):
            return render_digest(TrackerRun([home_run], '2026-11-10T20:00:00Z'), TrackerState())

    def test_types_and_scales_have_separate_findable_headlines_in_both_bodies(self):
        for key in ('text_body', 'html_body'):
            body = self.digest()[key]
            self.assertIn('Townhouse 附近固定條件觀察', body)
            self.assertIn('SFH 附近固定條件觀察', body)
            self.assertIn('Townhouse 區域掛牌背景', body)
            self.assertIn('SFH 區域掛牌背景', body)
            self.assertIn('ZIP 90000', body)
            self.assertIn('2026-10 對照 2025-10', body)
            self.assertIn('尚無條件一致的跨期基準', body)
            self.assertIn('有效樣本數未提供', body)
            self.assertIn('不是月底庫存', body)
            self.assertIn('來源更新 2026-11-10', body)
            self.assertIn('擷取 2026-11-10', body)
            self.assertNotIn('房市逐漸回升', body)
        html = self.digest()['html_body']
        self.assertIn('Synthetic home &lt;North&gt;', html)
        self.assertLess(html.index('目前待售來源 DOM'), html.index('附近固定條件觀察'))

    def test_comparable_baseline_reports_count_difference_not_new_sales(self):
        baseline = CaptureBaseline('cohort', 'comparable', '2026-11-03T20:00:00Z',
                                   {'Townhouse': 3, 'Single Family': 2}, {'Townhouse': 1, 'Single Family': 1})
        for key in ('text_body', 'html_body'):
            body = self.digest(baseline)[key]
            self.assertIn('待售由 3 筆變為 1 筆', body)
            self.assertIn('2026-11-03 → 2026-11-10', body)
            self.assertIn('筆數變化不等於新上市或成交', body)
            self.assertNotIn('新增成交', body)

    def test_partial_and_changed_cohort_cannot_claim_inventory_reduction(self):
        baseline = CaptureBaseline('cohort', 'comparable', '2026-11-03T20:00:00Z',
                                   {'Townhouse': 10}, {'Townhouse': 1})
        partial = [RetrievalInfo('properties', 500, 1, False)]
        for key in ('text_body', 'html_body'):
            body = self.digest(baseline, retrieval=partial)[key]
            self.assertIn('本期搜尋不完整', body)
            self.assertNotIn('待售由 10 筆變為 1 筆', body)
            changed = self.digest(replace(baseline, status='changed_cohort'))[key]
            self.assertIn('正在建立新的可比基準', changed)
            self.assertNotIn('待售由 10 筆變為 1 筆', changed)

    def test_regional_failure_keeps_existing_nearby_evidence(self):
        for key in ('text_body', 'html_body'):
            body = self.digest(history=False)[key]
            self.assertIn('區域資料未取得', body)
            self.assertIn('2 Example St', body)
            self.assertIn('3 Example St', body)
            self.assertIn('目前待售來源 DOM', body)
            self.assertNotIn('區域掛牌 DOM：0', body)


if __name__ == '__main__':
    unittest.main()
