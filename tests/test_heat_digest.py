from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from models import ComparableSale, HomeConfig, HomeRun, ListingObservation, ListingTimeline, RecordedSale, RedfinReference, RetrievalInfo, TrackerRun
from render import render_digest
from state import TrackerState


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.append(dict(attrs)['href'])


class HeatDigestTests(unittest.TestCase):
    def setUp(self):
        self.home = HomeConfig('h', 'Synthetic townhouse', '1 Synthetic St', 'Townhouse', 1.5, 365, 25)
        self.sale = ComparableSale('sale', '2 Synthetic Street #1 & <North>', sqft=1500,
                                  recorded_sale=RecordedSale(900000, '2026-10-01', 'property.lastSale'),
                                  source_property_types={'property': 'Townhouse'},
                                  listing_observations=[ListingObservation(1000000, '2026-09-10', '2026-09-01', '2026-09-15', 'MLS1', 10, 'listing')])
    def digest(self, rows, retrieval=()):
        return render_digest(TrackerRun([HomeRun(self.home, rows, retrieval=list(retrieval))], '2026-11-10T20:00:00+00:00'), TrackerState())

    def test_dual_types_keep_independent_windows_counts_and_subject(self):
        self.home = replace(self.home, comparable_property_types=['Townhouse', 'Single Family'])
        older = replace(self.sale, address='8 Older Townhouse',
                        recorded_sale=RecordedSale(900000, '2026-02-01', 'property.lastSale'))
        sfh = [replace(self.sale, id=f'sfh-{i}', address=f'{20+i} Detached Reference',
                       source_property_types={'property': 'Single Family'}) for i in range(3)]
        condo = replace(self.sale, address='99 Excluded Condo', source_property_types={'property': 'Condo'})
        digest = self.digest([older, *sfh, condo])
        for key in ('text_body', 'html_body'):
            body = digest[key]
            self.assertIn('Townhouse 365 天內成交紀錄 1 筆', body)
            self.assertIn('SFH 90 天內成交紀錄 3 筆', body)
            self.assertIn('Townhouse 成交參考', body)
            self.assertIn('SFH 周邊獨棟市場參考', body)
            self.assertIn('不同房型分開參考，不合併推算房價', body)
            self.assertIn('8 Older Townhouse', body)
            self.assertNotIn('99 Excluded Condo', body)
            self.assertNotIn('同房型成交紀錄', body)
            self.assertNotIn('同房型 365 天', body)
            self.assertIn('追蹤房屋：', body)
            self.assertIn('連棟住宅', body)
        self.assertEqual(digest['html_body'].count('Synthetic townhouse</h2>'), 2)

    def test_dual_types_show_zero_and_active_per_group(self):
        self.home = replace(self.home, comparable_property_types=['Townhouse', 'Single Family'])
        active = replace(self.sale, address='7 Active Detached', recorded_sale=None,
                         source_property_types={'property': 'Single Family'},
                         status='Active', last_seen_date='2026-11-10', listing_price=950000)
        for key in ('text_body', 'html_body'):
            body = self.digest([active])[key]
            self.assertIn('Townhouse 365 天內成交紀錄 0 筆', body)
            self.assertIn('SFH 365 天內成交紀錄 0 筆', body)
            self.assertIn('Townhouse 目前待售開價', body)
            self.assertIn('SFH 目前待售開價', body)
            self.assertIn('7 Active Detached', body)
            self.assertNotIn('同房型成交紀錄', body)
            self.assertNotIn('同房型 365 天', body)
            self.assertIn('零筆符合紀錄不代表周邊沒有成交', body)

    def test_condo_is_excluded_even_when_listing_calls_it_townhouse(self):
        condo = replace(self.sale, source_property_types={'listing': 'Townhouse', 'property': 'Condo'})
        for body in self.digest([condo]).values():
            if '同房型' not in body:
                continue
            self.assertIn('同房型 365 天內成交紀錄 0 筆', body)
            self.assertNotIn('較廣分類參考', body)
            self.assertNotIn('成交價 $900,000', body)
            self.assertIn('不代表周邊一年沒有成交', body)

    def test_unknown_and_unlike_types_do_not_enter_narrow_sale_sample(self):
        unknown = replace(self.sale, source_property_types={})
        unrelated = replace(self.sale, id='other', source_property_types={'property': 'Single Family'})
        body = self.digest([unknown, unrelated])['text_body']
        self.assertIn('同房型 365 天內成交紀錄 0 筆', body)
        self.assertIn('1 筆候選房型未提供', body)
        self.assertNotIn('成交價 $900,000', body)

    def test_dated_reference_is_not_presented_as_final_or_original_ratio(self):
        for body in (self.digest([self.sale])['text_body'], self.digest([self.sale])['html_body']):
            self.assertIn('成交對開價參考落差 -$100,000（-10.0%）', body)
            self.assertIn('開價紀錄日期 2026-09-10', body)
            self.assertIn('非已確認最終／原始開價比', body)
            self.assertIn('來源 DOM：10 天', body)
            self.assertIn('成交單價 $600／sqft', body)
            self.assertNotIn('議價折扣', body)

    def test_last_known_ask_caveat_is_adjacent_to_gap(self):
        row = replace(self.sale, listing_observations=[replace(self.sale.listing_observations[0],
                                                               later_price_unknown=True)])
        for key in ('text_body', 'html_body'):
            body = self.digest([row])[key]
            self.assertIn('最後已知售前開價參考 $1,000,000', body)
            self.assertIn('其後有改價紀錄但金額未公開', body)
            self.assertLess(body.index('成交對開價參考落差'), body.index('其後有改價紀錄'))

    def test_post_sale_asking_price_does_not_become_a_sale_gap(self):
        row = replace(self.sale, listing_observations=[replace(self.sale.listing_observations[0], observed_date='2026-10-10', removed_date='2026-10-20')])
        body = self.digest([row])['text_body']
        self.assertIn('無可核對的售前掛牌參考', body)
        self.assertNotIn('-10.0%', body)

    def test_overview_dom_medians_separate_types_sources_and_exclusions(self):
        self.home = replace(self.home, comparable_property_types=['Townhouse', 'Single Family'])
        active = replace(self.sale, recorded_sale=None, status='Active', last_seen_date='2026-11-10',
                         listing_price=950000, days_on_market=10)
        excluded = replace(active, id='excluded', address='3 Excluded Street', days_on_market=900,
                           exclude_from_median=True)
        sfh = replace(active, id='sfh', address='4 Detached Street', days_on_market=40,
                      source_property_types={'property': 'Single Family'})
        reference = RedfinReference('5 Source Street', '5 Source Street',
            'https://www.redfin.com/CA/Synthetic/5-Source-St-90000/home/123', '2026-11-10', 'page',
            field_evidence={'days_on_market': {'metric': 'cumulative_dom'}})
        cumulative = replace(active, id='cumulative', address='5 Source Street', days_on_market=100,
                             redfin_reference=reference, field_sources={'days_on_market': 'redfin'})
        for key in ('text_body', 'html_body'):
            body = self.digest([active, excluded, sfh, cumulative])[key]
            overview = body.split('變化追蹤尚未啟用')[0]
            self.assertIn('Townhouse 目前待售來源 DOM：RentCast 掛牌 DOM 中位數 10 天（有效 1/3 筆；非成交速度）', overview)
            self.assertIn('Townhouse 目前待售來源 DOM：Redfin 累計 DOM 中位數 100 天（有效 1/3 筆；非成交速度）', overview)
            self.assertIn('SFH 目前待售來源 DOM：RentCast 掛牌 DOM 中位數 40 天（有效 1/1 筆；非成交速度）', overview)
            self.assertNotIn('中位數 55', overview)

    def test_overview_dom_missing_is_explicit_and_sales_do_not_fabricate_dom(self):
        active = replace(self.sale, id='active', address='6 Active Street', recorded_sale=None,
                         status='Active', last_seen_date='2026-11-10', listing_price=950000,
                         days_on_market=None)
        sale = replace(self.sale, listing_observations=[])
        for key in ('text_body', 'html_body'):
            body = self.digest([active, sale])[key]
            self.assertIn('目前待售來源 DOM：未提供（有效 0/1 筆）', body.split('變化追蹤尚未啟用')[0])
            self.assertIn('來源 DOM：未提供；不以掛牌到成交日期推算。', body)

    def test_sale_timeline_is_visible_without_an_asking_gap(self):
        row = replace(self.sale, listing_observations=[], recorded_sale=RecordedSale(900000, '2026-10-23', 'redfin'))
        row = replace(row, listing_timeline=ListingTimeline(
            listed_date='2026-10-01', pending_date='2026-10-08', sold_date='2026-10-23',
            mls_number='SYNTHETIC1', source='redfin'))
        for key in ('text_body', 'html_body'):
            body = self.digest([row])[key]
            self.assertIn('上市至首次 Pending：7 天', body)
            self.assertIn('上市至成交：22 天', body)
            self.assertIn('含簽約後交割期間', body)
            self.assertIn('2026-10-01 → 2026-10-08', body)
            self.assertIn('來源 DOM：未提供；上述日期差不是來源 DOM。', body)
        row = replace(row, listing_timeline=ListingTimeline(
            listed_date='2026-10-01', pending_date=None, sold_date='2026-10-23',
            mls_number='SYNTHETIC1', source='redfin'))
        self.assertIn('上市至首次 Pending：未提供可核對的 Pending 日期。', self.digest([row])['text_body'])

    def test_dom_zero_is_available_while_missing_and_invalid_remain_unknown(self):
        active = replace(self.sale, recorded_sale=None, status='Active', last_seen_date='2026-11-10', listing_price=950000, days_on_market=0)
        missing = replace(active, id='missing', address='3 Synthetic Street', days_on_market=None)
        invalid = replace(active, id='invalid', address='4 Synthetic Street', days_on_market=-1)
        body = self.digest([active, missing, invalid])['text_body']
        self.assertIn('來源 DOM 有效值 1/3 筆', body)
        self.assertIn('來源 DOM：0 天', body)
        self.assertIn('非成交速度', body)
        self.assertNotIn('來源 DOM：-1', body)

    def test_incomplete_empty_and_unknown_retrieval_states_are_distinct(self):
        incomplete = self.digest([], [RetrievalInfo('properties', 500, 1, False, {'saleDateRange': 365}, '2026-11-10T20:00:00Z')])['text_body']
        complete = self.digest([], [RetrievalInfo('properties', 0, 1, True, {'saleDateRange': 365})])['text_body']
        unknown = self.digest([])['text_body']
        self.assertIn('部分已取得資料', incomplete)
        self.assertIn('500 筆原始紀錄', incomplete)
        self.assertIn('分頁已讀完', complete)
        self.assertIn('0 筆原始紀錄', complete)
        self.assertIn('完整性未知', unknown)
        self.assertNotIn('分頁已讀完', unknown)

    def test_search_fallback_is_encoded_honest_and_performs_no_network(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('render must not fetch')):
            digest = self.digest([self.sale])
        parser = Links(); parser.feed(digest['html_body'])
        self.assertEqual(len(parser.hrefs), 1)
        target = urlsplit(parser.hrefs[0])
        self.assertEqual(target.hostname, 'www.google.com')
        self.assertEqual(parse_qs(target.query)['q'], [f'site:redfin.com "{self.sale.address}"'])
        self.assertIn('用 Google 搜尋 Redfin：', digest['text_body'])
        self.assertIn('用 Google 搜尋 Redfin：', digest['html_body'])
        self.assertNotIn('<North>', digest['html_body'])

    def test_filter_layers_and_zero_explanation_are_adjacent_to_evidence(self):
        self.home = replace(self.home, comp_square_footage='1000:2500', ranked_comp_square_footage='1000:1900')
        for key in ('text_body', 'html_body'):
            body = self.digest([])[key]
            self.assertIn('須同時符合', body)
            self.assertIn('初步尺寸篩選：室內 sqft 1000 至 2500', body)
            self.assertIn('最終額外限制：室內 sqft 1000 至 1900', body)
            zero_section = body.split('同房型成交紀錄')[-1].split('同房型目前待售開價')[0]
            self.assertIn('零筆符合紀錄不代表周邊沒有成交', zero_section)
        self.home = replace(self.home, ranked_comp_square_footage=None)
        self.assertNotIn('最終額外限制', self.digest([])['text_body'])

    def test_link_text_identifies_each_destination_without_aria(self):
        second = replace(self.sale, id='second', address='3 Synthetic Avenue')
        html = self.digest([self.sale, second])['html_body']
        class Names(HTMLParser):
            def __init__(self):
                super().__init__(); self.names = []; self.inside = False
            def handle_starttag(self, tag, attrs):
                if tag == 'a': self.inside = True; self.names.append('')
            def handle_endtag(self, tag):
                if tag == 'a': self.inside = False
            def handle_data(self, value):
                if self.inside: self.names[-1] += value
        parser = Names(); parser.feed(html)
        self.assertEqual(len(set(parser.names)), 2)
        for address in (self.sale.address, second.address):
            self.assertTrue(any(address in name for name in parser.names))

    def test_source_provided_redfin_page_is_labeled_separately(self):
        url = 'https://www.redfin.com/CA/Synthetic/2-Synthetic-St-90000/home/123'
        digest = self.digest([replace(self.sale, redfin_url=url)])
        self.assertIn(url, digest['text_body'])
        self.assertIn('來源提供，請核對地址', digest['html_body'])
        self.assertNotIn('google.com/search', digest['html_body'])


if __name__ == '__main__':
    unittest.main()
