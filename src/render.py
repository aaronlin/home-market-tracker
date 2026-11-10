from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo

from models import ComparableSale, HomeRun, TrackerRun
from state import TrackerState


@dataclass(frozen=True)
class HomeEvidence:
    home_run: HomeRun
    sales: List[ComparableSale]
    active: List[ComparableSale]
    window_days: int
    stale_active: int


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _evidence(home_run: HomeRun, as_of: datetime) -> HomeEvidence:
    day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    pool = home_run.ranked_comps
    eligible_sales = []
    active = []
    stale_active = 0
    for comp in pool:
        if (comp.raw.get("_sale_record") or {}).get("_identity_conflict"):
            continue
        sale = comp.recorded_sale
        sale_date = _date(sale.transaction_date) if sale else None
        if sale and sale.price > 0 and sale_date and sale_date <= day:
            eligible_sales.append(comp)
        if (comp.status or "").lower() == "active" and comp.listing_price and comp.listing_price > 0:
            observed = _date(comp.last_seen_date)
            if observed and 0 <= (day - observed).days <= 7:
                active.append(comp)
            else:
                stale_active += 1
    cap = min(home_run.home.days_old if home_run.home.days_old > 0 else 365, 365)
    windows = sorted({min(90, cap), min(180, cap), cap})
    sales = []
    for window in windows:
        sales = [comp for comp in eligible_sales if _date(comp.recorded_sale.transaction_date) >= day - timedelta(days=window)]
        if len(sales) >= 3:
            break
    sales.sort(key=lambda comp: (comp.recorded_sale.transaction_date, comp.rank_score or 0), reverse=True)
    active.sort(key=lambda comp: (comp.last_seen_date or "", comp.rank_score or 0), reverse=True)
    return HomeEvidence(home_run, sales, active, window, stale_active)


def _summary(view: HomeEvidence) -> str:
    if not view.sales:
        return "缺少可用成交紀錄，目前無法據此判斷房屋價值。"
    if len(view.sales) < 3:
        return "成交樣本偏少，適合逐筆參考，不宜推論房價走勢。"
    return "已有附日期的成交紀錄可供比較；仍不代表這間房的估值。"


def _money(value: int | float | None) -> str:
    return f"${value:,.0f}" if value is not None else "未提供"


def _number(value: int | float | None) -> str:
    return f"{value:g}" if value is not None else "未提供"


def _attributes(comp: ComparableSale) -> str:
    parts = [f"{_number(comp.beds)} 房／{_number(comp.baths)} 衛", f"{comp.sqft:,} sqft" if comp.sqft else "室內面積未提供"]
    if comp.distance_miles is not None:
        parts.append(f"距離 {comp.distance_miles:.2f} 英里")
    if comp.hoa_fee is not None:
        parts.append(f"來源管理費 {_money(comp.hoa_fee)}／月")
    if comp.hoa_match:
        parts.append("符合已設定的同社區提示")
    return " · ".join(parts)


def _details(comp: ComparableSale, kind: str, as_of: datetime) -> List[str]:
    if kind == "sale":
        sale = comp.recorded_sale
        lines = [f"成交價 {_money(sale.price)} · 成交日期 {sale.transaction_date}", _attributes(comp), "來源：RentCast 房屋成交紀錄"]
    else:
        observed = _date(comp.last_seen_date)
        age = (as_of.date() - observed.date()).days
        lines = [f"開價 {_money(comp.listing_price)} · 最後觀察 {observed.date().isoformat()}（{age} 天前）", _attributes(comp)]
        if comp.days_on_market is not None:
            lines.append(f"來源顯示上市 {_number(comp.days_on_market)} 天")
    if comp.notes:
        lines.append("備註：" + "；".join(comp.notes))
    if comp.exclude_from_median:
        lines.append("備註：已設定不納入統計；本期僅逐筆列示。")
    return lines


def _sales_caption(view: HomeEvidence) -> str:
    return f"近 {view.window_days} 天 · {len(view.sales)} 筆符合條件" + (" · 已擴大回溯期間" if view.window_days > 90 else "")


def _counts(view: HomeEvidence) -> str:
    return f"{view.window_days} 天內成交紀錄 {len(view.sales)} 筆 · 近 7 天觀察到的待售開價 {len(view.active)} 筆"


def _subject_summary(home_run: HomeRun) -> str:
    subject = home_run.subject_property or {}
    raw_type = subject.get("propertyType") or home_run.home.property_type
    property_type = {"single family": "獨棟住宅", "townhouse": "連棟住宅", "condo": "公寓"}.get(raw_type.lower(), raw_type)
    parts = [property_type]
    if subject.get("bedrooms") is not None and subject.get("bathrooms") is not None:
        parts.append(f"{_number(subject['bedrooms'])} 房／{_number(subject['bathrooms'])} 衛")
    for key, label in (("squareFootage", "室內"), ("lotSize", "土地")):
        if subject.get(key) is not None:
            parts.append(f"{label} {subject[key]:,} sqft")
    if subject.get("yearBuilt") is not None:
        parts.append(f"{subject['yearBuilt']} 年建")
    return " · ".join(parts)


def _plain_home(view: HomeEvidence, as_of: datetime) -> str:
    home = view.home_run.home
    lines = [home.name, home.address, f"追蹤房屋： {_subject_summary(view.home_run)}", f"搜尋半徑 {home.max_radius_miles:g} 英里；只包含目前查詢與相似條件取得的資料。", _summary(view), "", "成交紀錄", _sales_caption(view)]
    if not view.sales:
        lines.append("查詢範圍內未取得同時具成交價與成交日期的可用紀錄；不以開價補足。")
    for comp in view.sales[:3]:
        lines.extend([comp.address, *_details(comp, "sale", as_of)])
    if len(view.sales) > 3:
        lines.append(f"列出最近 3 筆，共 {len(view.sales)} 筆。")
    lines.extend(["", "目前待售開價", "僅列來源近 7 天仍觀察為待售的物件；開價不等於成交價。"])
    if not view.active:
        lines.append("未取得符合條件且近期仍被觀察為待售的物件；不代表附近沒有待售房屋。")
    for comp in view.active[:3]:
        lines.extend([comp.address, *_details(comp, "active", as_of)])
    if len(view.active) > 3:
        lines.append(f"列出最近觀察的 3 筆，共 {len(view.active)} 筆。")
    if view.stale_active:
        lines.append(f"另有 {view.stale_active} 筆標示待售但觀察日期過舊、缺漏或異常，未列入目前待售。")
    return "\n".join(lines)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _html_rows(comps: List[ComparableSale], kind: str, as_of: datetime) -> str:
    rows = []
    for comp in comps[:3]:
        detail = _details(comp, kind, as_of)
        rows.append('<tr><td style="padding:16px 0;border-bottom:1px solid #dce3df;overflow-wrap:anywhere;word-break:break-word;">'
                    f'<p style="margin:0 0 6px;font-size:16px;font-weight:600;color:#172f32;">{_esc(comp.address)}</p>'
                    f'<p style="margin:0 0 5px;font-size:16px;color:#172f32;">{_esc(detail[0])}</p>'
                    + "".join(f'<p style="margin:4px 0 0;font-size:14px;line-height:1.7;color:#526866;">{_esc(line)}</p>' for line in detail[1:]) + '</td></tr>')
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;"><tbody>' + "".join(rows) + '</tbody></table>'


def _html_home(view: HomeEvidence, as_of: datetime) -> str:
    home = view.home_run.home
    parts = [f'<section style="margin-top:36px;"><h2 style="margin:0 0 8px;font-size:22px;line-height:1.4;color:#172f32;">{_esc(home.name)}</h2>',
             f'<p style="margin:0 0 6px;color:#526866;overflow-wrap:anywhere;word-break:break-word;">{_esc(home.address)}</p>',
             f'<p style="margin:0 0 6px;font-size:14px;color:#526866;">追蹤房屋：{_esc(_subject_summary(view.home_run))}</p>',
             f'<p style="margin:0 0 24px;font-size:14px;color:#526866;">搜尋半徑 {home.max_radius_miles:g} 英里；只包含目前查詢與相似條件取得的資料。</p>',
             '<h3 style="margin:24px 0 4px;font-size:18px;color:#172f32;">成交紀錄</h3>',
             f'<p style="margin:0;font-size:14px;color:#526866;">{_esc(_sales_caption(view))}</p>']
    if view.sales:
        parts.append(_html_rows(view.sales, "sale", as_of))
        if len(view.sales) > 3:
            parts.append(f'<p style="font-size:14px;color:#526866;">列出最近 3 筆，共 {len(view.sales)} 筆。</p>')
    else:
        parts.append('<p style="margin:12px 0 24px;padding:16px;background:#f3f5f1;color:#526866;">查詢範圍內未取得同時具成交價與成交日期的可用紀錄；不以開價補足。</p>')
    parts.extend(['<h3 style="margin:28px 0 4px;font-size:18px;color:#172f32;">目前待售開價</h3>', '<p style="margin:0 0 4px;font-size:14px;color:#526866;">僅列來源近 7 天仍觀察為待售的物件；開價不等於成交價。</p>'])
    if view.active:
        parts.append(_html_rows(view.active, "active", as_of))
        if len(view.active) > 3:
            parts.append(f'<p style="font-size:14px;color:#526866;">列出最近觀察的 3 筆，共 {len(view.active)} 筆。</p>')
    else:
        parts.append('<p style="padding:16px;background:#f3f5f1;color:#526866;">未取得符合條件且近期仍被觀察為待售的物件；不代表附近沒有待售房屋。</p>')
    if view.stale_active:
        parts.append(f'<p style="font-size:14px;color:#526866;">另有 {view.stale_active} 筆標示待售但觀察日期過舊、缺漏或異常，未列入目前待售。</p>')
    parts.append("</section>")
    return "".join(parts)


def render_digest(run: TrackerRun, previous: TrackerState) -> Dict[str, str]:
    as_of = datetime.fromisoformat(run.generated_at.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    local_time = as_of.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y/%m/%d %H:%M")
    views = [_evidence(home_run, as_of) for home_run in run.homes]
    title = f"房市近況｜{len(views)} 間房的成交與待售資料"
    time_label = f"資料取得：{local_time}（太平洋時間）"
    baseline = "變化追蹤尚未啟用；本期不提供新增、降價或房價漲跌比較。"
    disclaimer = "僅供市場追蹤，不是房屋估值。成交紀錄來自 RentCast 房屋資料，未經人工查證；下架不代表成交。"
    text = [title, time_label, "", "本期速覽"]
    overview = []
    for view in views:
        text.extend([view.home_run.home.name, _counts(view), _summary(view), ""])
        overview.append(f'<tr><td style="padding:16px 0;border-bottom:1px solid #dce3df;"><h2 style="margin:0 0 6px;font-size:17px;color:#172f32;">{_esc(view.home_run.home.name)}</h2><p style="margin:0 0 6px;font-size:15px;color:#1d6560;">{_esc(_counts(view))}</p><p style="margin:0;font-size:15px;color:#526866;">{_esc(_summary(view))}</p></td></tr>')
    text.extend([baseline, "", *[_plain_home(view, as_of) for view in views], "", disclaimer])
    body = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + _esc(title) + '</title></head>'
            '<body style="margin:0;padding:0;background:#f3f5f1;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',\'Noto Sans TC\',Arial,sans-serif;font-size:16px;line-height:1.75;color:#172f32;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;"><tr><td align="center" style="padding:20px 12px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;max-width:680px;background:#ffffff;table-layout:fixed;"><tr><td style="padding:28px 22px;overflow-wrap:anywhere;word-break:break-word;">'
            f'<h1 style="margin:0 0 8px;font-size:26px;line-height:1.4;font-weight:650;color:#172f32;">房屋市場近況</h1><p style="margin:0 0 20px;font-size:14px;color:#526866;">{_esc(time_label)}</p>'
            '<h2 style="margin:0;font-size:18px;color:#172f32;">本期速覽</h2><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;">' + "".join(overview) + '</table>'
            f'<p style="margin:16px 0 0;font-size:14px;color:#526866;">{_esc(baseline)}</p>' + "".join(_html_home(view, as_of) for view in views)
            + f'<p style="margin:36px 0 0;padding-top:20px;border-top:1px solid #dce3df;font-size:13px;color:#526866;">{_esc(disclaimer)}</p>'
            '</td></tr></table></td></tr></table></body></html>')
    return {"subject": title, "text_body": "\n".join(text), "html_body": body}
