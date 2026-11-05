from __future__ import annotations

import html
from typing import Dict, List, Optional

from models import ComparableSale, HomeRun, TrackerRun
from normalize import new_comps, percent_change
from state import TrackerState


def render_digest(run: TrackerRun, previous: TrackerState) -> Dict[str, str]:
    subject = f"房市週報：{len(run.homes)} 間房屋，{sum(_new_count(h, previous) for h in run.homes)} 筆新增物件"
    text_sections = [f"產生時間： {run.generated_at}", ""]
    html_sections = [
        '<html lang="zh-Hant"><head><meta charset="utf-8"></head><body>',
        f"<p><strong>產生時間：</strong> {html.escape(run.generated_at)}</p>",
    ]
    for home_run in run.homes:
        text_sections.append(_render_home_text(home_run, previous))
        html_sections.append(_render_home_html(home_run, previous))
    html_sections.append("</body></html>")
    return {"subject": subject, "text_body": "\n\n".join(text_sections), "html_body": "\n".join(html_sections)}


def _render_home_text(home_run: HomeRun, previous: TrackerState) -> str:
    prev_home = previous.homes.get(home_run.home.id)
    selected = home_run.selected_comps
    fresh = new_comps(selected, prev_home.seen_comp_ids if prev_home else [])
    sale_change = percent_change(home_run.median_sale_price, prev_home.median_sale_price if prev_home else None)
    ppsf_change = percent_change(home_run.median_price_per_sqft, prev_home.median_price_per_sqft if prev_home else None)
    lines = [
        f"{home_run.home.name} - {home_run.home.address}",
        f"RentCast 初步篩選： {_configured_filter(home_run)}",
        f"相似物件排序篩選： {_ranked_filter(home_run)}",
        f"追蹤房屋： {_subject_summary(home_run)}",
        f"購入價格參考： {_money(home_run.home.purchase_price)}",
        f"排序方式： {_ranking_explanation(home_run)}",
        f"初步候選物件： {len(home_run.comps)}",
        f"選出的相似物件： {len(selected)}",
        f"最終比較物件：{len(home_run.final_comps)} 筆",
        f"自上次寄送後新增： {len(fresh)}",
        f"比較物件價格中位數： {_money(home_run.median_sale_price)} ({_pct(sale_change)} 相較上次成功寄送)",
        f"比較物件每 sqft 價格中位數： {_money(home_run.median_price_per_sqft)} ({_pct(ppsf_change)} 相較上次成功寄送)",
    ]
    if not prev_home:
        lines.append("尚無先前寄送紀錄；目前選出的物件皆視為新增物件。")
    lines.append("")
    if prev_home:
        lines.append("自上次寄送後新增的物件：")
        lines.extend(_text_new_rows(fresh, home_run) if fresh else ["  無"])
        lines.append("")
    lines.append("")
    lines.append("相似物件排序結果：")
    lines.extend(_text_rows(selected) if selected else ["  無符合條件的物件"])
    if home_run.home.close_listing_radius_miles is not None:
        lines.append("")
        lines.append(f"附近待售物件，範圍 {_num(home_run.home.close_listing_radius_miles)} 英里：")
        lines.extend(_text_rows(home_run.nearby_listings) if home_run.nearby_listings else ["  無符合條件的物件"])
    return "\n".join(lines)


def _render_home_html(home_run: HomeRun, previous: TrackerState) -> str:
    prev_home = previous.homes.get(home_run.home.id)
    selected = home_run.selected_comps
    fresh = new_comps(selected, prev_home.seen_comp_ids if prev_home else [])
    sale_change = percent_change(home_run.median_sale_price, prev_home.median_sale_price if prev_home else None)
    ppsf_change = percent_change(home_run.median_price_per_sqft, prev_home.median_price_per_sqft if prev_home else None)
    parts = [
        f"<h2>{html.escape(home_run.home.name)}</h2>",
        f"<p>{html.escape(home_run.home.address)}</p>",
        "<ul>",
        f"<li>RentCast 初步篩選： {html.escape(_configured_filter(home_run))}</li>",
        f"<li>相似物件排序篩選： {html.escape(_ranked_filter(home_run))}</li>",
        f"<li>追蹤房屋： {html.escape(_subject_summary(home_run))}</li>",
        f"<li>購入價格參考： {_money(home_run.home.purchase_price)}</li>",
        f"<li>排序方式： {html.escape(_ranking_explanation(home_run))}</li>",
        f"<li>初步候選物件： {len(home_run.comps)}</li>",
        f"<li>選出的相似物件： {len(selected)}</li>",
        f"<li>最終比較物件：{len(home_run.final_comps)} 筆</li>",
        f"<li>自上次寄送後新增： {len(fresh)}</li>",
        f"<li>比較物件價格中位數： {_money(home_run.median_sale_price)} ({_pct(sale_change)} 相較上次成功寄送)</li>",
        f"<li>比較物件每 sqft 價格中位數： {_money(home_run.median_price_per_sqft)} ({_pct(ppsf_change)} 相較上次成功寄送)</li>",
        "</ul>",
    ]
    if not prev_home:
        parts.append("<p><em>尚無先前寄送紀錄；目前選出的物件皆視為新增物件。</em></p>")
    else:
        parts.append("<h3>自上次寄送後新增的物件</h3>")
        parts.append(_html_new_rows(fresh, home_run))
    parts.append("<h3>相似物件排序結果</h3>")
    parts.append(_html_detail_rows(selected, home_run))
    if home_run.home.close_listing_radius_miles is not None:
        parts.append(f"<h3>附近待售物件，範圍 {html.escape(_num(home_run.home.close_listing_radius_miles))} 英里</h3>")
        parts.append(_html_listing_table(home_run.nearby_listings))
    return "\n".join(parts)


def _text_new_rows(comps: List[ComparableSale], home_run: HomeRun) -> List[str]:
    final_ids = {comp.id for comp in home_run.final_comps}
    rows = []
    for comp in comps:
        recommendation = "納入最終比較物件" if comp.id in final_ids else "僅供觀察"
        rows.append(f"  {comp.address} | {recommendation} | 排序分數 {_num(comp.rank_score)} | 參考價格 {_money(comp.sale_price)} | 最後更新 {_plain(comp.last_seen_date)}")
    return rows


def _html_new_rows(comps: List[ComparableSale], home_run: HomeRun) -> str:
    if not comps:
        return "<p>無</p>"
    final_ids = {comp.id for comp in home_run.final_comps}
    rows = ["<ul>"]
    for comp in comps:
        recommendation = "納入最終比較物件" if comp.id in final_ids else "僅供觀察"
        rows.append(
            "<li>"
            f"{html.escape(comp.address)} - {html.escape(recommendation)}"
            f" (排序分數 {html.escape(_num(comp.rank_score))}, 參考價格 {html.escape(_money(comp.sale_price))}, 最後更新 {html.escape(_plain(comp.last_seen_date))})"
            "</li>"
        )
    rows.append("</ul>")
    return "".join(rows)


def _text_rows(comps: List[ComparableSale]) -> List[str]:
    rows = []
    include_hoa = _include_hoa_columns(comps)
    for comp in comps:
        extras = []
        if include_hoa:
            extras.append(f"管理費 {_money(comp.hoa_fee)}")
            extras.append(f"相同 HOA {_yes_no(comp.hoa_match)}")
        if comp.notes:
            extras.append(f"備註 {'; '.join(comp.notes)}")
        rows.append(
            "  "
            f"{comp.address} | 排序分數 {_num(comp.rank_score)} | 狀態 {_status(comp.status)} | 距離 {_num(comp.distance_miles)} 英里 | "
            f"{_beds_baths(comp)} | sqft {_plain(comp.sqft)} | 土地面積（sqft） {_plain(comp.lot_size)} | "
            f"開價 {_money(comp.listing_price)} | 成交價 {_money(comp.sold_price)} | 參考價格 {_money(comp.sale_price)} | "
            f"$/sqft {_money(comp.price_per_sqft)} | 上市天數 {_plain(comp.days_on_market)} | "
            f"上市日期 {_plain(comp.listed_date)} | 下架日期 {_plain(comp.removed_date)} | 最後更新 {_plain(comp.last_seen_date)}"
            + (f" | {' | '.join(extras)}" if extras else "")
        )
    return rows


def _html_detail_rows(comps: List[ComparableSale], home_run: HomeRun) -> str:
    if not comps:
        return "<p>無</p>"
    final_ids = {comp.id for comp in home_run.final_comps}
    include_hoa = _include_hoa_columns(comps)
    include_notes = any(comp.notes for comp in comps)
    sections = []
    for comp in comps:
        fields = [
            ("排序分數", _num(comp.rank_score)),
            ("最終比較物件", _yes_no(comp.id in final_ids)),
            ("狀態", _status(comp.status)),
            ("距離", f"{_num(comp.distance_miles)} 英里"),
            ("房間／衛浴", _beds_baths(comp)),
            ("sqft", _plain(comp.sqft)),
            ("土地面積（sqft）", _plain(comp.lot_size)),
            ("開價", _money(comp.listing_price)),
            ("成交價", _money(comp.sold_price)),
            ("參考價格", _money(comp.sale_price)),
            ("$/sqft", _money(comp.price_per_sqft)),
            ("上市天數", _plain(comp.days_on_market)),
            ("上市日期", _plain(comp.listed_date)),
            ("下架日期", _plain(comp.removed_date)),
            ("最後更新", _plain(comp.last_seen_date)),
        ]
        if include_hoa:
            fields.extend([("管理費", _money(comp.hoa_fee)), ("相同 HOA", _yes_no(comp.hoa_match))])
        if include_notes:
            fields.append(("備註", "; ".join(comp.notes) if comp.notes else ""))
        sections.append(_html_comp_detail(comp, fields))
    return "".join(sections)


def _html_comp_detail(comp: ComparableSale, fields: List[tuple[str, str]]) -> str:
    table_style = "border-collapse:collapse;margin:0 0 12px 0;width:100%;max-width:760px;border:1px solid #777;"
    header_style = "background:#eeeeee;border:1px solid #777;padding:6px;"
    label_style = "width:18%;white-space:nowrap;border:1px solid #777;padding:6px;background:#fafafa;"
    value_style = "width:32%;border:1px solid #777;padding:6px;"
    header = (
        f"{html.escape(comp.address)}"
        f" - {html.escape(_money(comp.sale_price))}"
        f" - {html.escape(_status(comp.status))}"
    )
    rows = [
        f'<table border="1" cellpadding="6" cellspacing="0" style="{table_style}">',
        "<tbody>",
        f'<tr><th colspan="4" align="left" style="{header_style}">'
        f"比較物件明細： {header}"
        "</th></tr>",
    ]
    for index in range(0, len(fields), 2):
        left_label, left_value = fields[index]
        if index + 1 < len(fields):
            right_label, right_value = fields[index + 1]
        else:
            right_label, right_value = "", ""
        rows.append(
            "<tr>"
            f'<th align="left" style="{label_style}">{html.escape(left_label)}</th>'
            f'<td style="{value_style}">{html.escape(left_value)}</td>'
            f'<th align="left" style="{label_style}">{html.escape(right_label)}</th>'
            f'<td style="{value_style}">{html.escape(right_value)}</td>'
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _html_listing_table(comps: List[ComparableSale]) -> str:
    if not comps:
        return "<p>無</p>"
    headers = ["地址", "狀態", "距離", "房間／衛浴", "sqft", "土地面積（sqft）", "開價", "$/sqft", "上市天數", "上市日期", "下架日期", "最後更新"]
    rows = [
        '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;margin:0 0 16px 0;width:100%;max-width:980px;border:1px solid #777;">',
        "<thead><tr>",
    ]
    rows.extend(
        f'<th align="left" style="border:1px solid #777;padding:6px;background:#eeeeee;">{html.escape(header)}</th>'
        for header in headers
    )
    rows.append("</tr></thead><tbody>")
    for comp in comps:
        values = [
            comp.address,
            _status(comp.status),
            f"{_num(comp.distance_miles)} 英里",
            _beds_baths(comp),
            _plain(comp.sqft),
            _plain(comp.lot_size),
            _money(comp.listing_price),
            _money(comp.price_per_sqft),
            _plain(comp.days_on_market),
            _plain(comp.listed_date),
            _plain(comp.removed_date),
            _plain(comp.last_seen_date),
        ]
        rows.append(
            "<tr>"
            + "".join(f'<td style="border:1px solid #777;padding:6px;">{html.escape(value)}</td>' for value in values)
            + "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _new_count(home_run: HomeRun, previous: TrackerState) -> int:
    prev_home = previous.homes.get(home_run.home.id)
    return len(new_comps(home_run.selected_comps, prev_home.seen_comp_ids if prev_home else []))


def _money(value: Optional[float]) -> str:
    if value is None:
        return "無資料"
    return f"${value:,.0f}"


def _pct(value: Optional[float]) -> str:
    if value is None:
        return "無資料"
    return f"{value:+.1f}%"


def _ratio(value: Optional[float]) -> str:
    if value is None:
        return "無資料"
    return f"{value * 100:.1f}%"


def _num(value: Optional[float]) -> str:
    if value is None:
        return "無資料"
    return f"{value:.1f}"


def _plain(value) -> str:
    return "無資料" if value is None else str(value)


def _beds_baths(comp: ComparableSale) -> str:
    return f"{_plain(comp.beds)}/{_plain(comp.baths)}"


def _yes_no(value: bool) -> str:
    return "是" if value else "否"


def _include_hoa_columns(comps: List[ComparableSale]) -> bool:
    return any(comp.hoa_fee is not None or comp.hoa_match for comp in comps)


def _ranking_explanation(home_run: HomeRun) -> str:
    if home_run.home.property_type.lower() == "townhouse":
        return "優先考量連棟住宅類型、相同 HOA／社區、sqft 相似度、距離及資料新舊"
    return "優先考量房屋類型、sqft／土地面積相似度、距離、資料新舊及備註"


def _configured_filter(home_run: HomeRun) -> str:
    home = home_run.home
    parts = [
        _property_type(home.property_type),
        f"{home.max_radius_miles:g} 英里",
        f"{home.days_old} 天",
        f"{home.comp_count} 筆比較物件",
    ]
    if home.bedrooms:
        parts.append(f"房間 {home.bedrooms}")
    if home.bathrooms:
        parts.append(f"衛浴 {home.bathrooms}")
    if home.square_footage:
        parts.append(f"sqft {home.square_footage}")
    if home.lot_size:
        parts.append(f"土地面積（sqft） {home.lot_size}")
    if home.year_built:
        parts.append(f"建造年份 {home.year_built}")
    if home.comp_square_footage:
        parts.append(f"比較物件 sqft {home.comp_square_footage}")
    if home.comp_lot_size:
        parts.append(f"比較物件土地面積（sqft） {home.comp_lot_size}")
    if home.display_count:
        parts.append(f"顯示 {home.display_count} 筆")
    if home.close_listing_radius_miles is not None:
        parts.append(f"附近待售物件 {home.close_listing_radius_miles:g} 英里")
    return ", ".join(parts)


def _ranked_filter(home_run: HomeRun) -> str:
    home = home_run.home
    parts = []
    if home.ranked_comp_bedrooms:
        parts.append(f"房間 {home.ranked_comp_bedrooms}")
    if home.ranked_comp_bathrooms:
        parts.append(f"衛浴 {home.ranked_comp_bathrooms}")
    if home.ranked_comp_square_footage:
        parts.append(f"sqft {home.ranked_comp_square_footage}")
    if home.ranked_comp_lot_size:
        parts.append(f"土地面積（sqft） {home.ranked_comp_lot_size}")
    return ", ".join(parts) if parts else "同 RentCast 初步篩選"


def _subject_summary(home_run: HomeRun) -> str:
    subject = home_run.subject_property or {}
    property_type = subject.get("propertyType") or home_run.home.property_type
    beds = subject.get("bedrooms")
    baths = subject.get("bathrooms")
    sqft = subject.get("squareFootage")
    lot = subject.get("lotSize")
    built = subject.get("yearBuilt")
    return f"{_property_type(property_type)}，房間／衛浴 {_plain(beds)}/{_plain(baths)}，sqft {_plain(sqft)}，土地面積（sqft） {_plain(lot)}，建造年份 {_plain(built)}"


def _status(value: Optional[str]) -> str:
    if value is None:
        return "無資料"
    return {
        "active": "待售",
        "pending": "待成交",
        "sold": "已售出",
        "inactive": "已下架",
        "off market": "未上市",
        "contingent": "附條件成交",
        "closed": "已成交",
        "withdrawn": "已撤回",
        "expired": "已到期",
        "canceled": "已取消",
        "cancelled": "已取消",
    }.get(value.lower(), value)


def _property_type(value: str) -> str:
    return {
        "single family": "獨棟住宅",
        "townhouse": "連棟住宅",
        "condo": "公寓",
        "multi-family": "多戶住宅",
        "apartment": "公寓",
        "manufactured": "預製住宅",
        "land": "土地",
    }.get(value.lower(), value)
