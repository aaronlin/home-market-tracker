from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from models import RegionalHistory, RegionalMonth, canonical_property_type


@dataclass(frozen=True)
class RegionalAssessment:
    status: str
    headline: str
    period: str
    comparison_period: str
    evidence: Tuple[str, ...]
    sufficiency: str
    limitations: Tuple[str, ...]
    source_line: str


def _number(value: Optional[float], unit: str = "") -> str:
    return f"{value:g}{unit}" if value is not None else "未提供"


def _money(value: Optional[float]) -> str:
    return f"${value:,.0f}" if value is not None else "未提供"


def _metric(current: Optional[float], previous: Optional[float], unit: str, money: bool = False) -> str:
    formatter = _money if money else lambda value: _number(value, unit)
    prior = formatter(previous) if previous is not None else "未提供"
    text = f"{formatter(current)}，去年同月{(' ' + prior) if previous is not None else '未提供'}"
    if current is not None and previous is not None:
        if money and previous > 0:
            text += f"（{(current - previous) / previous * 100:+.1f}%）"
        elif not money:
            text += f"（{current - previous:+g}{unit}）"
    return text


def assess_regional(history: Optional[RegionalHistory], property_type: str, as_of: datetime) -> RegionalAssessment:
    prior_month = as_of.replace(day=1) - timedelta(days=1)
    period = prior_month.strftime("%Y-%m")
    comparison = f"{prior_month.year - 1:04d}-{prior_month.month:02d}"
    source = (f"來源：RentCast 區域掛牌統計；來源更新 {history.source_updated_at[:10] if history.source_updated_at else '未提供'}；"
              f"擷取 {history.fetched_at[:10] or '未提供'}。") if history else "來源：RentCast 區域掛牌統計；本期未取得。"
    limits = ("同 ZIP、來源房型的全部尺寸；不是附近相似住宅的成交樣本。",
              "開價中位數不是成交價；當月曾待售數不是月底庫存，組成變動不代表同一房屋增值。")

    def result(status, headline, evidence=(), sufficiency="資料充分度：不足以作區域跨期判讀。"):
        return RegionalAssessment(status, headline, period, comparison, tuple(evidence), sufficiency, limits if evidence else (), source)

    if history is None or history.status == "unavailable":
        return result("unavailable", "區域資料未取得，本期不作跨期判讀。")
    if history.status != "ok":
        return result("stale_or_partial", "區域資料本期未更新，暫停跨期判讀。")
    kind = canonical_property_type(property_type)
    rows = {row.month: row for row in history.months if row.complete and row.property_type == kind}
    current = rows.get(period)
    if current is None:
        return result("insufficient", "最近完成月未取得此房型資料，暫停區域跨期判讀。")
    previous = rows.get(comparison)
    dom = _metric(current.median_dom, previous.median_dom if previous else None, " 天")
    new = _metric(current.new_listings, previous.new_listings if previous else None, " 筆")
    total = _metric(current.total_listings, previous.total_listings if previous else None, " 筆")
    price = _metric(current.median_asking_price, previous.median_asking_price if previous else None, "", money=True)
    evidence = (f"掛牌 DOM：{dom}。", f"新增掛牌：{new}；當月曾待售：{total}。", f"開價中位數：{price}。")
    if previous is None:
        return result("insufficient", "區域最近完成月已取得，缺少去年同月基期。", evidence)
    shared = any(getattr(current, field) is not None and getattr(previous, field) is not None
                 for field in ("median_dom", "median_asking_price", "new_listings", "total_listings"))
    if not shared:
        return result("insufficient", "區域歷史可比指標不足，暫不判讀。", evidence)
    if current.median_dom is None or previous.median_dom is None:
        headline = "區域掛牌資料可比較；DOM 變化仍缺證據。"
    elif current.median_dom < previous.median_dom:
        headline = "區域掛牌停留時間較去年同月縮短。"
    elif current.median_dom > previous.median_dom:
        headline = "區域掛牌停留時間較去年同月拉長。"
    else:
        headline = "區域掛牌停留時間與去年同月相同。"
    return result("descriptive_change", headline, evidence,
                  "資料充分度：月份可比較，指標有效樣本數未提供；整體成交熱度仍無法判定。")
