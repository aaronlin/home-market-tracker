from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from models import ComparableSale, HomeConfig, HomeRun, RecordedSale, TrackerRun
from render import render_digest
from state import HomeState, TrackerState


class TextContent(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)


class DigestEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.home = HomeConfig("a", "第一間房", "1 Synthetic Way", "Single Family", 1.5, 365, 25, display_count=1)
        self.at = "2026-11-09T16:00:00+00:00"

    def digest(self, rows, home=None):
        return render_digest(TrackerRun([HomeRun(home or self.home, rows)], self.at), TrackerState())

    def sale(self, ident="sale", day="2026-10-15", price=1800000, **kwargs):
        return ComparableSale(ident, f"{ident} Synthetic Road", sold_price=price,
                              recorded_sale=RecordedSale(price, day, "property.lastSale"), **kwargs)

    def test_separates_source_supported_sales_from_current_asks(self):
        sale = self.sale()
        asking = ComparableSale("ask", "2 Asking Lane", status="Active", listing_price=999999,
                                last_seen_date="2026-11-08", beds=3, baths=2, sqft=1400)
        unconfirmed = ComparableSale("off", "3 Old Asking Lane", status="Inactive", listing_price=888888,
                                     sold_price=888888, removed_date="2026-11-05")
        digest = self.digest([asking, unconfirmed, sale])
        for body in (digest['text_body'], digest['html_body']):
            self.assertIn("成交價 $1,800,000", body)
            self.assertIn("成交日期 2026-10-15", body)
            self.assertIn("開價 $999,999", body)
            self.assertNotIn("$888,888", body)
            self.assertIn("365 天內成交紀錄 1 筆", body)
            self.assertNotIn("中位數", body)
            self.assertNotIn("相較上次", body)
        self.assertNotIn("新增物件", digest['subject'])

    def test_inactive_missing_old_or_future_observations_are_not_current_inventory(self):
        rows = [ComparableSale(str(i), f"{i} Excluded Place", status=status, listing_price=999000,
                               last_seen_date=seen) for i, (status, seen) in enumerate([
                                   ("Inactive", "2026-11-09"), ("Active", None),
                                   ("Active", "2026-10-01"), ("Active", "2026-11-10")])]
        digest = self.digest(rows)
        for body in (digest['text_body'], digest['html_body']):
            self.assertIn("待售開價 0 筆", body)
            self.assertIn("不代表附近沒有待售房屋", body)
            self.assertIn("另有 3 筆", body)
            self.assertNotIn("$999,000", body)

    def test_seven_day_observation_boundary_and_zero_price(self):
        rows = [ComparableSale("boundary", "7 Day Road", status="Active", listing_price=900000, last_seen_date="2026-11-02"),
                ComparableSale("old", "8 Day Road", status="Active", listing_price=800000, last_seen_date="2026-11-01"),
                ComparableSale("zero", "Zero Road", status="Active", listing_price=0, last_seen_date="2026-11-09")]
        body = self.digest(rows)['text_body']
        self.assertIn("待售開價 1 筆", body)
        self.assertIn("7 Day Road", body)
        self.assertNotIn("8 Day Road", body)
        self.assertNotIn("Zero Road", body)

    def test_adaptive_sale_window_and_summary_counts_precede_display_limit(self):
        rows = [self.sale("recent"), self.sale("older", "2026-07-01"), self.sale("oldest", "2026-06-01"), self.sale("fourth", "2026-05-20")]
        body = self.digest(rows)['text_body']
        self.assertIn("180 天內成交紀錄 4 筆", body)
        self.assertIn("已擴大回溯期間", body)
        self.assertIn("列出最近 3 筆，共 4 筆", body)
        self.assertNotIn("fourth Synthetic Road", body)

    def test_sale_window_does_not_overstate_configured_acquisition(self):
        home = replace(self.home, days_old=120)
        body = self.digest([self.sale("eligible", "2026-07-15"), self.sale("too-old", "2026-06-01")], home)['text_body']
        self.assertIn("120 天內成交紀錄 1 筆", body)
        self.assertNotIn("too-old Synthetic Road", body)

    def test_future_or_invalid_sales_are_not_displayed(self):
        body = self.digest([self.sale("future", "2026-11-10"), self.sale("invalid", "2026-19-50")])['text_body']
        self.assertIn("成交紀錄 0 筆", body)
        self.assertIn("目前無法據此判斷房屋價值", body)

    def test_annotations_remain_visible_without_aggregating(self):
        sale = self.sale(exclude_from_median=True, notes=["道路噪音待留意"], hoa_match=True)
        for body in self.digest([sale]).values():
            if 'Synthetic Road' in body:
                self.assertIn("道路噪音待留意", body)
                self.assertIn("符合已設定的同社區提示", body)
                self.assertIn("本期僅逐筆列示", body)

    def test_no_change_baseline_is_invented_from_old_state(self):
        run = TrackerRun([HomeRun(self.home, [self.sale()])], self.at)
        a = render_digest(run, TrackerState())
        b = render_digest(run, TrackerState(homes={'a': HomeState(median_sale_price=1, seen_comp_ids=[])}))
        self.assertEqual(a, b)
        self.assertIn("變化追蹤尚未啟用", a['text_body'])
        self.assertNotIn("+0.0%", a['text_body'])

    def test_html_escapes_private_content_and_matches_text_facts(self):
        home = replace(self.home, name='A & <north>', address='1 Long & <Street>')
        row = self.sale(ident='A & <Annex>', notes=['原始備註 & <內容>'])
        digest = self.digest([row], home)
        self.assertIn('A &amp; &lt;north&gt;', digest['html_body'])
        self.assertNotIn('<Annex>', digest['html_body'])
        parser = TextContent()
        parser.feed(digest['html_body'])
        visible = ' '.join(parser.text)
        for fact in ['成交價 $1,800,000', '成交日期 2026-10-15', '原始備註 & <內容>', '365 天內成交紀錄 1 筆', '變化追蹤尚未啟用']:
            self.assertIn(fact, digest['text_body'])
            self.assertIn(fact, visible)

    def test_both_home_overviews_precede_the_detailed_rows(self):
        home_b = replace(self.home, id='b', name='第二間房', property_type='Townhouse')
        run = TrackerRun([HomeRun(self.home, [self.sale()]), HomeRun(home_b, [])], self.at)
        digest = render_digest(run, TrackerState())
        for body in (digest['text_body'], digest['html_body']):
            self.assertLess(body.index('第二間房'), body.index('sale Synthetic Road'))
            self.assertIn('目前無法據此判斷房屋價值', body)
        self.assertIn('2 間房', digest['subject'])


if __name__ == '__main__':
    unittest.main()
