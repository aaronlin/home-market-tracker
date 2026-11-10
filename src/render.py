from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

from market_metrics import asking_gap, cohort_for, source_dom

from models import ComparableSale, HomeRun, TrackerRun
from state import TrackerState


@dataclass(frozen=True)
class HomeEvidence:
    home_run: HomeRun
    sales: List[ComparableSale]
    active: List[ComparableSale]
    window_days: int
    stale_active: int
    context_sales: List[ComparableSale]
    context_active: List[ComparableSale]
    context_days: int
    unknown_type: int


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _evidence(home_run: HomeRun, as_of: datetime) -> HomeEvidence:
    day = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    pool = [comp for comp in home_run.ranked_comps
            if not (comp.raw.get("_sale_record") or {}).get("_identity_conflict")
            and not comp.raw.get("_listing_identity_conflict")]
    primary = [comp for comp in pool if cohort_for(home_run.home, comp) == "primary"]
    context = [comp for comp in pool if cohort_for(home_run.home, comp) == "condo_context"]
    cap = min(home_run.home.days_old if home_run.home.days_old > 0 else 365, 365)

    def sales_in(rows, window):
        eligible = [comp for comp in rows if comp.recorded_sale and comp.recorded_sale.price > 0
                    and _date(comp.recorded_sale.transaction_date)
                    and day - timedelta(days=window) <= _date(comp.recorded_sale.transaction_date) <= day]
        return sorted(eligible, key=lambda comp: (comp.recorded_sale.transaction_date, comp.rank_score or 0), reverse=True)

    def active_in(rows):
        active, stale = [], 0
        for comp in rows:
            if (comp.status or "").lower() != "active" or not comp.listing_price or comp.listing_price <= 0:
                continue
            observed = _date(comp.last_seen_date)
            if observed and 0 <= (day - observed).days <= 7:
                active.append(comp)
            else:
                stale += 1
        active.sort(key=lambda comp: (comp.last_seen_date or "", comp.rank_score or 0), reverse=True)
        return active, stale

    for window in sorted({min(90, cap), min(180, cap), cap}):
        sales = sales_in(primary, window)
        if len(sales) >= 3:
            break
    active, stale = active_in(primary)
    context_active, context_stale = active_in(context)
    return HomeEvidence(home_run, sales, active, window, stale + context_stale,
                        sales_in(context, cap), context_active, cap,
                        sum(cohort_for(home_run.home, comp) == "unknown" for comp in pool))


def _summary(view: HomeEvidence) -> str:
    if not view.sales:
        return "缺少同房型可用成交紀錄，目前無法據此判斷房屋價值或市場冷熱。"
    if len(view.sales) < 3:
        return "成交樣本偏少，適合逐筆參考，不宜推論房價走勢。"
    return "可逐筆比較成交與掛牌證據；樣本不代表完整市場或這間房的估值。"


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


def _classifications(comp: ComparableSale) -> str:
    values = comp.source_property_types
    if not values:
        return "來源房型未提供"
    if len(set(values.values())) == 1:
        return "來源房型：" + next(iter(values.values()))
    names = {"property": "房屋紀錄", "listing": "掛牌", "avm": "AVM 候選"}
    return "來源房型：" + "；".join(f"{names.get(key, key)} {value}" for key, value in values.items())


def _redfin_link(comp: ComparableSale) -> tuple[str, str]:
    if comp.redfin_url:
        return comp.redfin_url, f"Redfin 房源頁：{comp.address}（來源提供，請核對地址）"
    query = urlencode({"q": f'site:redfin.com "{comp.address}"'})
    return "https://www.google.com/search?" + query, f"用 Google 搜尋 Redfin：{comp.address}"


def _details(comp: ComparableSale, kind: str, as_of: datetime) -> List[str]:
    if kind == "sale":
        sale = comp.recorded_sale
        lines = [f"成交價 {_money(sale.price)} · 成交日期 {sale.transaction_date}", _attributes(comp), "來源：RentCast 房屋成交紀錄", _classifications(comp)]
        if comp.sqft and comp.sqft > 0:
            lines.append(f"成交單價 {_money(sale.price / comp.sqft)}／sqft")
        gap = asking_gap(comp)
        if gap:
            sign = "+" if gap.amount >= 0 else "-"
            lines.extend([f"售前開價參考 {_money(gap.observation.price)}（觀察日期 {gap.observation.observed_date}）",
                          f"成交對開價參考落差 {sign}${abs(gap.amount):,.0f}（{gap.percent:+.1f}%）；非已確認最終／原始開價比。"])
            dom = source_dom(gap.observation.days_on_market)
            if dom is not None:
                lines.append(f"對應掛牌參考的來源 DOM：{dom} 天；非到成交所需天數。")
        else:
            lines.append("開價落差：無可核對的售前掛牌參考，不計算。")
    else:
        observed = _date(comp.last_seen_date)
        age = (as_of.date() - observed.date()).days
        lines = [f"開價 {_money(comp.listing_price)} · 最後觀察 {observed.date().isoformat()}（{age} 天前）", _attributes(comp), _classifications(comp)]
        dom = source_dom(comp.days_on_market)
        lines.append(f"來源 DOM：{dom} 天（截至觀察日；非成交速度）。" if dom is not None else "來源 DOM：未提供有效數值。")
    if comp.notes:
        lines.append("備註：" + "；".join(comp.notes))
    if comp.exclude_from_median:
        lines.append("備註：已設定不納入統計；本期僅逐筆列示。")
    return lines


def _sales_caption(view: HomeEvidence) -> str:
    return f"近 {view.window_days} 天 · {len(view.sales)} 筆符合條件" + (" · 已擴大回溯期間" if view.window_days > 90 else "")


def _counts(view: HomeEvidence) -> str:
    return (f"同房型 {view.window_days} 天內成交紀錄 {len(view.sales)} 筆 · 近 7 天觀察到的待售開價 {len(view.active)} 筆"
            + (f"；另有 Condo 參考成交 {len(view.context_sales)} 筆（近 {view.context_days} 天，未併入同房型）" if view.context_sales else ""))


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


def _dom_caption(rows: List[ComparableSale]) -> str:
    available = sum(source_dom(comp.days_on_market) is not None for comp in rows)
    return f"來源 DOM 有效值 {available}/{len(rows)} 筆；不是掛牌至簽約或成交的天數。"


def _health_lines(view: HomeEvidence) -> List[str]:
    lines = []
    metadata = view.home_run.retrieval
    if not metadata:
        lines.append("未保存查詢頁數與範圍，來源搜尋完整性未知。")
    elif any(not entry.complete for entry in metadata):
        lines.append("有搜尋達到本期頁數上限；以下僅為部分已取得資料，不能視為完整市場。")
    else:
        lines.append("本次搜尋分頁已讀完；這不保證供應商涵蓋所有交易。")
    for entry in metadata:
        if entry.source == "properties":
            days = entry.filters.get("saleDateRange", "未記錄")
            lines.append(f"房屋紀錄搜尋近 {days} 天，取得 {entry.rows} 筆原始紀錄；再依尺寸、房型與日期判定可比資格。")
    dates = sorted({entry.fetched_at[:10] for entry in metadata if entry.fetched_at})
    if dates:
        lines.append("來源查詢日期：" + "、".join(dates) + "。")
    if view.unknown_type:
        lines.append(f"另有 {view.unknown_type} 筆候選房型未提供，未併入同房型證據。")
    if not view.sales:
        lines.append("同房型符合條件的成交為零，是目前資料與篩選結果，不代表周邊一年沒有成交。")
    if view.stale_active:
        lines.append(f"另有 {view.stale_active} 筆標示待售但觀察日期過舊、缺漏或異常，未列入目前待售。")
    return lines


def _plain_rows(rows: List[ComparableSale], kind: str, as_of: datetime) -> List[str]:
    lines = []
    for comp in rows[:3]:
        href, label = _redfin_link(comp)
        lines.extend([comp.address, *_details(comp, kind, as_of), f"{label}：{href}", ""])
    if len(rows) > 3:
        lines.append(f"列出最近 3 筆，共 {len(rows)} 筆。")
    return lines


def _criteria(home_run: HomeRun) -> str:
    home = home_run.home
    groups = []
    for heading, fields in (
        ("初步尺寸篩選", (("室內 sqft", home.comp_square_footage), ("土地 sqft", home.comp_lot_size))),
        ("最終額外限制", (("房數", home.ranked_comp_bedrooms), ("衛浴", home.ranked_comp_bathrooms),
                         ("室內 sqft", home.ranked_comp_square_footage), ("土地 sqft", home.ranked_comp_lot_size))),
    ):
        parts = [label + " " + str(value).replace(":", " 至 ") for label, value in fields if value]
        if parts:
            groups.append(heading + "：" + "；".join(parts))
    if not groups:
        return "相似條件：未另設尺寸／房數硬限制，依來源房型分類。"
    return "相似條件（須同時符合）：" + "。".join(groups) + "。"


def _plain_home(view: HomeEvidence, as_of: datetime) -> str:
    home = view.home_run.home
    lines = [home.name, home.address, f"追蹤房屋： {_subject_summary(view.home_run)}", f"搜尋半徑 {home.max_radius_miles:g} 英里。", _criteria(view.home_run), _summary(view), "", "同房型成交紀錄", _sales_caption(view)]
    if not view.sales:
        lines.append("查詢範圍內未取得同時具成交價、成交日期及相符來源房型的可用紀錄；不以開價補足。零筆符合紀錄不代表周邊沒有成交。")
    lines.extend(_plain_rows(view.sales, "sale", as_of))
    lines.extend(["", "同房型目前待售開價", "僅列來源近 7 天仍觀察為待售的物件；開價不等於成交價。", _dom_caption(view.active)])
    if not view.active:
        lines.append("未取得符合條件且近期仍被觀察為待售的物件；不代表附近沒有待售房屋。")
    lines.extend(_plain_rows(view.active, "active", as_of))
    if view.context_sales or view.context_active:
        lines.extend(["", "較廣分類參考：Condo", f"近 {view.context_days} 天成交 {len(view.context_sales)} 筆；近期待售 {len(view.context_active)} 筆。來源分類與追蹤的 Townhouse 不同，未併入同房型。", "參考成交紀錄"])
        lines.extend(_plain_rows(view.context_sales, "sale", as_of))
        if view.context_active:
            lines.extend(["Condo 待售參考", _dom_caption(view.context_active), *_plain_rows(view.context_active, "active", as_of)])
    lines.extend(["", "資料完整度", *_health_lines(view)])
    return "\n".join(lines)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _html_rows(comps: List[ComparableSale], kind: str, as_of: datetime) -> str:
    rows = []
    for comp in comps[:3]:
        detail = _details(comp, kind, as_of)
        href, label = _redfin_link(comp)
        rows.append('<tr><td style="padding:16px 0;border-bottom:1px solid #dce3df;overflow-wrap:anywhere;word-break:break-word;">'
                    f'<p style="margin:0 0 6px;font-size:16px;font-weight:600;color:#172f32;">{_esc(comp.address)}</p>'
                    f'<p style="margin:0 0 5px;font-size:16px;color:#172f32;">{_esc(detail[0])}</p>'
                    + "".join(f'<p style="margin:4px 0 0;font-size:14px;line-height:1.7;color:#526866;">{_esc(line)}</p>' for line in detail[1:]) + f'<p style="margin:10px 0 0;font-size:14px;"><a href="{_esc(href)}" style="color:#1d6560;text-decoration:underline;text-underline-offset:3px;display:inline-block;padding:10px 0;">{_esc(label)}</a></p></td></tr>')
    return '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;"><tbody>' + "".join(rows) + '</tbody></table>'


def _html_home(view: HomeEvidence, as_of: datetime) -> str:
    home = view.home_run.home
    def paragraph(text):
        return f'<p style="margin:8px 0;font-size:14px;color:#526866;">{_esc(text)}</p>'
    def section(title, caption, rows, kind, empty):
        result = [f'<h3 style="margin:28px 0 4px;font-size:18px;color:#172f32;">{_esc(title)}</h3>', paragraph(caption)]
        if rows:
            result.append(_html_rows(rows, kind, as_of))
            if len(rows) > 3:
                result.append(paragraph(f"列出最近 3 筆，共 {len(rows)} 筆。"))
        elif empty:
            result.append(f'<p style="padding:16px;background:#f3f5f1;color:#526866;">{_esc(empty)}</p>')
        return "".join(result)
    parts = [f'<section style="margin-top:36px;"><h2 style="margin:0 0 8px;font-size:22px;line-height:1.4;color:#172f32;">{_esc(home.name)}</h2>',
             paragraph(home.address), paragraph("追蹤房屋：" + _subject_summary(view.home_run)),
             paragraph(f"搜尋半徑 {home.max_radius_miles:g} 英里。"), paragraph(_criteria(view.home_run)),
             section("同房型成交紀錄", _sales_caption(view), view.sales, "sale", "查詢範圍內未取得同時具成交價、成交日期及相符來源房型的可用紀錄；不以開價補足。零筆符合紀錄不代表周邊沒有成交。"),
             section("同房型目前待售開價", "僅列來源近 7 天仍觀察為待售的物件；開價不等於成交價。 " + _dom_caption(view.active), view.active, "active", "未取得符合條件且近期仍被觀察為待售的物件；不代表附近沒有待售房屋。")]
    if view.context_sales or view.context_active:
        parts.extend(['<h3 style="margin:32px 0 4px;font-size:18px;color:#172f32;">較廣分類參考：Condo</h3>',
                      paragraph(f"近 {view.context_days} 天成交 {len(view.context_sales)} 筆；近期待售 {len(view.context_active)} 筆。來源分類與追蹤的 Townhouse 不同，未併入同房型。"),
                      section("Condo 參考成交紀錄", "僅作較廣分類的個別參考。", view.context_sales, "sale", "")])
        if view.context_active:
            parts.append(section("Condo 待售參考", _dom_caption(view.context_active), view.context_active, "active", ""))
    parts.extend(['<h3 style="margin:28px 0 4px;font-size:16px;color:#172f32;">資料完整度</h3>', *[paragraph(line) for line in _health_lines(view)], '</section>'])
    return "".join(parts)


def render_digest(run: TrackerRun, previous: TrackerState) -> Dict[str, str]:
    as_of = datetime.fromisoformat(run.generated_at.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    as_of = as_of.astimezone(timezone.utc)
    local_time = as_of.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y/%m/%d %H:%M")
    views = [_evidence(home_run, as_of) for home_run in run.homes]
    title = f"房市近況｜{len(views)} 間房的成交與待售資料"
    time_label = f"報告產生：{local_time}（太平洋時間）"
    baseline = "變化追蹤尚未啟用；本期不提供新增、降價或房價漲跌比較。"
    disclaimer = "提供買賣判斷的市場證據，不是房屋估值或買賣建議。成交紀錄來自 RentCast 房屋資料；下架不代表成交。開價參考落差不是已確認的最終／原始開價比，也不代表議價幅度。"
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
